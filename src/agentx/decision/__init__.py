"""Phase 7.8 Human Decision Boundary Layer（决策保护层）。

AI 提供分析依据，用户拥有最终选择权：
- candidate_analyzer：需求 → 修改候选（规则化，Fact/Inference 分离）
- decision_gate：候选 → 人工确认判定（多候选/分差/公共接口/大影响/跨层）
- 用户选择（candidate_id 精确锚定）→ Plan 仅围绕所选目标

原则（禁止事项）：
- 禁止 AI 自动选择修改目标（不生成"应该修改 X"）
- 禁止把 responsibility 当事实（只允许 inference）
- confidence = 证据强度（规则计算），不是 AI 自信
- 用户未确认不生成修改方案（Gate 拦截时不调 LLM）
"""

from __future__ import annotations

from agentx.decision.analyzer import analyze_candidates
from agentx.decision.gate import (
    DEFAULT_THRESHOLDS,
    GateVerdict,
    evaluate_gate,
)

__all__ = [
    "analyze_candidates",
    "evaluate_gate",
    "GateVerdict",
    "DEFAULT_THRESHOLDS",
]
