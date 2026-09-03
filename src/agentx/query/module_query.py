"""Module Query：模块知识查询（Phase 7.7，零 LLM、零扫描）。

- search_module：--module KEY → 模块卡片（files/symbols/entry/consumers/
  dependencies/build/confidence/evidence.basis）
- module_of_file / module_of_symbol：普通 symbol/feature 查询自动附带模块
- format_module_view：Planner 上下文模块视图（命中模块 + 相邻模块）
"""

from __future__ import annotations

from typing import Any

from agentx.index.index import ProjectIndex


def module_of_file(index: ProjectIndex, path: str) -> dict[str, Any] | None:
    """文件 → 模块（精确路径 > 唯一 basename）。"""
    for m in index.modules:
        if path in m.get("files", []):
            return m
    base = path.rsplit("/", 1)[-1]
    matches = [
        m for m in index.modules if base in {f.rsplit("/", 1)[-1] for f in m.get("files", [])}
    ]
    if len(matches) == 1:
        return matches[0]
    return None


def module_of_symbol(index: ProjectIndex, name: str) -> dict[str, Any] | None:
    """符号 → 模块。"""
    for m in index.modules:
        if name in m.get("symbols", []):
            return m
    return None


def _module_hit(index: ProjectIndex, name: str) -> dict[str, Any] | None:
    """模块名匹配：精确（大小写不敏感）→ 子串。"""
    lower = name.casefold()
    for m in index.modules:
        if str(m.get("name", "")).casefold() == lower:
            return m
    for m in index.modules:
        if lower in str(m.get("name", "")).casefold():
            return m
    return None


def search_module(index: ProjectIndex, name: str) -> dict[str, Any]:
    """模块查询：完整模块卡片（全部来自 Index 事实）。"""
    name = (name or "").strip()
    if not name:
        return {
            "query": name,
            "confidence": "low",
            "module": None,
            "evidence": [],
            "recommended_action": {"type": "read_source"},
            "reason": ["no module name provided"],
        }
    if not index.modules:
        return {
            "query": name,
            "confidence": "low",
            "module": None,
            "evidence": [],
            "recommended_action": {
                "type": "reindex",
                "reason": "module knowledge unavailable (run `agentx plan` to reindex)",
            },
            "reason": ["module knowledge unavailable"],
        }
    target = _module_hit(index, name)
    if target is None:
        return {
            "query": name,
            "confidence": "low",
            "module": None,
            "evidence": [],
            "recommended_action": {"type": "read_source"},
            "reason": [f"no matching module: {name}"],
        }
    evidence = [
        f"{target['name']} module: {len(target.get('files', []))} files, "
        f"{len(target.get('symbols', []))} symbols",
        f"basis: {', '.join(target.get('evidence', {}).get('basis', []))}",
    ]
    if target.get("consumers"):
        evidence.append(f"consumers: {', '.join(target['consumers'][:10])}")
    if target.get("dependencies"):
        evidence.append(f"dependencies: {', '.join(target['dependencies'][:10])}")
    if target.get("third_party"):
        evidence.append("third_party: yes (not modified by project code)")
    confidence = "high" if target.get("confidence", 0) >= 0.85 else "medium"
    return {
        "query": name,
        "module": target,
        "confidence": confidence,
        "evidence": evidence,
        "recommended_action": {"type": "answer"},
        "reason": [f"module {target['name']} matched"],
    }


def format_module_card(result: dict[str, Any]) -> str:
    """模块卡片（ASCII 安全，CLI 展示）。"""
    m = result.get("module")
    if m is None:
        return "(no module data)"
    lines = [f"Module: {m.get('name')} ({m.get('type', 'unknown')})"]
    files = m.get("files", [])
    if files:
        shown = files[:6]
        suffix = f" ... (+{len(files) - 6})" if len(files) > 6 else ""
        lines.append(f"  Files ({len(files)}): {', '.join(shown)}{suffix}")
    syms = m.get("symbols", [])
    if syms:
        lines.append(f"  Symbols ({len(syms)}): {', '.join(syms[:12])}")
    if m.get("entry_points"):
        lines.append(f"  Entry: {', '.join(m['entry_points'])}")
    if m.get("consumers"):
        lines.append(f"  Consumers: {', '.join(m['consumers'][:10])}")
    if m.get("dependencies"):
        lines.append(f"  Dependencies: {', '.join(m['dependencies'][:10])}")
    bs = m.get("build_status")
    stats = m.get("build_stats") or {}
    if bs:
        lines.append(f"  Build: {bs} ({stats.get('compiled', 0)}/{stats.get('total', 0)} compiled)")
    if m.get("third_party"):
        lines.append("  Third-party: yes")
    lines.append(f"  Confidence: {m.get('confidence', 0)}")
    basis = (m.get("evidence") or {}).get("basis", [])
    if basis:
        lines.append(f"  Basis: {', '.join(basis)}")
    return "\n".join(lines)


def format_module_view(index: ProjectIndex, query_result: dict[str, Any]) -> str:
    """Planner 模块视图：命中文件/符号所在模块 + 相邻模块（consumers/dependencies）。"""
    if not index.modules:
        return ""
    hit: list[dict[str, Any]] = []
    seen: set[str] = set()
    for f in query_result.get("files", []):
        m = module_of_file(index, str(f.get("path", "")))
        if m and m["name"] not in seen:
            seen.add(m["name"])
            hit.append(m)
    for s in query_result.get("symbols", []):
        m = module_of_symbol(index, str(s.get("name", "")))
        if m and m["name"] not in seen:
            seen.add(m["name"])
            hit.append(m)
    if not hit:
        return ""
    lines = ["模块视图（Module Knowledge）:"]
    for m in hit[:6]:
        parts = []
        if m.get("consumers"):
            parts.append(f"consumers={','.join(m['consumers'][:5])}")
        if m.get("dependencies"):
            parts.append(f"depends={','.join(m['dependencies'][:5])}")
        suffix = f" {', '.join(parts)}" if parts else ""
        lines.append(
            f"  {m['name']} ({m.get('type', '?')}) build={m.get('build_status', '?')}{suffix}"
        )
    return "\n".join(lines)
