"""Phase 7.8 Candidate Analyzer：修改候选生成（零 LLM，规则化）。

需求 → query_index 命中 → 每命中点生成候选（module/symbol/file 级）。

候选证据结构（事实/推断严格分离）：
- Fact:     {type:"fact",     source: call_graph|indirect_calls|struct_usage|
             dependencies|consumers, description}
- Inference:{type:"inference", source: responsibility|naming, description}

Confidence（规则计算，非 LLM 自评）：
- HIGH:   ≥2 个独立事实来源（call_graph/indirect_calls/struct_usage/deps/cons）
- MEDIUM: 单事实来源
- LOW:    仅名称/路径/文件名
confidence 只表示证据强度，不是"AI 认为答案正确"。

candidate_id 版本绑定：候选携带生成时的 index fingerprint + module facts
快照；选择时校验（工程变化 → 提示重新生成，防选择错位）。
"""

from __future__ import annotations

from typing import Any

from agentx.index.index import ProjectIndex
from agentx.query.module_query import module_of_file, module_of_symbol

# 独立事实来源（confidence 计数域）
_FACT_SOURCES = ("call_graph", "indirect_calls", "struct_usage", "dependencies", "consumers")

# 分数（排序用）：0.3 + 0.35 * 独立事实来源数（cap 3）→ HIGH=1.0 / MEDIUM=0.65 / LOW=0.3
_SCORE_BASE = 0.3
_SCORE_PER_SOURCE = 0.35
_SCORE_CAP = 3


def _fact(evidence: list[dict[str, Any]], source: str, description: str) -> None:
    evidence.append({"type": "fact", "source": source, "description": description})


def _inference(evidence: list[dict[str, Any]], source: str, description: str) -> None:
    evidence.append({"type": "inference", "source": source, "description": description})


def _goal_keywords(goal: str) -> list[str]:
    """需求关键词：字母数字 token（中英文混合工程：中文原样 + 英文 token）。"""
    import re

    words = re.findall(r"[A-Za-z_][A-Za-z0-9_]{2,}", goal)
    chinese = re.findall(r"[\u4e00-\u9fff]{2,}", goal)
    return words + chinese


def _responsibility_match(
    module: dict[str, Any], entries: dict[str, Any], keywords: list[str]
) -> str | None:
    """responsibility 与需求关键词重合 → 推断证据（仅 inference，禁止当事实）。"""
    entry = entries.get(str(module.get("name", "")))
    if entry is None:
        return None
    resp = str(entry.get("responsibility", ""))
    if not resp:
        return None
    for kw in keywords:
        if kw and kw.lower() in resp.lower():
            return resp[:80]
    return None


def analyze_candidates(
    index: ProjectIndex,
    query_result: dict[str, Any],
    goal: str,
    responsibility_entries: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """从需求生成修改候选列表（有序：分数降序）。

    返回 []：无任何命中（调用方决定是否直接放行/提示）。
    """
    from agentx.understanding.impact import build_impact_data

    entries = responsibility_entries or {}
    keywords = _goal_keywords(goal)
    calls = index.call_graph
    indirect = index.indirect_calls or []
    usage = (index.type_semantics or {}).get("struct_usage", {})

    # 命中模块集合（文件/符号 → 模块）
    hit_mods: dict[str, dict[str, Any]] = {}
    for f in query_result.get("files", []):
        m = module_of_file(index, str(f.get("path", "")))
        if m is not None:
            hit_mods[str(m["name"])] = m
    for s in query_result.get("symbols", []):
        m = module_of_symbol(index, str(s.get("name", "")))
        if m is not None:
            hit_mods[str(m["name"])] = m
            _attach_symbol_hit(m, str(s.get("name", "")))
    # 中文需求 ↔ 英文符号名不直接匹配：responsibility 文本匹配补充候选
    # （仅补充命中集合——证据仍分 fact/inference，不会抬高置信度）
    if entries and keywords:
        for mod in index.modules:
            name = str(mod.get("name", ""))
            if name in hit_mods:
                continue
            if _responsibility_match(mod, entries, keywords) is not None:
                hit_mods[name] = mod

    candidates: list[dict[str, Any]] = []
    for mod_name, mod in hit_mods.items():
        evidence: list[dict[str, Any]] = []
        sources: set[str] = set()

        # Fact: 调用关系（模块内符号在 call_graph 中的边）
        syms = {str(s) for s in mod.get("symbols", []) or []}
        if any(e.get("caller") in syms or e.get("callee") in syms for e in calls):
            _fact(evidence, "call_graph", f"模块符号参与 {len(calls)} 条调用边（部分）")
            sources.add("call_graph")
        # Fact: 间接注册
        mod_files = set(mod.get("files", []) or [])
        regs = [
            e
            for e in indirect
            if str(e.get("callee", "")) in syms or str(e.get("file", "")) in mod_files
        ]
        if regs:
            _fact(evidence, "indirect_calls", f"{len(regs)} 处函数注册/绑定（回调接口）")
            sources.add("indirect_calls")
        # Fact: 字段读写
        field_usage = {
            fname: agg
            for fname, agg in usage.items()
            if any(w in syms for w in agg.get("read_by", []) + agg.get("write_by", []))
        }
        if field_usage:
            used = list(field_usage)[:3]
            _fact(evidence, "struct_usage", f"字段读写（{', '.join(used)}...）")
            sources.add("struct_usage")
        # Fact: 依赖/消费方
        if mod.get("dependencies"):
            _fact(
                evidence,
                "dependencies",
                f"依赖: {', '.join(str(d) for d in mod['dependencies'][:5])}",
            )
            sources.add("dependencies")
        if mod.get("consumers"):
            _fact(evidence, "consumers", f"被 {len(mod['consumers'])} 个模块调用")
            sources.add("consumers")

        # Inference: responsibility 匹配（理解层，仅推断）
        match = _responsibility_match(mod, entries, keywords)
        if match:
            _inference(evidence, "responsibility", f"职责描述含需求关键词: {match}")
        else:
            _inference(evidence, "naming", "模块名与需求关键词相关（名称匹配，非事实）")

        # Confidence（规则）
        n_sources = len(sources)
        confidence = "HIGH" if n_sources >= 2 else ("MEDIUM" if n_sources >= 1 else "LOW")
        confidence_reason = f"{n_sources} 个独立事实来源: {', '.join(sorted(sources)) or '无'}"
        score = round(
            min(
                _SCORE_BASE + _SCORE_PER_SOURCE * n_sources,
                _SCORE_BASE + _SCORE_PER_SOURCE * _SCORE_CAP,
            ),
            2,
        )

        # Impact scope（模块波及）
        query = {"files": [], "symbols": [{"name": n} for n in syms]}
        impact = build_impact_data(index, query)
        impact_mods = [m for m in impact.get("modules", []) if m.get("name") != mod_name]

        # risk_level（规则）：公共接口 / 大影响 → high
        risk = "low"
        if len(impact_mods) > 5 or mod.get("third_party"):
            risk = "high"
        elif impact_mods:
            risk = "medium"

        candidates.append(
            {
                "id": "",
                "target": mod_name,
                "target_type": "module",
                "confidence": confidence,
                "confidence_reason": confidence_reason,
                "score": score,
                "evidence": evidence,
                "impact_scope": {
                    "modules": len(impact_mods),
                    "module_names": [m["name"] for m in impact_mods[:8]],
                },
                "risk_level": risk,
                "index_fingerprint": index.project_fingerprint,
                "module_facts": {
                    "entry_points": mod.get("entry_points", []),
                    "dependencies": mod.get("dependencies", []),
                    "consumers": mod.get("consumers", []),
                    "symbol_count": len(syms),
                },
            }
        )

    # 排序 + 编号（C001...）
    candidates.sort(key=lambda c: -c["score"])
    for i, c in enumerate(candidates, start=1):
        c["id"] = f"C{i:03d}"
    return candidates


def _attach_symbol_hit(module: dict[str, Any], symbol: str) -> None:
    """命中符号注入模块（供 evidence 展示；不修改事实层数据本身）。"""
    module.setdefault("_hit_symbols", set()).add(symbol)
