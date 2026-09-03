"""Module 关系推断：依赖 / 消费方 / 入口点 / Build 聚合（确定性，零 LLM）。

基于 call_graph（weight=3，high confidence +1）与 include_map（weight=2）
计算跨模块依赖；入口点 = 被模块外调用且模块内零调用的公开函数；
build_status = 模块文件 compile_status 聚合（compiled > not_compiled > excluded > unknown）。
"""

from __future__ import annotations

from typing import Any

from agentx.index.index import ProjectIndex


def infer_module_relations(
    modules: list[dict[str, Any]], index: ProjectIndex
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """补充模块级关系；返回 (modules, dependencies)。"""
    file_to_mod: dict[str, str] = {}
    sym_to_mod: dict[str, str] = {}
    for m in modules:
        for f in m.get("files", []):
            file_to_mod[f] = str(m["name"])
        for s in m.get("symbols", []):
            sym_to_mod[s] = str(m["name"])

    dep_map: dict[tuple[str, str], dict[str, Any]] = {}

    def _add(frm: str, to: str, kind: str, via: str, weight: int) -> None:
        d = dep_map.setdefault(
            (frm, to), {"from": frm, "to": to, "kind": [], "weight": 0, "via": []}
        )
        if kind not in d["kind"]:
            d["kind"].append(kind)
        d["weight"] += weight
        if via not in d["via"]:
            d["via"].append(via)

    # 调用边（符号级，caller 文件兜底）
    for e in index.call_graph:
        caller = str(e.get("caller", ""))
        callee = str(e.get("callee", ""))
        m1 = sym_to_mod.get(caller) or file_to_mod.get(str(e.get("file", "")))
        m2 = sym_to_mod.get(callee)
        if m1 and m2 and m1 != m2:
            w = 3 + (1 if e.get("confidence") == "high" else 0)
            _add(m1, m2, "call", callee, w)

    # include 边（文件级）
    for src, targets in index.include_map.items():
        m1 = file_to_mod.get(src)
        if not m1:
            continue
        for t in targets:
            m2 = file_to_mod.get(t)
            if m2 and m2 != m1:
                _add(m1, m2, "include", t, 2)

    dependencies = sorted(dep_map.values(), key=lambda d: -d["weight"])
    consumers: dict[str, list[str]] = {}
    for d in dependencies:
        consumers.setdefault(d["to"], []).append(d["from"])

    calls = index.call_graph
    file_meta = {f.path: f for f in index.files}
    for mod in modules:
        syms = list(mod["symbols"])  # 保序（entry_points 确定性）
        entries: list[tuple[str, int]] = []
        for s in syms:
            internal = any(e.get("caller") == s and e.get("callee") in syms for e in calls)
            external = [e for e in calls if e.get("callee") == s and e.get("caller") not in syms]
            if external and not internal:
                entries.append((s, len(external)))
        entries.sort(key=lambda x: -x[1])
        mod["entry_points"] = [n for n, _ in entries[:3]]
        mod["dependencies"] = [d["to"] for d in dependencies if d["from"] == mod["name"]]
        mod["consumers"] = consumers.get(mod["name"], [])

        # Build 聚合：模块内任一文件 compiled → compiled
        statuses = [file_meta[f].compile_status for f in mod["files"] if f in file_meta]
        if "compiled" in statuses:
            bs = "compiled"
        elif "not_compiled" in statuses:
            bs = "not_compiled"
        elif "excluded" in statuses:
            bs = "excluded"
        else:
            bs = "unknown"
        mod["build_status"] = bs
        mod["build_stats"] = {
            "compiled": statuses.count("compiled"),
            "total": len(mod["files"]),
        }
    return modules, dependencies
