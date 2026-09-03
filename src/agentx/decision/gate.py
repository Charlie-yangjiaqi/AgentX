"""Phase 7.8 Decision Gate：修改候选的决策边界判定（规则化）。

触发人工确认（任一满足）：
1. 候选数量 >= 2
2. 最高与第二候选分数差 < score_gap（默认 0.2）
3. 修改公共接口：header / struct / enum / 被 ≥public_consumer_threshold 个
   消费者引用的符号（API）
4. 影响模块数 > large_impact_threshold（默认 5）
5. 跨层修改：目标模块与波及模块跨越 ≥cross_module_type_threshold 个
   module.type（app/driver/bsp/middleware/hal/lib/unknown，不绑定目录名）

放行条件：唯一候选 且 HIGH 且无上述任何条件 → 直接进入 Plan。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from agentx.index.index import ProjectIndex

# 阈值默认值（可配置；误拦截比误放行安全）
DEFAULT_THRESHOLDS: dict[str, float] = {
    "candidate_score_gap": 0.2,
    "public_consumer_threshold": 3,
    "cross_module_type_threshold": 2,
    "large_impact_threshold": 5,
}


@dataclass
class GateVerdict:
    confirm: bool  # True=需要用户确认（decision_required）
    reasons: list[str]  # 触发原因（可展示）
    selected: dict[str, Any] | None = None  # 放行时的唯一候选


def _target_is_public(
    candidate: dict[str, Any],
    index: ProjectIndex,
    consumer_threshold: int,
) -> tuple[bool, str]:
    """公共接口判定：header / struct / enum / API（多消费者符号）。"""
    target = str(candidate.get("target", ""))
    # 命中文件是否头文件
    for f in index.files:
        if f.path.endswith(".h") and (target in f.path or f.path.endswith(f"{target}.h")):
            return True, f"涉及头文件 {f.path}"
    # 命中 struct/enum（type_semantics）
    ts = index.type_semantics or {}
    for st in ts.get("structs", []):
        if st.get("name") == target:
            return True, f"修改结构体 {target}（公共数据模型）"
    for en in ts.get("enums", []):
        if en.get("name") == target:
            return True, f"修改枚举 {target}（公共数据模型）"
    # 被多消费者引用的符号（API）
    mod = next((m for m in index.modules if str(m.get("name", "")) == target), None)
    if mod is not None and len(mod.get("consumers", []) or []) >= consumer_threshold:
        return True, f"{target} 被 {len(mod['consumers'])} 个模块消费（公共 API）"
    return False, ""


def _module_types(candidate: dict[str, Any], index: ProjectIndex) -> tuple[str, set[str]]:
    """(目标模块 type, 波及模块 type 集合)。"""
    mod = next(
        (m for m in index.modules if str(m.get("name", "")) == candidate.get("target")), None
    )
    target_type = str(mod.get("type", "unknown") or "unknown") if mod else "unknown"
    impacted: set[str] = set()
    for name in candidate.get("impact_scope", {}).get("module_names", []) or []:
        m = next((x for x in index.modules if str(x.get("name", "")) == name), None)
        if m is not None:
            impacted.add(str(m.get("type", "unknown") or "unknown"))
    return target_type, impacted


def evaluate_gate(
    candidates: list[dict[str, Any]],
    index: ProjectIndex,
    thresholds: dict[str, float] | None = None,
) -> GateVerdict:
    """Decision Gate 判定：返回 GateVerdict（confirm + reasons）。"""
    th = {**DEFAULT_THRESHOLDS, **(thresholds or {})}
    reasons: list[str] = []

    if not candidates:
        # 无候选：不拦截（无证据可言，交给 LLM 空检索兜底）；
        # 但低证据任务不允许生成修改方案由 confidence 层保证
        return GateVerdict(confirm=False, reasons=[])

    if len(candidates) >= 2:
        reasons.append(f"存在 {len(candidates)} 个候选修改位置")
    if len(candidates) >= 2:
        top = candidates[0]["score"]
        second = candidates[1]["score"]
        if top - second < float(th["candidate_score_gap"]):
            reasons.append(
                f"候选分数接近（{top:.2f} vs {second:.2f}，差 < {th['candidate_score_gap']}）"
            )
    for c in candidates:
        public, why = _target_is_public(c, index, int(th["public_consumer_threshold"]))
        if public:
            reasons.append(why)
            break
    top_impact = candidates[0].get("impact_scope", {}).get("modules", 0)
    if top_impact > int(th["large_impact_threshold"]):
        reasons.append(f"影响范围较大（{top_impact} 个模块 > {int(th['large_impact_threshold'])}）")
    target_type, impacted_types = _module_types(candidates[0], index)
    if target_type != "unknown" and impacted_types:
        crossed = {target_type} | impacted_types
        if len(crossed) >= int(th["cross_module_type_threshold"]):
            reasons.append(f"跨层修改（{target_type} → {', '.join(sorted(impacted_types))}）")

    unique_high = len(candidates) == 1 and candidates[0].get("confidence") == "HIGH"
    if not reasons and unique_high:
        return GateVerdict(confirm=False, reasons=[], selected=candidates[0])
    return GateVerdict(confirm=True, reasons=reasons, selected=None)
