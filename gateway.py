"""
Gateway 网关模块

Gateway 是多渠道接入的核心网关，负责：
- 管理多个渠道适配器（Channel）
- 处理入站消息：从 MessageBus 消费 → 分发给对应的 Agent
- 分发出站消息：从 MessageBus 消费 → 发送给对应的渠道
- 缓存 Agent 实例（按 session_key）

使用示例：
    bus = MessageBus()
    cli_channel = CLIChannel(bus)

    def create_agent(session_key: str) -> AgentLoop:
        # 根据 session_key 创建 Agent
        ...

    gateway = Gateway(
        bus=bus,
        channels=[cli_channel],
        agent_factory=create_agent
    )

    # 启动网关
    asyncio.run(gateway.run())
"""

import asyncio
import os
from typing import Callable

from bus.queue import MessageBus, InboundMessage, OutboundMessage
from channels.base import Channel
from agent.loop import AgentLoop
from privacy import safe_print as print
from gateway_coordination import (
    InMemoryRuntimeCoordinator, RequestScope, RuntimeCoordinator,
    new_worker_id, payload_digest,
)


class Gateway:
    """
    网关核心类

    管理多渠道接入和消息路由。
    """

    def __init__(
        self,
        bus: MessageBus,
        channels: list[Channel],
        agent_factory: Callable[[str], AgentLoop],
        *,
        max_concurrency: int | None = None,
        drain_timeout_seconds: float = 30.0,
        lease_seconds: int = 30,
        coordinator: RuntimeCoordinator | None = None,
        worker_id: str | None = None,
    ) -> None:
        """
        Args:
            bus: 消息总线实例
            channels: 渠道适配器列表
            agent_factory: Agent 创建函数，输入 session_key，返回 AgentLoop 实例
        """
        self.bus = bus
        self.channels = channels
        self.agent_factory = agent_factory
        configured = int(os.environ.get("NANOCLAW_GATEWAY_MAX_CONCURRENCY", "16"))
        self.max_concurrency = configured if max_concurrency is None else max_concurrency
        if self.max_concurrency <= 0:
            raise ValueError("gateway max_concurrency must be greater than zero")
        if drain_timeout_seconds <= 0 or lease_seconds <= 0:
            raise ValueError("gateway drain timeout and lease must be greater than zero")
        self.drain_timeout_seconds = drain_timeout_seconds
        self.lease_seconds = lease_seconds
        self.coordinator = coordinator or InMemoryRuntimeCoordinator()
        self.worker_id = worker_id or new_worker_id()

        # 内部状态
        self._agents: dict[str, AgentLoop] = {}  # 按 session_key 缓存 Agent
        self._channel_map: dict[str, Channel] = {}  # 按渠道名索引渠道
        self._semaphore = asyncio.Semaphore(self.max_concurrency)
        self._session_locks: dict[str, asyncio.Lock] = {}
        self._inflight: set[asyncio.Task] = set()
        self._shutdown_started = False

        # 构建渠道映射表
        for channel in channels:
            self._channel_map[channel.name] = channel

    async def run(self) -> None:
        """
        启动网关

        用 asyncio.gather 并发启动：
        - 所有渠道的 start()
        - _process_inbound() 入站消费循环
        - _dispatch_outbound() 出站分发循环
        """
        tasks: list[asyncio.Task] = []

        # 添加所有渠道的启动任务
        for channel in self.channels:
            tasks.append(asyncio.create_task(channel.start(), name=f"channel:{channel.name}"))

        # 添加入站和出站处理循环
        tasks.append(asyncio.create_task(self._process_inbound(), name="gateway:inbound"))
        tasks.append(asyncio.create_task(self._dispatch_outbound(), name="gateway:outbound"))

        # 并发启动所有任务
        try:
            # 等待所有任务（通常会阻塞直到某个渠道退出）
            await asyncio.gather(*tasks)
        except asyncio.CancelledError:
            pass
        finally:
            # 取消所有未完成的任务
            for task in tasks:
                if not task.done():
                    task.cancel()
            # 等待任务取消完成（忽略 CancelledError）
            await asyncio.gather(*tasks, return_exceptions=True)
            # 关闭所有渠道
            await self.shutdown()

    async def _process_inbound(self) -> None:
        """
        处理入站消息循环

        while True 循环：
        - 从 bus.consume_inbound() 取消息
        - 构造 session_key = f"{msg.channel}:{msg.sender_id}"
        - 从 _agents 缓存获取 Agent，不存在则调用 agent_factory 创建
        - 调用 agent.run(msg.content)
        - 构造 OutboundMessage 发布到 bus.publish_outbound()
        - 用 try-except 包裹，Agent 出错时发送友好错误消息
        """
        while True:
            try:
                # 从消息总线消费入站消息
                inbound_msg = await self.bus.consume_inbound()

                task = asyncio.create_task(self._handle_inbound(inbound_msg),
                                           name="gateway:request")
                self._inflight.add(task)
                task.add_done_callback(self._inflight.discard)

            except asyncio.CancelledError:
                # 任务被取消，退出循环
                break

            except Exception as e:
                # 消费循环异常，打印日志并继续
                print(f"警告: 入站处理循环异常 - {e}")
                continue

    def _request_scope(self, message: InboundMessage) -> RequestScope | None:
        raw = message.raw or {}
        conversation_id = str(raw.get("conversation_id") or "")
        request_id = str(raw.get("request_id") or "")
        if not conversation_id or not request_id:
            return None
        return RequestScope(
            tenant_id=str(raw.get("tenant_id") or ""),
            account_id=str(raw.get("account_id") or ""),
            channel=message.channel,
            conversation_id=conversation_id,
            request_id=request_id,
        )

    async def _renew_lease(self, scope: RequestScope) -> None:
        while True:
            await asyncio.sleep(max(1, self.lease_seconds // 3))
            if not await self.coordinator.renew_conversation(
                    scope, self.worker_id, self.lease_seconds):
                raise RuntimeError("conversation lease lost")

    async def _acquire_lease(self, scope: RequestScope) -> None:
        deadline = asyncio.get_running_loop().time() + self.lease_seconds
        while not await self.coordinator.acquire_conversation(
                scope, self.worker_id, self.lease_seconds):
            if asyncio.get_running_loop().time() >= deadline:
                raise TimeoutError("conversation lease unavailable")
            await asyncio.sleep(0.05)

    async def _handle_inbound(self, inbound_msg: InboundMessage) -> None:
        raw = inbound_msg.raw or {}
        session_key = f"{inbound_msg.channel}:{inbound_msg.sender_id}"
        event = raw.get("event")
        if event == "conversation_deleted":
            removed_agent = self._agents.pop(session_key, None)
            if removed_agent is not None and hasattr(removed_agent, "clear_peer_history"):
                removed_agent.clear_peer_history()
            return
        if event == "clear_conversation":
            existing_agent = self._agents.get(session_key)
            if existing_agent is not None:
                existing_agent.clear_history()
            return

        scope = self._request_scope(inbound_msg)
        claimed = False
        lease_acquired = False
        renewal: asyncio.Task | None = None
        request_id = str(raw.get("request_id") or "") or None
        conversation_id = str(raw.get("conversation_id") or "") or None
        try:
            if scope is not None:
                claim = await self.coordinator.claim(
                    scope, payload_digest(inbound_msg.content), self.worker_id,
                    self.lease_seconds,
                )
                if claim.decision in {"running", "completed"}:
                    await self.bus.publish_outbound(OutboundMessage(
                        channel=inbound_msg.channel, chat_id=inbound_msg.chat_id,
                        content="", conversation_id=conversation_id,
                        request_id=request_id, event_type="chat.duplicate",
                    ))
                    return
                if claim.decision == "conflict":
                    await self.bus.publish_outbound(OutboundMessage(
                        channel=inbound_msg.channel, chat_id=inbound_msg.chat_id,
                        content="", conversation_id=conversation_id,
                        request_id=request_id, event_type="error",
                        error_code="request_id_conflict",
                    ))
                    return
                claimed = True

            lock = self._session_locks.setdefault(session_key, asyncio.Lock())
            async with self._semaphore, lock:
                if scope is not None:
                    await self._acquire_lease(scope)
                    lease_acquired = True
                    renewal = asyncio.create_task(self._renew_lease(scope))
                # A shared coordinator may hand the next turn to another process;
                # reconstruct the Agent so its file-backed history is reloaded.
                shared = bool(getattr(self.coordinator, "shared_across_instances", False))
                agent = None if shared else self._agents.get(session_key)
                if agent is None:
                    agent = self.agent_factory(session_key)
                    if not shared:
                        self._agents[session_key] = agent
                if hasattr(agent, "set_request_context"):
                    trusted_context = {"channel": inbound_msg.channel,
                                       "language": raw.get("language", "en")}
                    if (inbound_msg.channel == "customer_portal"
                            and "tenant_id" in raw and "account_id" in raw):
                        trusted_context.update({key: raw[key] for key in (
                            "tenant_id", "account_id", "conversation_id", "request_id"
                        ) if key in raw})
                    agent.set_request_context(trusted_context)
                response = await agent.run(inbound_msg.content)
                if scope is not None and not await self.coordinator.complete(
                        scope, self.worker_id):
                    raise RuntimeError("request claim lost before completion")
                await self.bus.publish_outbound(OutboundMessage(
                    channel=inbound_msg.channel, chat_id=inbound_msg.chat_id,
                    content=response, conversation_id=conversation_id,
                    request_id=request_id,
                ))
        except asyncio.CancelledError:
            if claimed and scope is not None:
                await self.coordinator.fail(scope, self.worker_id)
            raise
        except Exception as exc:
            if claimed and scope is not None:
                await self.coordinator.fail(scope, self.worker_id)
            language = str(raw.get("language", "en"))
            public_error = {
                "zh": "抱歉，暂时无法处理您的询盘，请稍后重试。",
                "de": "Ihre Anfrage kann derzeit nicht bearbeitet werden. Bitte versuchen Sie es später erneut.",
                "en": "Sorry, your inquiry cannot be processed right now. Please try again later.",
            }.get(language, "Sorry, your inquiry cannot be processed right now. Please try again later.")
            await self.bus.publish_outbound(OutboundMessage(
                channel=inbound_msg.channel, chat_id=inbound_msg.chat_id,
                content=(public_error if inbound_msg.channel == "customer_portal"
                         else "抱歉，处理消息时发生了错误。请稍后重试。"),
                conversation_id=conversation_id, request_id=request_id,
                event_type="error", error_code="gateway_processing_failed",
            ))
            print(f"警告: Agent 处理出错 - {type(exc).__name__}")
        finally:
            if renewal is not None:
                renewal.cancel()
                await asyncio.gather(renewal, return_exceptions=True)
            if lease_acquired and scope is not None:
                await self.coordinator.release_conversation(scope, self.worker_id)

    async def _dispatch_outbound(self) -> None:
        """
        分发出站消息循环

        while True 循环：
        - 从 bus.consume_outbound() 取回复
        - 根据 msg.channel 找到对应的 Channel
        - 调用 channel.send(msg)
        """
        while True:
            try:
                # 从消息总线消费出站消息
                outbound_msg = await self.bus.consume_outbound()

                # 查找对应的渠道
                channel = self._channel_map.get(outbound_msg.channel)

                if channel is None:
                    print(f"警告: 渠道 '{outbound_msg.channel}' 不存在，无法发送消息")
                    continue

                # 调用渠道发送消息
                try:
                    await channel.send(outbound_msg)

                except Exception as e:
                    # 渠道发送失败，打印日志
                    print(f"警告: 渠道 '{outbound_msg.channel}' 发送失败 - {e}")

            except asyncio.CancelledError:
                # 任务被取消，退出循环
                break

            except Exception as e:
                # 分发循环异常，打印日志并继续
                print(f"警告: 出站分发循环异常 - {e}")
                continue

    async def shutdown(self) -> None:
        """
        关闭网关

        - 遍历 channels 调用 stop()
        - 清空 _agents 缓存
        """
        if self._shutdown_started:
            return
        self._shutdown_started = True
        if self._inflight:
            _done, pending = await asyncio.wait(
                tuple(self._inflight), timeout=self.drain_timeout_seconds)
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)

        # 停止所有渠道
        for channel in self.channels:
            try:
                await channel.stop()
            except Exception as e:
                print(f"警告: 渠道 '{channel.name}' 关闭失败 - {e}")

        # 清空 Agent 缓存
        self._agents.clear()
        self._session_locks.clear()
