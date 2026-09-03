"""Feature Query：工程探索（"XXX功能在哪里？"）。

基于 query/index_query.py 共享底座，零 LLM、零扫描，纯 Index 事实。
匹配优先级：symbol > file > include/dependency > keyword。
"""

from __future__ import annotations

from typing import Any

from agentx.index.index import ProjectIndex
from agentx.query.index_query import (
    _basename,
    build_facts,
    expand_call_edges,
    expand_includes,
    find_files,
    find_symbols,
    merge_edge_files,
    no_evidence,
    related_modules,
)
from agentx.understanding.query import extract_keywords

CONF_HIGH = "high"
CONF_MEDIUM = "medium"
CONF_LOW = "low"

NEXT_ANSWER = "answer"
NEXT_READ_SOURCE = "read_source"


def search_feature(index: ProjectIndex, task: str) -> dict[str, Any]:
    """工程探索：任务 → Feature Evidence（基于 Index 事实，不扫描工程）。"""
    keywords = extract_keywords(task)
    if not keywords:
        return no_evidence("no query keywords extracted")

    # 1. symbol 直接命中（最高优先级）→ 2. 文件命中
    hit_symbols = find_symbols(index, keywords)
    hit_files = find_files(index, keywords)
    if not hit_symbols and not hit_files:
        return no_evidence("no matching symbols/files")

    file_meta = {f.path: f for f in index.files}

    # 3. 调用图扩展 + 边另一端的文件并入
    hit_sym_names = set(hit_symbols)
    edges = expand_call_edges(index, hit_sym_names)
    merge_edge_files(index, edges, hit_sym_names, hit_files)

    # 4. include/dependency 扩展
    dep_targets, dep_sources = expand_includes(index, hit_files)

    # 5. Build Reality
    build = build_facts(index, list(hit_files))
    # excluded 主文件：不作正式实现推荐（事实标注，不编造）
    excluded_primary = False
    for p in hit_files:
        meta_entry = file_meta.get(p)
        if meta_entry is not None and str(meta_entry.compile_status) == "excluded":
            excluded_primary = True
            break

    # 6. confidence（符号命中优先）
    confidence = (CONF_HIGH if edges else CONF_MEDIUM) if hit_symbols else CONF_MEDIUM

    # 7. call_chain（方向性：from 调用 to）
    call_chain: list[dict[str, str]] = [
        {
            "from": str(e.get("caller", "")),
            "to": str(e.get("callee", "")),
            "type": "caller",
        }
        for e in edges
    ]

    # 8. related_modules（消费方/相邻模块）
    modules = related_modules(index, hit_files)

    # 9. evidence（事实句，规则生成）
    evidence = _build_evidence(index, hit_symbols, hit_files, edges, dep_targets, dep_sources)

    # 10. summary（规则拼接，不编造）
    summary = _build_summary(hit_symbols, hit_files, call_chain, build)

    # 11. recommended_action（枚举对象；excluded 不推荐为正式实现）
    recommended = _recommend_action(index, confidence, hit_symbols, edges, hit_files, build)
    if excluded_primary and recommended.get("type") == NEXT_ANSWER:
        recommended = {"type": NEXT_READ_SOURCE, "files": list(hit_files)[:5]}
        summary += "（命中文件未参与编译，非正式固件路径）"

    feature = _feature_name(hit_symbols, keywords)

    return {
        "query": task,
        "feature": feature,
        "confidence": confidence,
        "files": [{"path": p, "reason": r} for p, r in list(hit_files.items())[:20]],
        "dependency_files": [
            {"path": p, "reason": r}
            for p, r in dict(list(dep_targets.items()) + list(dep_sources.items())).items()
        ],
        "symbols": [
            {"name": str(s.get("name", "")), "file": str(s.get("file", ""))}
            for s in hit_symbols.values()
        ][:20],
        "call_chain": call_chain[:20],
        "related_modules": modules,
        "build": build,
        "evidence": evidence[:30],
        "summary": summary,
        "recommended_action": recommended,
        "reason": [f"{p} ← {r}" for p, r in list(hit_files.items())[:15]]
        + [f"{n} ← {hit_symbols[n].get('hit')}" for n in list(hit_symbols)[:15]],
    }


def search_feature_legacy(index: ProjectIndex, task: str) -> dict[str, Any]:
    """兼容旧调用方（recommended_next_action 字符串版）。"""
    result = search_feature(index, task)
    ra = result["recommended_action"]
    result["recommended_next_action"] = ra.get("type", NEXT_READ_SOURCE)
    return result


# ---------- 内部辅助 ----------


def _build_evidence(
    index: ProjectIndex,
    hit_symbols: dict[str, dict[str, Any]],
    hit_files: dict[str, str],
    edges: list[dict[str, Any]],
    dep_targets: dict[str, str],
    dep_sources: dict[str, str],
) -> list[str]:
    out: list[str] = []
    for name, sym in hit_symbols.items():
        f = str(sym.get("file", ""))
        out.append(f"{name} defined in {f}" if f else f"{name} defined (no file recorded)")
    for e in edges:
        caller, callee = str(e.get("caller", "")), str(e.get("callee", ""))
        caller_file = None
        for s in index.symbols:
            if s.get("name") == caller:
                caller_file = s.get("file")
                break
        label = caller_file or caller
        out.append(f"{callee} consumed by {label}")
    for t, r in dep_targets.items():
        out.append(f"{_basename(t)} included by matched files ({r})")
    for src, r in dep_sources.items():
        out.append(f"{_basename(src)} includes matched files ({r})")
    return out


def _feature_name(hit_symbols: dict[str, dict[str, Any]], keywords: list[str]) -> str:
    if hit_symbols:
        return next(iter(hit_symbols))
    return keywords[0] if keywords else "unknown"


def _build_summary(
    hit_symbols: dict[str, dict[str, Any]],
    hit_files: dict[str, str],
    call_chain: list[dict[str, str]],
    build: dict[str, Any],
) -> str:
    parts: list[str] = []
    if hit_symbols:
        parts.append("由 " + ", ".join(list(hit_symbols)[:3]) + " 实现")
    if hit_files:
        parts.append("位于 " + ", ".join(list(hit_files)[:3]))
    if call_chain:
        first = call_chain[0]
        parts.append(f"{first.get('from')} 调用 {first.get('to')}")
    cs = build.get("compile_status")
    if cs:
        parts.append(f"编译状态: {cs}")
    if not parts:
        return ""
    return "；".join(parts) + "。"


def _recommend_action(
    index: ProjectIndex,
    confidence: str,
    hit_symbols: dict[str, dict[str, Any]],
    edges: list[dict[str, Any]],
    hit_files: dict[str, str],
    build: dict[str, Any],
) -> dict[str, Any]:
    if confidence == CONF_LOW:
        return {"type": NEXT_READ_SOURCE}
    cs = build.get("compile_status")
    if cs in ("excluded", "unknown"):
        return {"type": NEXT_READ_SOURCE, "files": list(hit_files)[:5]}
    if not hit_symbols and not edges:
        return {"type": NEXT_READ_SOURCE, "files": list(hit_files)[:5]}
    return {"type": NEXT_ANSWER}
