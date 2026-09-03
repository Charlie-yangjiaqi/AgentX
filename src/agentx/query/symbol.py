"""Symbol Query："这个函数在哪里？谁调用它？"

输入精确/部分符号名（key_scan / uart_protocol_init），
基于 Index 事实返回定义、调用方（文件级）、被调用方、关联文件。
零 LLM、零扫描。
"""

from __future__ import annotations

from typing import Any

from agentx.index.index import ProjectIndex
from agentx.query.index_query import (
    callees_of,
    caller_files_of,
    callers_of,
    expand_includes,
    file_of_symbol,
    no_evidence,
)

NEXT_ANSWER = "answer"
NEXT_READ_SOURCE = "read_source"


def search_symbol(index: ProjectIndex, symbol_name: str) -> dict[str, Any]:
    """按符号名查询：definition / callers（文件级）/ callees / related_files。"""
    name = (symbol_name or "").strip()
    if not name:
        return no_evidence("no symbol name provided")

    # 精确匹配优先，其次大小写不敏感，再次子串
    target: dict[str, Any] | None = None
    exact = next((s for s in index.symbols if s.get("name") == name), None)
    if exact is not None:
        target = exact
    else:
        lower = name.lower()
        casefold = next((s for s in index.symbols if str(s.get("name", "")).lower() == lower), None)
        if casefold is not None:
            target = casefold
        else:
            substring = next(
                (s for s in index.symbols if lower in str(s.get("name", "")).lower()), None
            )
            if substring is not None:
                target = substring

    if target is None:
        return no_evidence(f"no matching symbol: {name}")

    sym_name = str(target.get("name", ""))
    def_file = str(target.get("file", ""))
    callers = callers_of(index, sym_name)
    callees = callees_of(index, sym_name)
    caller_files = caller_files_of(index, sym_name)

    # related_files：调用方文件 + include 关联
    hit_files: dict[str, str] = {}
    if def_file:
        hit_files[def_file] = "definition"
    for f in caller_files:
        if f not in hit_files:
            hit_files[f] = "caller"
    dep_targets, dep_sources = expand_includes(index, hit_files)

    related_files: list[str] = []
    seen: set[str] = set()
    for f in caller_files:
        if f != def_file and f not in seen:
            seen.add(f)
            related_files.append(f)
    for p in list(dep_targets) + list(dep_sources):
        if p != def_file and p not in seen:
            seen.add(p)
            related_files.append(p)

    evidence = [f"{sym_name} defined in {def_file}" if def_file else f"{sym_name} defined"]
    for c in callers:
        cf = file_of_symbol(index, c) or c
        evidence.append(f"{sym_name} called by {c} in {cf}")
    for c in callees:
        evidence.append(f"{sym_name} calls {c}")

    confidence = "high" if callers or callees else "medium"
    recommended: dict[str, Any] = {"type": NEXT_ANSWER}

    # Phase 7.6：语义细节（signature/members/value）与 reindex 建议
    semantic_detail: dict[str, Any] = {}
    has_semantic = target.get("semantic") is True
    if has_semantic:
        for field in ("signature", "members", "value", "value_expr"):
            if field in target:
                semantic_detail[field] = target[field]
    elif target.get("type") in ("function", "struct", "enum", "macro"):
        # 旧 Index / 未经过语义提取：不猜测，明确建议
        caps = (index.capabilities or {}).get("semantic") or {}
        if caps.get("enabled") is False:
            # Semantic runtime 不可用（不是数据旧）：明确诊断动作
            reason = str(caps.get("reason") or "semantic runtime unavailable")
            recommended = {"type": "doctor", "reason": f"Semantic unavailable: {reason}"}
        else:
            recommended = {"type": "reindex", "reason": "semantic index data unavailable"}

    if not callers and not callees and recommended.get("type") == NEXT_ANSWER:
        recommended = {"type": NEXT_READ_SOURCE, "files": [def_file] if def_file else []}

    # Build Reality（definition 文件编译状态）
    build: dict[str, Any] = {}
    from agentx.query.index_query import build_facts

    if def_file:
        build = build_facts(index, [def_file])
    if recommended.get("type") == NEXT_ANSWER and build.get("compile_status") == "excluded":
        recommended = {"type": NEXT_READ_SOURCE, "files": [def_file] if def_file else []}

    # Phase 7.7：符号所在模块（Module Knowledge）
    from agentx.query.module_query import module_of_symbol

    module = module_of_symbol(index, sym_name)

    return {
        "query": symbol_name,
        "symbol": sym_name,
        "module": module,
        "definition": {
            "file": def_file,
            "start_line": target.get("start_line"),
            "end_line": target.get("end_line"),
            "type": target.get("type"),
            "semantic": has_semantic,
            **semantic_detail,
        },
        "callers": callers[:20],
        "caller_files": caller_files[:20],
        "callees": callees[:20],
        "related_files": related_files[:20],
        "build": build,
        "confidence": confidence,
        "evidence": evidence[:30],
        "recommended_action": recommended,
        "reason": [f"symbol {sym_name} matched"],
    }


def search_symbol_legacy(index: ProjectIndex, symbol_name: str) -> dict[str, Any]:
    result = search_symbol(index, symbol_name)
    result["recommended_next_action"] = result["recommended_action"].get("type", NEXT_READ_SOURCE)
    return result
