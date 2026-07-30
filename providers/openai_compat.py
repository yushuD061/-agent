"""
OpenAI 兼容 Provider 实现

兼容 OpenAI API 格式的各种服务商，包括：
- OpenAI 官方 API
- 硅基流动 (SiliconFlow)
- Kimi (Moonshot)
- DeepSeek
- 其他 OpenAI 兼容的第三方服务

通过统一接口封装，自动处理工具调用参数、推理内容提取和异常容错。
"""

import json
from typing import Any

from openai import AsyncOpenAI

from providers.base import LLMProvider, LLMResponse, ToolCallRequest
from privacy import sanitize_text


class OpenAICompatProvider(LLMProvider):
    """
    OpenAI 兼容 API Provider

    支持所有 OpenAI API 格式的服务，通过 base_url 切换不同提供商。
    内置异常容错，API 调用失败时返回错误响应而非抛出异常。

    关键特性：
    - 仅在 tools 参数存在时传递 tool_choice，避免部分 API 报错
    - 自动提取 reasoning_content（支持 Kimi-K2.5、DeepSeek-R1 等思考模型）
    - 自动解析 tool_calls 的 arguments JSON

    使用示例：
        # OpenAI 官方
        provider = OpenAICompatProvider(
            api_key="sk-xxx",
            base_url="https://api.openai.com/v1",
            model="gpt-4"
        )

        # 硅基流动
        provider = OpenAICompatProvider(
            api_key="sk-xxx",
            base_url="https://api.siliconflow.cn/v1",
            model="Qwen/Qwen2.5-72B-Instruct"
        )

        # DeepSeek
        provider = OpenAICompatProvider(
            api_key="sk-xxx",
            base_url="https://api.deepseek.com/v1",
            model="deepseek-chat"
        )
    """

    def __init__(
        self,
        api_key: str,
        base_url: str,
        model: str,
    ) -> None:
        """
        Args:
            api_key: API 密钥
            base_url: API 基础 URL，不同服务商地址不同
            model: 默认使用的模型名称
        """
        self.api_key = api_key
        self.base_url = base_url
        self.model = model
        self.client = AsyncOpenAI(api_key=api_key, base_url=base_url)

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
    ) -> LLMResponse:
        """
        发送对话请求

        动态构建请求参数：仅在 tools 存在时传递 tool_choice，
        避免硅基流动等 API 报 "tools must be specified when tool_choice is utilized" 错误。
        """
        try:
            # 动态构建请求参数
            request_params: dict[str, Any] = {
                "model": model or self.model,
                "messages": messages,
            }

            # 仅在 tools 存在时添加工具相关参数
            if tools:
                request_params["tools"] = tools
                request_params["tool_choice"] = "auto"

            # 调用 API
            response = await self.client.chat.completions.create(**request_params)

            # 解析响应
            message = response.choices[0].message
            finish_reason = response.choices[0].finish_reason or "stop"

            # 提取 usage
            usage: dict[str, Any] = {}
            if response.usage:
                usage = {
                    "prompt_tokens": response.usage.prompt_tokens,
                    "completion_tokens": response.usage.completion_tokens,
                    "total_tokens": response.usage.total_tokens,
                }

            # 获取 message 顶层的 reasoning_content（部分模型放在这里）
            message_reasoning = getattr(message, "reasoning_content", None)

            # 解析 tool_calls
            tool_calls: list[ToolCallRequest] = []
            if message.tool_calls:
                for tc in message.tool_calls:
                    # 解析 arguments JSON
                    try:
                        arguments = json.loads(tc.function.arguments)
                    except json.JSONDecodeError:
                        arguments = {"raw": tc.function.arguments}

                    # 获取 tool_call 级别的 reasoning_content
                    tc_reasoning = getattr(tc, "reasoning_content", None)

                    # 合并 reasoning：优先 tool_call 级别，其次 message 级别
                    reasoning_content = tc_reasoning or message_reasoning

                    tool_calls.append(
                        ToolCallRequest(
                            id=tc.id,
                            name=tc.function.name,
                            arguments=arguments,
                            reasoning_content=reasoning_content,
                        )
                    )

            return LLMResponse(
                content=message.content,
                tool_calls=tool_calls,
                finish_reason=finish_reason,
                usage=usage,
            )

        except Exception as e:
            # 异常容错：返回错误响应而非抛出异常
            return LLMResponse(
                content=sanitize_text(f"错误: API 调用失败 - {e}"),
                finish_reason="error",
            )
