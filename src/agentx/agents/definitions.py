"""Agent 定义：角色 ≠ 模型。

AgentDefinition 是配置（身份、角色、模型引用、Prompt、工具、权限），
AgentRuntime 是运行实例。角色与模型解耦：换模型不碰业务核心。
"""

from __future__ import annotations

from agentx.state.models import AgentRole, AgentXModel
from agentx.tools.base import ROLE_PERMISSIONS, Permission


class AgentDefinition(AgentXModel):
    id: str
    role: AgentRole
    provider_ref: str = "default"
    model: str | None = None
    system_prompt: str
    permissions: frozenset[Permission] = frozenset()
    max_steps: int = 12

    @classmethod
    def from_role(
        cls, agent_id: str, role: AgentRole, system_prompt: str, model: str | None = None
    ) -> AgentDefinition:
        """按角色创建默认 Agent：权限取自 ROLE_PERMISSIONS。"""
        return cls(
            id=agent_id,
            role=role,
            system_prompt=system_prompt,
            model=model,
            permissions=ROLE_PERMISSIONS[role.value],
        )
