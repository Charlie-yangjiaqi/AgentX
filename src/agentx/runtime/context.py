"""RuntimeContext：解释 AgentX 的 Index 决策，不改变状态机本身。

决策规则（顺序固定，force 优先级最高，避免 VALID+force→reuse 冲突）：

    force_rebuild            → rebuild_index ("user requested rebuild")
    VALID                    → reuse_index   ("fingerprint matched existing index")
    STALE                    → sync_index    ("project changed since index")
    MISSING / CORRUPTED      → rebuild_index ("index missing / corrupted")

原则：VALID 默认复用，不允许隐式 rebuild；force_rebuild 是用户显式意图。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from agentx.index.index import IndexStatus

# 决策动作常量
ACTION_REUSE = "reuse_index"
ACTION_SYNC = "sync_index"
ACTION_REBUILD = "rebuild_index"

REASONS: dict[str, str] = {
    ACTION_REUSE: "fingerprint matched existing index",
    ACTION_SYNC: "project changed since index",
    ACTION_REBUILD: "user requested rebuild",
}


def decide_index_action(
    index_state: str,
    fingerprint_result: dict[str, Any] | None = None,
    force_rebuild: bool = False,
) -> dict[str, str]:
    """解释 AgentX 为什么选择 reuse / sync / rebuild。

    输入：
        index_state        ：VALID / STALE / MISSING / CORRUPTED
        fingerprint_result ：index_status() 的 reason 等附加信息（可选，用于 reason 补充）
        force_rebuild      ：用户显式意图，优先级最高
    输出：
        {"action": ..., "reason": ...}
    """
    state = index_state.upper()
    if force_rebuild:
        return {
            "action": ACTION_REBUILD,
            "reason": "user requested rebuild",
        }
    if state == IndexStatus.VALID.value.upper():
        return {
            "action": ACTION_REUSE,
            "reason": REASONS[ACTION_REUSE],
        }
    if state == IndexStatus.STALE.value.upper():
        return {
            "action": ACTION_SYNC,
            "reason": REASONS[ACTION_SYNC],
        }
    if state in {IndexStatus.MISSING.value.upper(), IndexStatus.CORRUPTED.value.upper()}:
        return {
            "action": ACTION_REBUILD,
            "reason": "index missing / corrupted",
        }
    return {
        "action": ACTION_REBUILD,
        "reason": f"unknown index state: {index_state}",
    }


@dataclass
class RuntimeContext:
    """统一描述当前 AgentX 状态（解释层，不改状态机）。"""

    index_state: str
    fingerprint: str = ""
    decision: dict[str, str] = field(default_factory=dict)
    workflow: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "index_state": self.index_state,
            "fingerprint": self.fingerprint,
        }
        if self.decision:
            out["decision"] = self.decision
        if self.workflow:
            out["workflow"] = self.workflow
        return out


def build_runtime_context(
    index_state: str,
    fingerprint: str = "",
    force_rebuild: bool = False,
    workflow_action: str = "",
    workflow_stage: str = "idle",
    fingerprint_result: dict[str, Any] | None = None,
) -> RuntimeContext:
    """组装 RuntimeContext：Index 状态 + 指纹 + 决策解释 + workflow 阶段。"""
    decision = decide_index_action(index_state, fingerprint_result, force_rebuild)
    workflow: dict[str, str] = {}
    if workflow_action:
        workflow["action"] = workflow_action
    workflow["stage"] = workflow_stage
    return RuntimeContext(
        index_state=index_state,
        fingerprint=fingerprint,
        decision=decision,
        workflow=workflow,
    )
