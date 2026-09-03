"""Tool 注册表：注册、按权限门禁、执行并发布事件。

所有高风险 Tool 都必须产生 ToolStarted / ToolFinished 事件，
并允许返回 requires_approval=True 的结果，由上层交给用户审批。
"""

from __future__ import annotations

from typing import Any

from agentx.core.event_bus import EventBus, make_event
from agentx.state.models import (
    EVENT_TOOL_FINISHED,
    EVENT_TOOL_STARTED,
)
from agentx.tools.base import (
    FunctionTool,
    Permission,
    ToolContext,
    ToolResult,
)


class ToolRegistry:
    def __init__(self, event_bus: EventBus | None = None) -> None:
        self._tools: dict[str, FunctionTool] = {}
        self._event_bus = event_bus

    def register(self, tool: FunctionTool) -> FunctionTool:
        if tool.name in self._tools:
            raise ValueError(f"Tool 重复注册: {tool.name}")
        self._tools[tool.name] = tool
        return tool

    def get(self, name: str) -> FunctionTool:
        if name not in self._tools:
            raise KeyError(f"Tool 未注册: {name}")
        return self._tools[name]

    def names(self) -> list[str]:
        return sorted(self._tools)

    def specs(self) -> list[Any]:
        return [t.to_spec() for t in self._tools.values()]

    def can_execute(self, name: str, permissions: frozenset[Permission]) -> bool:
        try:
            tool = self.get(name)
        except KeyError:
            return False
        return tool.permission in permissions

    async def execute(
        self,
        name: str,
        args: dict[str, Any],
        ctx: ToolContext,
        permissions: frozenset[Permission],
        *,
        task_id: str | None = None,
        agent_id: str | None = None,
    ) -> ToolResult:
        """执行 Tool：先过权限门禁，再发布开始/结束事件。"""
        tool = self.get(name)
        if tool.permission not in permissions:
            return ToolResult(
                ok=False,
                error=(
                    f"权限不足: Agent({agent_id}) 无 {tool.permission.value} 权限，无法调用 {name}"
                ),
            )

        if self._event_bus is not None:
            await self._event_bus.publish(
                make_event(
                    EVENT_TOOL_STARTED,
                    task_id=task_id,
                    agent_id=agent_id,
                    payload={"tool": name, "args": args},
                )
            )

        result = await tool.execute(args, ctx)
        result.args = args

        if self._event_bus is not None:
            await self._event_bus.publish(
                make_event(
                    EVENT_TOOL_FINISHED,
                    task_id=task_id,
                    agent_id=agent_id,
                    payload={
                        "tool": name,
                        "ok": result.ok,
                        "exit_code": result.exit_code,
                        "requires_approval": result.requires_approval,
                        "output": (result.output or result.error or "")[:500],
                        "args": args,
                    },
                )
            )
        return result
