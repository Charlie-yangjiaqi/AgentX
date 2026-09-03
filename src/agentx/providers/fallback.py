"""Fallback Provider：主模型失败后切换备用 Provider。"""

from __future__ import annotations

from typing import Any, cast

from agentx.providers.messages import ChatMessage, ModelResponse, ToolSpec


class FallbackProvider:
    """包装主/备 Provider：主失败（重试后仍失败）→ 备用 → 结构化错误。"""

    def __init__(self, primary: Any, fallback: Any) -> None:
        self.name = f"{primary.name}+{fallback.name}"
        self.primary = primary
        self.fallback = fallback

    async def chat(
        self,
        messages: list[ChatMessage],
        model: str,
        tools: list[ToolSpec] | None = None,
        **options: object,
    ) -> ModelResponse:
        try:
            return cast(
                ModelResponse,
                await self.primary.chat(messages, model=model, tools=tools, **options),
            )
        except Exception as primary_error:
            try:
                return cast(
                    ModelResponse,
                    await self.fallback.chat(messages, model=model, tools=tools, **options),
                )
            except Exception as fallback_error:
                raise RuntimeError(
                    f"模型调用失败：主 Provider({self.primary.name}) → "
                    f"{primary_error}; 备用 Provider({self.fallback.name}) → {fallback_error}"
                ) from fallback_error
