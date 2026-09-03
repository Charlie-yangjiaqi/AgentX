"""ModelProvider 协议：Agent Runtime 与模型 API 的唯一接口。

原则：模型供应商细节不允许泄漏到 Orchestrator / Agent 层。
并发、重试、超时、限流都由 Provider 实现统一处理。
"""

from __future__ import annotations

from typing import Protocol

from agentx.providers.messages import ChatMessage, ModelResponse, ToolSpec


class ModelProvider(Protocol):
    """统一的模型调用接口。"""

    name: str

    async def chat(
        self,
        messages: list[ChatMessage],
        model: str,
        tools: list[ToolSpec] | None = None,
        **options: object,
    ) -> ModelResponse:
        """发起一次对话，返回文本或工具调用请求。"""
        ...
