"""V1 默认 Tool 集。"""

from __future__ import annotations

from agentx.core.event_bus import EventBus
from agentx.tools.base import FunctionTool as FunctionTool
from agentx.tools.fs import build_fs_tools
from agentx.tools.git import build_git_tools
from agentx.tools.project import build_project_tools
from agentx.tools.registry import ToolRegistry
from agentx.tools.shell import build_shell_tools
from agentx.tools.test import build_test_tools


def build_default_registry(event_bus: EventBus | None = None) -> ToolRegistry:
    """构建 V1 默认 Tool 注册表。"""
    registry = ToolRegistry(event_bus=event_bus)
    for tool in (
        *build_fs_tools(),
        *build_shell_tools(),
        *build_git_tools(),
        *build_test_tools(),
        *build_project_tools(),
    ):
        registry.register(tool)
    return registry
