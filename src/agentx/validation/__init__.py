"""Phase 7.9 Evidence Validation Layer。

解决"为什么修改这里"：Plan 的每个修改点必须绑定工程事实才能输出。

- evidence.py：确定性证据收集（零 LLM，只允许来自 Index）
- validator.py：规则判定 PASS / WARNING / BLOCK + 跨模块传播链（联合图 BFS≤2）
- 与 7.8 关系：7.8 决定"改哪里"（用户选择），7.9 验证"为什么改这里"（方案必须有据）
"""

from agentx.validation.evidence import ChangeEvidence, collect_change_evidence
from agentx.validation.validator import (
    DEFAULT_RULES,
    ValidationResult,
    build_impact_graph,
    validate_plan,
)

__all__ = [
    "ChangeEvidence",
    "ValidationResult",
    "DEFAULT_RULES",
    "build_impact_graph",
    "collect_change_evidence",
    "validate_plan",
]
