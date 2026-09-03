"""Evidence Builder：Index 事实 → 证据卡（AgentX 提供事实，宿主负责表达）。

格式（Phase 7）：
- [Driver]/[Consumer] 分组：谁提供能力（Provides）？谁使用能力（Consumes）？
- Architecture 用 Evidence Flow（符号链事实，不解释语义）
- 禁止输出无证据的推断（"该功能一定用于xxx"）
"""

from __future__ import annotations

from typing import Any

from agentx.index.index import ProjectIndex
from agentx.query.index_query import _basename, file_of_symbol


def build_evidence_card(index: ProjectIndex, result: dict[str, Any]) -> str:
    """feature 证据卡：[Module]（文件所在模块）+ [Driver]/[Consumer] + Build。"""
    lines: list[str] = []
    summary = result.get("summary", "")
    if summary:
        lines.append("Conclusion:")
        lines.append(f"  {summary}")
        lines.append("")

    # Phase 7.7：命中文件所在模块（Module Knowledge）
    from agentx.query.module_query import module_of_file

    hit_mods: list[dict[str, Any]] = []
    seen_mods: set[str] = set()
    for sym in result.get("symbols", []):
        f = sym.get("file") if isinstance(sym, dict) else None
        if not f:
            f = file_of_symbol(index, str(sym.get("name", sym)))
        if not f:
            continue
        m = module_of_file(index, str(f))
        if m and m["name"] not in seen_mods:
            seen_mods.add(m["name"])
            hit_mods.append(m)
    if hit_mods:
        lines.append("")
        lines.append("[Module]")
        for m in hit_mods[:4]:
            entry = ",".join(m.get("entry_points", []) or ["-"])
            lines.append(
                f"  {m['name']} ({m.get('type', 'unknown')}) "
                f"build={m.get('build_status', '?')} "
                f"files={len(m.get('files', []))} entry={entry}"
            )

    driver_files: dict[str, list[str]] = {}
    for sym in result.get("symbols", []):
        f = sym.get("file") if isinstance(sym, dict) else None
        if not f:
            f = file_of_symbol(index, str(sym.get("name", sym)))
        if not f:
            continue
        driver_files.setdefault(str(f), []).append(str(sym.get("name", sym)))

    consumer_files: dict[str, list[str]] = {}
    for e in result.get("call_chain", []):
        # from（调用者）文件：from 为符号时查定义文件
        caller = str(e.get("from", ""))
        callee = str(e.get("to", ""))
        caller_file = file_of_symbol(index, caller) or caller
        callee_file = file_of_symbol(index, callee) or callee
        # 消费的是被调用符号
        if callee_file != caller_file:
            consumer_files.setdefault(str(caller_file), [])
            if callee not in consumer_files[str(caller_file)]:
                consumer_files[str(caller_file)].append(callee)

    # 依赖（include）也作为 Consumer 证据
    for d in result.get("dependency_files", []):
        p = str(d.get("path", ""))
        consumer_files.setdefault(p, [])
        reason = str(d.get("reason", ""))
        if reason not in consumer_files[p]:
            consumer_files[p].append(f"({reason})")

    lines.append("Evidence:")
    if driver_files:
        lines.append("")
        lines.append("[Driver]")
        for f, syms in driver_files.items():
            lines.append(f"  File: {f}")
            lines.append(f"  Provides: {', '.join(f'{s}()' for s in syms)}")
            # Phase 7.6：该文件的宏定义（来自 Index semantic 字段）
            file_defines = [
                s
                for s in index.symbols
                if s.get("type") == "macro" and s.get("file") == f and s.get("semantic") is True
            ]
            if file_defines:
                parts = []
                for d in file_defines[:8]:
                    name = str(d.get("name", ""))
                    if d.get("value") is not None:
                        parts.append(f"{name} = {d['value']}")
                    elif d.get("value_expr") is not None:
                        parts.append(f"{name} = {d['value_expr']}")
                    else:
                        parts.append(name)
                lines.append(f"  Defines: {', '.join(parts)}")
    if consumer_files:
        lines.append("")
        lines.append("[Consumer]")
        for f, items in consumer_files.items():
            lines.append(f"  File: {f}")
            lines.append(f"  Consumes: {', '.join(items)}")
    if not driver_files and not consumer_files:
        lines.append("  (no structural evidence)")

    build = result.get("build", {})
    if build:
        lines.append("")
        lines.append("Build:")
        lines.append(f"  compile_status: {build.get('compile_status')}")
        if build.get("target"):
            lines.append(f"  target: {build['target']}")
        if build.get("cpu"):
            lines.append(f"  cpu: {build['cpu']}")
        if build.get("defines"):
            defines = ", ".join(str(d) for d in build["defines"][:10])
            lines.append(f"  defines: {defines}")
        if build.get("compile_status") == "excluded":
            lines.append("  note: excluded from build (not in production firmware)")

    lines.append("")
    lines.append(f"Confidence: {result.get('confidence', 'unknown')}")
    return "\n".join(lines)


def format_flow(flow: list[str], evidence: list[str]) -> str:
    """Architecture 证据流：符号链事实（不解释语义）。"""
    lines: list[str] = []
    if flow:
        lines.append("Evidence Flow:")
        for i, step in enumerate(flow):
            if i > 0:
                lines.append("      |")
                lines.append("      v")
            lines.append(f"  {step}")
    if evidence:
        lines.append("")
        lines.append("Evidence:")
        for e in evidence:
            lines.append(f"  - {e}")
    return "\n".join(lines)


def format_symbol_card(result: dict[str, Any]) -> str:
    """Symbol 证据卡：模块 + 定义 + 语义细节 + 调用方 + 被调用方 + 关联文件。"""
    lines: list[str] = []
    lines.append("Evidence:")
    # Phase 7.7：符号所在模块
    module = result.get("module")
    if isinstance(module, dict) and module.get("name"):
        lines.append("")
        lines.append("[Module]")
        lines.append(
            f"  {module.get('name')} ({module.get('type', 'unknown')}) "
            f"build={module.get('build_status', '?')} "
            f"entry={','.join(module.get('entry_points', []) or ['-'])}"
        )
        if module.get("consumers"):
            lines.append(f"  Consumers: {', '.join(module['consumers'][:6])}")
    definition = result.get("definition") or {}
    lines.append("")
    lines.append("[Definition]")
    lines.append(f"  File: {definition.get('file', '(no file recorded)')}")
    if definition.get("start_line") is not None:
        lines.append(f"  Lines: {definition.get('start_line')}-{definition.get('end_line')}")
    if definition.get("semantic"):
        lines.append("")
        lines.append("[Semantic]")
        signature = definition.get("signature")
        if isinstance(signature, dict) and signature.get("text"):
            lines.append(f"  Signature: {signature['text']}")
        elif isinstance(signature, dict):
            params = ", ".join(
                f"{p.get('type', '')} {p.get('name', '')}".strip()
                for p in signature.get("parameters", [])
            )
            lines.append(
                f"  Signature: {signature.get('return_type', '')} ({params})".replace(" ( )", "()")
            )
        elif isinstance(signature, str) and signature:
            lines.append(f"  Signature: {signature}")
        members = definition.get("members")
        if isinstance(members, list) and members:
            lines.append("  Members:")
            for m in members:
                lines.append(f"    - {m.get('name')} : {m.get('type')}")
        value = definition.get("value")
        if value is not None:
            lines.append(f"  Value: {value}")
        value_expr = definition.get("value_expr")
        if value_expr is not None:
            lines.append(f"  Value: {value_expr}")
    elif definition.get("type") in ("function", "struct", "enum", "macro"):
        lines.append("  (semantic detail unavailable: run `agentx plan` to reindex)")
    build = result.get("build") or {}
    if build:
        lines.append("")
        lines.append("Build:")
        lines.append(f"  compile_status: {build.get('compile_status')}")
        if build.get("target"):
            lines.append(f"  target: {build['target']}")
    lines.append("")
    lines.append("[Callers]")
    callers = result.get("callers") or []
    if callers:
        for c in callers:
            lines.append(f"  {c}()")
    else:
        lines.append("  (none)")
    lines.append("")
    lines.append("[Callees]")
    callees = result.get("callees") or []
    if callees:
        for c in callees:
            lines.append(f"  {c}()")
    else:
        lines.append("  (none)")
    related = result.get("related_files") or []
    if related:
        lines.append("")
        lines.append("[Related Files]")
        for f in related:
            lines.append(f"  {f}")
    lines.append("")
    lines.append(f"Confidence: {result.get('confidence', 'unknown')}")
    return "\n".join(lines)


def _file_label(f: str) -> str:
    return _basename(f) if f else ""
