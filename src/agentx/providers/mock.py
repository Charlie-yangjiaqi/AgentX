"""MockProvider：离线开发与测试用。

按脚本顺序返回预设回复；没有脚本时返回固定文本。
用于验证 Agent Runtime / Orchestrator 的数据流，不依赖真实 API。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from agentx.providers.messages import ChatMessage, ModelResponse, ToolSpec, Usage

MockHandler = Callable[[list[ChatMessage]], Awaitable[ModelResponse] | ModelResponse]


class MockProvider:
    """脚本化 Mock：可预设回复序列，或提供自定义 handler。"""

    def __init__(self, name: str = "mock") -> None:
        self.name = name
        self._script: list[ModelResponse] = []
        self._handler: MockHandler | None = None
        self.calls: list[list[ChatMessage]] = []

    def respond(self, *responses: ModelResponse) -> MockProvider:
        """追加预设回复，按调用顺序依次消费。"""
        self._script.extend(responses)
        return self

    def with_handler(self, handler: MockHandler) -> MockProvider:
        """自定义 handler：接收完整消息列表，返回回复。"""
        self._handler = handler
        return self

    async def chat(
        self,
        messages: list[ChatMessage],
        model: str,
        tools: list[ToolSpec] | None = None,
        **options: object,
    ) -> ModelResponse:
        self.calls.append(list(messages))
        if self._handler is not None:
            result = self._handler(messages)
            if isinstance(result, Awaitable):
                return await result
            return result
        if self._script:
            return self._script.pop(0)
        last = messages[-1] if messages else None
        return ModelResponse(content=f"[mock:{self.name}] 收到 {len(messages)} 条消息: {last}")


def text_response(
    content: str, prompt_tokens: int = 0, completion_tokens: int = 0
) -> ModelResponse:
    return ModelResponse(
        content=content,
        usage=Usage(prompt_tokens=prompt_tokens, completion_tokens=completion_tokens),
    )
