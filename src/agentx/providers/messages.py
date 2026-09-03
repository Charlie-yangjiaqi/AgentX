"""Provider 层的消息与工具类型。

这些类型是 Agent Runtime 与模型 API 之间的"对话语言"，
与 state.models 的持久化消息分开：state 记录事实，这里描述对话。
"""

from __future__ import annotations

from typing import Any

from pydantic import Field

from agentx.state.models import AgentXModel


class ToolCall(AgentXModel):
    """模型请求调用某个 Tool。"""

    id: str
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)


class ToolSpec(AgentXModel):
    """暴露给模型的 Tool 描述（JSON Schema 形式）。"""

    name: str
    description: str
    parameters: dict[str, Any] = Field(default_factory=dict)


class ChatMessage(AgentXModel):
    """对话消息：system / user / assistant / tool。"""

    role: str
    content: str | None = None
    tool_calls: list[ToolCall] | None = None
    tool_call_id: str | None = None


class Usage(AgentXModel):
    prompt_tokens: int = 0
    completion_tokens: int = 0

    @property
    def total(self) -> int:
        return self.prompt_tokens + self.completion_tokens


class ModelResponse(AgentXModel):
    """模型单次回复：文本内容 + 工具调用请求。"""

    content: str | None = None
    tool_calls: list[ToolCall] = Field(default_factory=list)
    usage: Usage = Field(default_factory=Usage)

    @property
    def has_tool_calls(self) -> bool:
        return bool(self.tool_calls)
