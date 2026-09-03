"""Impact 数据卡：规则化影响分析证据（零 LLM）。

Planner 的风险判断必须基于证据，而不是经验。本模块从 Index 的
call_graph / include_map / build_info / understanding 计算：
- 相关符号：callers / callees / include_by / compile_status / critical
- direct（Query 直接命中）vs indirect（1-hop 扩展）标记
- 文件级影响证据

供 Plan 提示词中的 Impact Analysis 阶段引用。
"""

from __future__ import annotations

from typing import Any

from agentx.index.index import ProjectIndex


def build_impact_data(index: ProjectIndex, query_result: dict[str, Any]) -> dict[str, Any]:
    """计算任务相关的影响分析证据卡。

    query_result 来自 query_index()。返回：
    {"symbols": [{name, file, type, compile_status, callers, callees,
                  include_by, critical, impact, module, registered_by}],
     "files": [{path, compile_status, callers, include_count, critical, impact, module}],
     "dependency_chain": [{from, to, impact}],
     "modules": [{name, type, build_status, consumers, impact}]}

    Phase 7.7.3：registered_by = 该符号被注册为回调/函数指针绑定的位置
    （index.indirect_calls，函数注册事实——静态调用图无法表达，单独标注）。
    """
    from agentx.query.module_query import module_of_file, module_of_symbol

    critical_files = {
        str(f).lower() for f in (index.project_understanding or {}).get("critical_files", []) or []
    }
    calls = index.call_graph
    includes = index.include_map
    file_by_path = {f.path: f for f in index.files}
    indirect: list[dict[str, Any]] = index.indirect_calls or []
    registered: dict[str, list[dict[str, Any]]] = {}
    for e in indirect:
        registered.setdefault(str(e.get("callee", "")), []).append(
            {
                "via": e.get("via", ""),
                "file": e.get("file", ""),
                "line": e.get("line", 0),
                "caller_hint": e.get("caller_hint"),
            }
        )

    hit_symbols = {str(s.get("name", "")) for s in query_result.get("symbols", [])}
    hit_files = {str(f.get("path", "")) for f in query_result.get("files", [])}

    # 依赖链（规则化 direct/indirect）
    dependency_chain: list[dict[str, Any]] = []
    seen_edges: set[tuple[str, str]] = set()
    for e in calls:
        caller, callee = str(e.get("caller", "")), str(e.get("callee", ""))
        impact = "direct" if (caller in hit_symbols or callee in hit_symbols) else "indirect"
        key = (caller, callee)
        if key in seen_edges:
            continue
        seen_edges.add(key)
        if impact == "direct" or (caller in hit_symbols or callee in hit_symbols):
            dependency_chain.append({"from": caller, "to": callee, "impact": impact})

    # 符号证据卡（direct 命中 + indirect 波及，均从 Index 取证据）
    index_by_name = {str(s.get("name", "")): s for s in index.symbols}
    chain_symbols: set[str] = set()
    for e in dependency_chain:
        chain_symbols.add(str(e.get("from", "")))
        chain_symbols.add(str(e.get("to", "")))
    symbols: list[dict[str, Any]] = []
    for name in sorted(hit_symbols | chain_symbols):
        sym = index_by_name.get(name)
        if sym is None:
            continue
        file = str(sym.get("file", ""))
        meta = file_by_path.get(file)
        callers = sorted({str(e.get("caller", "")) for e in calls if e.get("callee") == name})
        callees = sorted({str(e.get("callee", "")) for e in calls if e.get("caller") == name})
        include_by = sorted(
            {
                src
                for src, targets in includes.items()
                if file.split("/")[-1] in {t.split("/")[-1] for t in targets}
            }
        )
        mod = module_of_symbol(index, name)
        symbols.append(
            {
                "name": name,
                "file": file,
                "type": sym.get("type"),
                "compile_status": meta.compile_status if meta else "unknown",
                "callers": callers,
                "callees": callees,
                "include_by": include_by,
                "critical": file.lower() in critical_files,
                "impact": "direct" if name in hit_symbols else "indirect",
                "module": mod["name"] if mod else None,
                "registered_by": registered.get(name, []),
            }
        )

    # 文件证据卡
    files: list[dict[str, Any]] = []
    for f in query_result.get("files", []):
        path = str(f.get("path", ""))
        path.split("/")[-1]
        meta = file_by_path.get(path)
        callers = sorted(
            {str(e.get("caller", "")) for e in calls if str(e.get("file", "")) == path}
        )
        include_count = len(includes.get(path, []))
        mod = module_of_file(index, path)
        files.append(
            {
                "path": path,
                "compile_status": meta.compile_status if meta else "unknown",
                "build_source": meta.build_source if meta else None,
                "callers": callers,
                "include_count": include_count,
                "critical": path.lower() in critical_files,
                "impact": "direct" if path in hit_files else "indirect",
                "module": mod["name"] if mod else None,
            }
        )

    # 模块级波及（Phase 7.7）：命中模块 + 其 consumers（改一个模块影响谁）
    mod_by_name = {str(m["name"]): m for m in index.modules}
    modules: list[dict[str, Any]] = []
    seen_mods: set[str] = set()
    for f in files:
        mod_name = f.get("module")
        if mod_name and mod_name not in seen_mods:
            seen_mods.add(mod_name)
            m = mod_by_name.get(mod_name)
            if m is not None:
                modules.append(
                    {
                        "name": mod_name,
                        "type": m.get("type", "unknown"),
                        "build_status": m.get("build_status", "unknown"),
                        "consumers": m.get("consumers", []),
                        "impact": "direct",
                    }
                )
    for s in symbols:
        mod_name = s.get("module")
        if mod_name and mod_name not in seen_mods:
            seen_mods.add(mod_name)
            m = mod_by_name.get(mod_name)
            if m is not None:
                modules.append(
                    {
                        "name": mod_name,
                        "type": m.get("type", "unknown"),
                        "build_status": m.get("build_status", "unknown"),
                        "consumers": m.get("consumers", []),
                        "impact": "direct" if s["impact"] == "direct" else "indirect",
                    }
                )

    return {
        "symbols": symbols,
        "files": files,
        "dependency_chain": dependency_chain[:40],
        "modules": modules,
    }


def format_impact_data(data: dict[str, Any]) -> str:
    """把证据卡转成 Planner 可读文本。"""
    lines: list[str] = []
    if data["symbols"]:
        lines.append("影响分析证据（规则计算，非推测）:")
        for s in data["symbols"]:
            mod = f" module={s.get('module')}" if s.get("module") else ""
            lines.append(
                f"  [{s['impact']}] {s['name']} ({s['type']}) @ {s['file']} "
                f"compile={s['compile_status']} callers={s['callers']} "
                f"callees={s['callees']} critical={'YES' if s['critical'] else 'no'}{mod}"
            )
            # Phase 7.7.3：回调注册标注（函数地址逃逸——静态调用图无法表达）
            regs = s.get("registered_by") or []
            if regs:
                lines.append(
                    f"    注意: {s['name']} 作为回调被注册 {len(regs)} 处"
                    f"（静态调用图无法表达调用路径，修改其签名将影响所有注册点）:"
                )
                for r in regs[:5]:
                    hint = f" 注册者={r['caller_hint']}" if r.get("caller_hint") else ""
                    lines.append(f"      via={r['via']} @ {r['file']}:{r['line']}{hint}")
                if len(regs) > 5:
                    lines.append(f"      ... 另有 {len(regs) - 5} 处")
    if data["files"]:
        lines.append("文件级证据:")
        for f in data["files"]:
            mod = f" module={f.get('module')}" if f.get("module") else ""
            lines.append(
                f"  [{f['impact']}] {f['path']} compile={f['compile_status']} "
                f"callers={f['callers']} includes={f['include_count']} "
                f"critical={'YES' if f['critical'] else 'no'}{mod}"
            )
    if data["dependency_chain"]:
        lines.append("依赖链:")
        for e in data["dependency_chain"]:
            lines.append(f"  {e['from']} -> {e['to']} [{e['impact']}]")
    # Phase 7.7：模块级波及（改一个模块 → 下游 consumers）
    if data["modules"]:
        lines.append("模块级波及（Module Knowledge）:")
        for m in data["modules"]:
            consumers = ", ".join(m["consumers"][:6]) if m["consumers"] else "(none)"
            lines.append(
                f"  [{m['impact']}] {m['name']} ({m['type']}) "
                f"build={m['build_status']} consumers={consumers}"
            )
    # Build Reality 建议（规则化）：excluded/unknown 文件不修改生产路径
    excluded = [f["path"] for f in data["files"] if f.get("compile_status") == "excluded"]
    unknown = [f["path"] for f in data["files"] if f.get("compile_status") == "unknown"]
    if excluded:
        lines.append("Build Reality:")
        lines.append(f"  以下文件未参与编译（excluded）：{', '.join(excluded[:5])}")
        lines.append("  Recommendation: only modify production path; 避免修改未编译文件")
    if unknown:
        lines.append(f"  以下文件编译状态未知（无构建配置）：{', '.join(unknown[:5])}")
    return "\n".join(lines)
