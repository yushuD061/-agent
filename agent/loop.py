"""Agent 主循环模块"""

import json
import uuid

from providers.base import LLMProvider, LLMResponse
from agent.tools.registry import ToolRegistry
from agent.context import ContextBuilder
from session.manager import SessionManager
from privacy import safe_print as print
from agent.workflow import WorkflowService
from agent.memory_runtime.models import (
    ToolResultEvent, TurnAbortedEvent, TurnCompletedEvent,
)

class AgentLoop:
    """Agent 执行循环

    管理 LLM 与工具之间的交互循环，包含防爆检测机制。
    """

    # 防爆阈值常量
    FUSE_THRESHOLD = 20  # 熔断阈值
    WARNING_THRESHOLD = 10  # 警告阈值
    WINDOW_SIZE = 30  # 滑动窗口大小

    def __init__(
        self,
        provider: LLMProvider,
        tools: ToolRegistry,
        context: ContextBuilder,
        session_manager: SessionManager,
        model: str | None = None,
        max_iterations: int = 32,
        session_key: str = "cli:direct",
        workflow_service: WorkflowService | None = None,
        memory_lifecycle=None,
        memory_request_factory=None,
        memory_command_router=None,
    ):
        """初始化 AgentLoop

        Args:
            provider: LLM 提供方实例
            tools: 工具注册表
            context: 上下文构建器
            session_manager: 会话管理器
            model: 模型名称（可选）
            max_iterations: 最大迭代次数
            session_key: 会话标识符，默认 "cli:direct"
        """
        self.provider = provider
        self.tools = tools
        self.context = context
        self.session_manager = session_manager
        self.session_key = session_key
        self.model = model
        self.max_iterations = max_iterations
        self.workflow_service = workflow_service
        self.memory_lifecycle = memory_lifecycle
        self.memory_request_factory = memory_request_factory
        self.memory_command_router = memory_command_router
        self.consolidator = None  # 在整合压缩器之前先设为 None
        self._request_context: dict = {}

        # 内部状态
        self._tool_call_history: list[str] = []  # 滑动窗口
        # 从会话管理器恢复历史
        self._session_history: list[dict] = session_manager.get_history(session_key)

    def set_request_context(self, context: dict | None) -> None:
        """Accept trusted per-request metadata supplied by the server channel."""
        self._request_context = dict(context or {})

    async def run(self, user_message: str) -> str:
        request_id = str(self._request_context.get("request_id") or uuid.uuid4())
        prepared = None
        try:
            if self.memory_command_router is not None and user_message.startswith("/memory"):
                response = await self.memory_command_router.execute(user_message)
                self.session_manager.save_message(
                    self.session_key, {"role": "user", "content": user_message}
                )
                self._persist_final_response(response)
                return response
            if self.memory_lifecycle is not None and self.memory_request_factory is not None:
                request = self.memory_request_factory(
                    self.session_key, request_id, user_message,
                    tuple(self._session_history), dict(self._request_context),
                )
                prepared = await self.memory_lifecycle.prepare_turn(request)
                self._session_history = list(prepared.history)
            response = await self._run_turn(user_message, prepared, request_id)
            if self.memory_lifecycle is not None:
                outcome = await self.memory_lifecycle.complete_turn(
                    TurnCompletedEvent(request_id, response, f"{request_id}:assistant")
                )
                notice = getattr(outcome, "notice", "") if outcome is not None else ""
                if notice:
                    response += notice
            self._persist_final_response(response)
            return response
        except Exception as exc:
            if self.memory_lifecycle is not None:
                await self.memory_lifecycle.abort_turn(
                    TurnAbortedEvent(request_id, type(exc).__name__)
                )
            raise

    async def _run_turn(self, user_message: str, prepared_memory=None, request_id: str = "") -> str:
        """执行 Agent 主循环

        Args:
            user_message: 用户输入消息

        Returns:
            str: Agent 的最终响应
        """
        # 1. 构建初始 messages
        messages = self.context.build_messages(
            history=self._session_history,
            current_message=user_message,
            request_context=self._request_context,
            prepared_memory=prepared_memory,
        )

        # 保存用户消息
        user_msg = {"role": "user", "content": user_message}
        self.session_manager.save_message(self.session_key, user_msg)

        # 2. 循环处理
        for iteration in range(self.max_iterations):
            if hasattr(self, 'consolidator') and self.consolidator:
                messages = await self.consolidator.maybe_consolidate(messages)
            # a. 调用 LLM（打印思考提示）
            print(f"  💭 思考中... (第 {iteration + 1} 轮)", end="", flush=True)    
            response = await self.provider.chat(
                messages=messages,
                tools=self.tools.get_definitions(),
                model=self.model
            )
            print(" ✓")  # 思考完成

            if response.has_tool_calls and not self._tool_calls_allowed(response):
                final_content = self._disallowed_tool_response()
                self._save_to_history(messages)
                return final_content

            # 打印 reasoning_content（思考过程）
            if response.tool_calls and response.tool_calls[0].reasoning_content:
                reasoning = response.tool_calls[0].reasoning_content
                # 截断显示，避免刷屏
                preview = reasoning[:300] + "..." if len(reasoning) > 300 else reasoning
                print(f"  🧠 思考过程:\n{preview}")

            # b. 检查错误
            if response.finish_reason == "error":
                return self._finalize_response("抱歉，发生了错误，请稍后重试。")

            # c. 处理工具调用
            if response.has_tool_calls:
                # 构造 assistant 消息（含 tool_calls）
                assistant_msg = self._build_assistant_message(response)
                messages.append(assistant_msg)

                # 保存 assistant 消息（含 tool_calls）
                self.session_manager.save_message(self.session_key, assistant_msg)

                # 遍历每个 tool_call
                for tc in response.tool_calls:
                    # 获取参数 JSON 字符串
                    args_json = json.dumps(tc.arguments, ensure_ascii=False)

                    # ★ 打印工具调用过程，让用户看到 Agent 在干什么
                    print(f"\n  🛠️  调用工具: {tc.name}({args_json})")

                    # 防爆检测
                    check_result = self._check_tool_loop(tc.name, args_json)

                    if check_result and "熔断" in check_result:
                        # 熔断 - 直接返回
                        print(f"  🚨 {check_result}")
                        return check_result
                    elif check_result and "警告" in check_result:
                        # 警告 - append SYSTEM_ERROR 消息，skip 执行
                        print(f"  ⚠️  {check_result}")
                        tool_msg = {
                            "role": "tool",
                            "tool_call_id": tc.id,
                            "content": f"系统警告：{check_result}"
                        }
                        messages.append(tool_msg)
                        self.session_manager.save_message(self.session_key, tool_msg)
                        if self.memory_lifecycle is not None:
                            await self.memory_lifecycle.observe_tool_result(
                                ToolResultEvent(
                                    request_id, tc.name, tool_msg["content"], tc.id,
                                    f"{request_id}:tool:{tc.id}",
                                )
                            )
                    else:
                        # 防爆通过 - 执行工具
                        result = await self.tools.execute(tc.name, tc.arguments)
                        if self.workflow_service is not None:
                            bare_name = tc.name.rsplit("__", 1)[-1]
                            if bare_name == "query_trade_data":
                                await self.workflow_service.observe_analytics(self.session_key, result)
                            else:
                                await self.workflow_service.observe_tool(self.session_key, tc.name, result)
                        # 打印工具结果（截断显示，避免刷屏）
                        preview = result[:200] + "..." if len(result) > 200 else result
                        print(f"  ✅ 结果: {preview}")
                        tool_msg = {
                            "role": "tool",
                            "tool_call_id": tc.id,
                            "content": result
                        }
                        messages.append(tool_msg)
                        self.session_manager.save_message(self.session_key, tool_msg)
                        if self.memory_lifecycle is not None:
                            await self.memory_lifecycle.observe_tool_result(
                                ToolResultEvent(
                                    request_id, tc.name, result, tc.id,
                                    f"{request_id}:tool:{tc.id}",
                                )
                            )

                # continue 回到循环顶部
                continue

            # d. 没有工具调用 - 保存历史并返回
            self._save_to_history(messages)

            final_content = self._finalize_response(response.content or "")
            return final_content

        # 3. for-else: 超过最大次数
        return "已达到最大迭代次数，任务未完成。"

    def _persist_final_response(self, content: str) -> None:
        """Persist the final assistant response exactly once after memory completion."""
        if not content:
            return
        message = {"role": "assistant", "content": content}
        self.session_manager.save_message(self.session_key, message)
        self._session_history.append(message)

    def _finalize_response(self, content: str) -> str:
        """Apply channel-specific response policy before persistence and return."""
        return content

    def _tool_calls_allowed(self, response: LLMResponse) -> bool:
        del response
        return True

    def _disallowed_tool_response(self) -> str:
        return self._finalize_response("Tool calls are not available for this agent.")

    def _build_assistant_message(self, response: LLMResponse) -> dict:
        """构建 assistant 消息（含 tool_calls）

        严格按照 OpenAI 格式构建，reasoning_content 放在顶层。

        Args:
            response: LLM 响应

        Returns:
            dict: assistant 消息字典
        """
        assistant_msg: dict = {
            "role": "assistant",
            "content": response.content
        }

        # 构建 tool_calls 数组
        tool_calls = []
        for tc in response.tool_calls:
            tool_call_item = {
                "id": tc.id,
                "type": "function",
                "function": {
                    "name": tc.name,
                    "arguments": json.dumps(tc.arguments, ensure_ascii=False)
                }
            }
            tool_calls.append(tool_call_item)

        if tool_calls:
            assistant_msg["tool_calls"] = tool_calls

        # reasoning_content 放在顶层（不能放在 tool_calls 里）
        if response.tool_calls and response.tool_calls[0].reasoning_content:
            assistant_msg["reasoning_content"] = response.tool_calls[0].reasoning_content

        return assistant_msg

    def _check_tool_loop(self, tool_name: str, tool_args_json: str) -> str | None:
        """防爆检测

        检测工具调用的重复频率，防止无限循环。

        Args:
            tool_name: 工具名称
            tool_args_json: 工具参数的 JSON 字符串

        Returns:
            str | None: 熔断/警告消息，或 None 表示放行
        """
        signature = f"{tool_name}:{tool_args_json}"

        # 统计签名出现次数
        count = self._tool_call_history.count(signature)

        if count >= self.FUSE_THRESHOLD:
            return f"检测到工具 '{tool_name}' 重复调用已达熔断阈值（{self.FUSE_THRESHOLD}次），已自动终止执行。请尝试其他方案。"
        elif count >= self.WARNING_THRESHOLD:
            return f"警告：工具 '{tool_name}' 重复调用已达 {self.WARNING_THRESHOLD} 次，请检查是否有循环调用问题。"

        # 放行 - 添加到历史
        self._tool_call_history.append(signature)

        # 维护滑动窗口大小
        if len(self._tool_call_history) > self.WINDOW_SIZE:
            self._tool_call_history.pop(0)

        return None

    def _save_to_history(self, messages_snapshot: list[dict]) -> None:
        """保存本轮新增消息到会话历史

        注意：消息已通过 session_manager.save_message 持久化，
        此方法仅更新内存中的 _session_history，供下次对话使用。

        Args:
            messages_snapshot: 当前完整的消息列表
        """
        # 计算新增消息（跳过 system 消息）
        # 新消息数量 = 总消息数 - 已有历史消息数 - 1 (system)
        existing_count = len(self._session_history)

        # 新增的消息（不包含 system）
        new_messages = messages_snapshot[1 + existing_count:]

        # 添加到历史（仅更新内存状态）
        self._session_history.extend(new_messages)

    def clear_history(self) -> None:
        """清空对话历史和工具调用历史"""
        self._tool_call_history.clear()
        self._session_history.clear()
        # 清空会话文件
        self.session_manager.clear(self.session_key)
