"""Reasonix Provider：使用宿主 Reasonix 的模型（模式 A）。

通过 MCP sampling 通道请求宿主生成，AgentX 不持有 API Key。
宿主未提供采样能力时返回结构化错误，提示改用 model_source=agentx。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from agentx.providers.messages import ChatMessage, ModelResponse, ToolSpec

SamplingHandler = Callable[..., Awaitable[ModelResponse] | ModelResponse]


class ReasonixProvider:
    """宿主模型 Provider：依赖 MCP sampling 通道。"""

    name = "reasonix"

    def __init__(self, sampling_handler: SamplingHandler | None = None) -> None:
        self._sampling_handler = sampling_handler

    async def chat(
        self,
        messages: list[ChatMessage],
        model: str,
        tools: list[ToolSpec] | None = None,
        **options: object,
    ) -> ModelResponse:
        if self._sampling_handler is None:
            raise RuntimeError(
                "Reasonix 宿主模型不可用：宿主未提供 MCP sampling 能力。"
                "请配置 model_source=agentx（AgentX 自有 API），"
                "或等宿主支持采样后重试。"
            )
        result = self._sampling_handler(messages=messages, model=model, tools=tools)
        if isinstance(result, Awaitable):
            return await result
        return result
