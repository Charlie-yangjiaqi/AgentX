"""Index Quality Report（Index Pipeline Reliability）。

每次 enrich 后输出质量报告：semantic 覆盖率 / module 可信度 / scope / 评分。
防止"Index 生成成功但质量 C"的静默退化——成功 ≠ 可用，必须可量化。
"""

from __future__ import annotations

from typing import Any

from agentx.index.index import ProjectIndex


def count_invalid_tokens(symbols: list[dict[str, Any]]) -> int:
    """污染统计：含非法字符（( * : 空格 等）的 token 符号数。

    函数指针类型（(*TASKFUNCTION）/ typedef 字符串等——module discovery
    会拒绝它们作为模块名。短前缀（g_state → "g"）只是无意义，不算污染。
    """
    n = 0
    for s in symbols:
        tok = str(s.get("name", "")).split("_", 1)[0].casefold()
        if tok and not tok.replace("_", "").isalnum():
            n += 1
    return n


def compute_quality(index: ProjectIndex) -> dict[str, Any]:
    """计算 Index 质量报告（全部基于 Index 事实，零 LLM）。"""
    symbols = index.symbols
    funcs = [s for s in symbols if s.get("type") == "function"]
    sig = sum(
        1
        for s in funcs
        if isinstance(s.get("signature"), dict)
        and (s["signature"].get("text") or s["signature"].get("parameters"))
    )
    structs = [s for s in symbols if s.get("type") == "struct"]
    structs_mem = sum(1 for s in structs if s.get("members"))
    enums = [s for s in symbols if s.get("type") == "enum"]
    enums_val = sum(1 for s in enums if s.get("members"))
    macros = sum(
        1
        for s in symbols
        if s.get("type") == "macro"
        and (s.get("value") is not None or s.get("value_expr") is not None)
    )

    caps = index.capabilities or {}
    sem = caps.get("semantic") or {}
    sem_enabled = sem.get("enabled") is True

    def _ratio(got: int, total: int) -> float:
        if total == 0:
            return 1.0 if sem_enabled else 0.0
        return got / total

    func_ratio = _ratio(sig, len(funcs))
    struct_ratio = _ratio(structs_mem, len(structs))
    enum_ratio = _ratio(enums_val, len(enums))
    rejected = count_invalid_tokens(symbols)

    if not sem_enabled:
        grade = "C"
    else:
        worst = min(func_ratio, struct_ratio, enum_ratio)
        if worst >= 0.95:
            grade = "A"
        elif worst >= 0.85:
            grade = "A-"
        elif worst >= 0.7:
            grade = "B+"
        elif worst >= 0.5:
            grade = "B"
        else:
            grade = "C"
        if rejected and grade in ("A", "A-"):
            grade = "B+"  # 大量污染 token：降级

    return {
        "semantic": "enabled" if sem_enabled else "disabled",
        "semantic_reason": sem.get("reason"),
        "parser": sem.get("parser"),
        "functions": len(funcs),
        "functions_with_signature": sig,
        "structs": len(structs),
        "structs_with_members": structs_mem,
        "enums": len(enums),
        "enums_with_values": enums_val,
        "macros": macros,
        "modules": len(index.modules),
        "rejected_tokens": rejected,
        "grade": grade,
    }


def compute_scope_report(index: ProjectIndex) -> dict[str, Any]:
    """Scope Report（Phase 7.8 三层统计）：project/third_party 文件与符号 + 模块分类。"""
    files = index.files
    project_files = [f for f in files if getattr(f, "scope_type", "project") == "project"]
    third_files = [f for f in files if getattr(f, "scope_type", "project") == "third_party"]

    third_by_name: dict[str, dict[str, Any]] = {}
    for f in third_files:
        name = str(f.scope_name or f.path.split("/")[0])
        entry = third_by_name.setdefault(name, {"files": 0, "symbols": 0})
        entry["files"] += 1
    for s in index.symbols:
        if s.get("scope_type") == "third_party":
            name = str(s.get("scope_name") or str(s.get("file", "")).split("/")[0])
            entry = third_by_name.setdefault(name, {"files": 0, "symbols": 0})
            entry["symbols"] += 1

    project_symbols = sum(1 for s in index.symbols if s.get("scope_type") != "third_party")
    third_symbols = sum(e["symbols"] for e in third_by_name.values())

    modules = index.modules or []
    project_modules = [m for m in modules if m.get("scope_type") != "third_party"]
    third_modules = [m for m in modules if m.get("scope_type") == "third_party"]

    return {
        "project_files": len(project_files),
        "project_symbols": project_symbols,
        "third_party": {name: entry for name, entry in sorted(third_by_name.items())},
        "third_party_files": len(third_files),
        "third_party_symbols": third_symbols,
        "project_modules": len(project_modules),
        "third_party_modules": len(third_modules),
        # 检查项：third_party 模块数 > 第三方目录数 = 冻结失败
        "third_party_module_overflow": len(third_modules) > len(third_by_name),
    }


def format_quality_report(quality: dict[str, Any]) -> str:
    """质量报告文本（ASCII 安全）。"""
    lines = ["AgentX Index Quality Report"]
    lines.append(f"  Semantic: {quality['semantic']}")
    if quality.get("parser"):
        lines.append(f"  Parser: {quality['parser']}")
    if quality.get("semantic_reason"):
        lines.append(f"  Reason: {quality['semantic_reason']}")
    lines.append(
        f"  Functions: {quality['functions']} "
        f"(with signature: {quality['functions_with_signature']})"
    )
    lines.append(
        f"  Struct: {quality['structs']} (with members: {quality['structs_with_members']})"
    )
    lines.append(f"  Enum: {quality['enums']} (with values: {quality['enums_with_values']})")
    lines.append(f"  Macro: {quality['macros']}")
    lines.append(
        f"  Module: valid {quality['modules']}, rejected tokens: {quality['rejected_tokens']}"
    )
    lines.append(f"  Quality: {quality['grade']}")
    return "\n".join(lines)


def format_scope_report(scope: dict[str, Any]) -> str:
    """Scope Report 文本（Phase 7.8 三层统计 + 检查项）。"""
    lines = ["Scope Report"]
    lines.append(
        f"  Project: files={scope['project_files']} symbols={scope['project_symbols']} "
        f"modules={scope['project_modules']}"
    )
    tp = scope.get("third_party") or {}
    for name, entry in tp.items():
        lines.append(f"  Third Party {name}: files={entry['files']} symbols={entry['symbols']}")
    lines.append(
        f"  Third Party total: files={scope['third_party_files']} "
        f"symbols={scope['third_party_symbols']} modules={scope['third_party_modules']}"
    )
    if scope.get("third_party_module_overflow"):
        lines.append("  [WARN] third_party 模块数 > 第三方目录数（冻结失败，请检查 Scope 配置）")
    return "\n".join(lines)
