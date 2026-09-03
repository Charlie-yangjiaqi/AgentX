"""Tool 协议与权限模型。

原则：Agent 不直接碰操作系统，一切能力通过 Tool 接口申请。
System Prompt 是行为约束；Tool Permission 才是能力约束。
"""

from __future__ import annotations

import enum
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from pydantic import Field

from agentx.providers.messages import ToolSpec
from agentx.state.models import AgentXModel


class Permission(enum.StrEnum):
    """能力权限：Agent 的权限集合决定能调用哪些 Tool。"""

    READ = "read"
    WRITE = "write"
    GIT_READ = "git_read"
    GIT = "git"
    SHELL = "shell"
    TEST = "test"


class ToolResult(AgentXModel):
    """Tool 执行结果：ok + output / error，exit_code 作为事实证据。"""

    ok: bool = True
    output: str | None = None
    exit_code: int | None = None
    error: str | None = None
    requires_approval: bool = False
    args: dict[str, Any] = Field(default_factory=dict)

    @property
    def summary(self) -> str:
        if not self.ok:
            return f"失败: {self.error}"
        if self.exit_code is not None:
            return f"exit={self.exit_code}"
        return "ok"


class ToolContext(AgentXModel):
    """Tool 执行上下文：项目根目录 + 超时。"""

    project_root: Path
    timeout: float = 120.0


ToolFunc = Callable[[dict[str, Any], ToolContext], Awaitable[ToolResult] | ToolResult]


class FunctionTool:
    """最小 Tool 实现：名称 + 描述 + 权限 + JSON Schema + 异步函数。"""

    def __init__(
        self,
        name: str,
        description: str,
        permission: Permission,
        parameters: dict[str, Any],
        func: ToolFunc,
    ) -> None:
        self.name = name
        self.description = description
        self.permission = permission
        self.parameters = parameters
        self._func = func

    def to_spec(self) -> ToolSpec:
        return ToolSpec(
            name=self.name,
            description=self.description,
            parameters=self.parameters,
        )

    async def execute(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        result = self._func(args, ctx)
        if isinstance(result, Awaitable):
            return await result
        return result


# 角色 → 权限集合（V1 默认值，来自蓝图）
ROLE_PERMISSIONS: dict[str, frozenset[Permission]] = {
    "executor": frozenset(
        {
            Permission.READ,
            Permission.WRITE,
            Permission.GIT_READ,
            Permission.GIT,
            Permission.SHELL,
            Permission.TEST,
        }
    ),
    "reviewer": frozenset({Permission.READ, Permission.GIT_READ}),
    "verifier": frozenset(
        {Permission.READ, Permission.GIT_READ, Permission.SHELL, Permission.TEST}
    ),
    "plan": frozenset({Permission.READ, Permission.GIT_READ, Permission.SHELL}),
}


def resolve_safe_path(root: Path, target: str) -> Path:
    """把相对路径解析到项目根内；越界则抛 ValueError。"""
    root_resolved = root.resolve()
    candidate = (root / target).resolve()
    if candidate != root_resolved and not candidate.is_relative_to(root_resolved):
        raise ValueError(f"路径越出项目根目录: {target}")
    return candidate
