"""Semantic 结果与 CodeGraph symbols 的合并（Phase 7.6）。

原则：CodeGraph 负责项目级关系（symbol 定位/调用边/include），Tree-sitter
只补充文件级语法语义（signature/members/value）。merge 不覆盖 CodeGraph
可信字段，只补空值；macro 等 CodeGraph 不收录的符号直接追加。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from agentx.semantic.types import EnumInfo, FileSemantics, FunctionInfo, MacroInfo, StructInfo

_SOURCE_EXTS = {".c", ".h", ".cpp", ".hpp", ".cc", ".cxx", ".s", ".S"}


def merge_semantics(
    symbols: list[dict[str, Any]], project_root: Path, source_files: list[str]
) -> tuple[list[dict[str, Any]], list[str], list[dict[str, Any]], list[FileSemantics]]:
    """对源码文件提取语义并合并进 symbols。

    返回 (合并后的 symbols, semantic 错误列表, indirect_calls, semantics)。
    单文件解析失败只记录错误，不影响其他文件。

    Phase 7.8.1：worker_mode=true 时解析在子进程隔离执行
    （native crash 不杀死主进程）；默认进程内（每文件独立 Parser）。

    Phase 7.7.3：bindings（语法层函数地址绑定）用 index.symbols 过滤——
    仅右值标识符 ∈ symbols 且 type=function 的条目进入 indirect_calls
    （函数注册/绑定事实，不承诺真实调用路径）。

    Phase 7.7.4：semantics 返回完整 FileSemantics（含 struct 函数指针标记/
    宏分类/字段使用），供 type_extractor 组装 type_semantics（不进 symbols）。
    """
    from agentx.config.config import load_config, resolve_semantic_config
    from agentx.semantic.worker import run_jobs_isolated

    sem_cfg = resolve_semantic_config(load_config())
    jobs: list[tuple[str, str]] = []
    for rel in sorted(source_files):
        if Path(rel).suffix.lower() not in _SOURCE_EXTS:
            continue
        path = project_root / rel
        try:
            source = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue  # 读取失败的文件直接跳过（semantic 层不阻塞）
        jobs.append((rel, source))

    errors: list[str] = []
    semantic_symbols: list[dict[str, Any]] = []
    bindings: list[dict[str, Any]] = []
    semantics: list[FileSemantics] = []
    if sem_cfg["worker_mode"] and len(jobs) >= 2:
        results, worker_errors = run_jobs_isolated(
            jobs, timeout_seconds=sem_cfg["worker_timeout_seconds"]
        )
        errors.extend(worker_errors)
        semantics = results
    else:
        from agentx.semantic.extractor import extract_file_semantics

        results = []
        for rel, source in jobs:
            try:
                results.append(extract_file_semantics(rel, source))
            except Exception as e:  # 单文件解析异常不阻断
                errors.append(f"{rel}: {type(e).__name__}: {e}")
                results.append(FileSemantics())
        semantics = results
    for sem in results:
        errors.extend(sem.errors)
        semantic_symbols.extend(_to_symbols(sem))
        bindings.extend(sem.bindings)

    indirect_calls = _filter_indirect_calls(bindings, symbols)
    merged = _merge(symbols, semantic_symbols)
    return merged, errors, indirect_calls, semantics


def _filter_indirect_calls(
    bindings: list[dict[str, Any]], symbols: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """bindings → indirect_calls：右值标识符 ∈ symbols 且 type=function。

    每条保留：callee / via / file / line / caller_hint / confidence=high。
    语义：函数被注册/绑定到此处（字段/表项/注册 API），不承诺真实调用。
    """
    fn_by_name: dict[str, dict[str, Any]] = {}
    for s in symbols:
        if str(s.get("type", "")) == "function":
            fn_by_name[str(s.get("name", ""))] = s

    out: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str, int]] = set()
    for b in bindings:
        name = str(b.get("name", ""))
        if name not in fn_by_name:
            continue  # 非函数符号（变量/宏/未收录）不产生间接边
        key = (name, str(b.get("via", "")), str(b.get("file", "")), int(b.get("line", 0)))
        if key in seen:
            continue
        seen.add(key)
        out.append(
            {
                "callee": name,
                "via": str(b.get("via", "var_assign")),
                "file": str(b.get("file", "")),
                "line": int(b.get("line", 0)),
                "caller_hint": b.get("caller_hint"),
                "confidence": "high",
                # Phase 7.7.4：field_assign 绑定的字段名（struct 联动用）
                "field": str(b.get("field", "")),
            }
        )
    out.sort(key=lambda e: (e["file"], e["line"]))
    return out


def _to_symbols(sem: FileSemantics) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for fn in sem.functions:
        result.append(_function_symbol(fn))
    for st in sem.structs:
        result.append(_struct_symbol(st))
    for en in sem.enums:
        result.append(_enum_symbol(en))
    # Phase 7.7.4：函数宏/条件编译标志宏不进 symbols（保持原始事实纯净），
    # 完整分类见 type_semantics（type_extractor）
    for macro in sem.macros:
        if macro.kind == "constant":
            result.append(_macro_symbol(macro))
    return result


def _function_symbol(fn: FunctionInfo) -> dict[str, Any]:
    sym: dict[str, Any] = {
        "name": fn.name,
        "qualified_name": fn.name,
        "type": "function",
        "file": fn.file,
        "start_line": fn.start_line,
        "end_line": fn.end_line,
        "semantic": True,
    }
    if fn.signature is not None:
        sym["signature"] = {
            "return_type": fn.signature.return_type,
            "parameters": [{"name": p.name, "type": p.type} for p in fn.signature.parameters],
            "text": fn.signature.text,
        }
    else:
        sym["signature"] = None  # 已尝试解析但语法复杂
    return sym


def _struct_symbol(st: StructInfo) -> dict[str, Any]:
    return {
        "name": st.name,
        "qualified_name": st.name,
        "type": "struct",
        "file": st.file,
        "start_line": st.start_line,
        "end_line": st.end_line,
        "semantic": True,
        "members": [{"name": m.name, "type": m.type, "line": m.line} for m in st.members],
    }


def _enum_symbol(en: EnumInfo) -> dict[str, Any]:
    members = []
    for m in en.members:
        entry: dict[str, Any] = {"name": m.name, "line": m.line}
        if m.value is not None:
            entry["value"] = m.value
        if m.value_expr is not None:
            entry["value_expr"] = m.value_expr
        members.append(entry)
    return {
        "name": en.name,
        "qualified_name": en.name,
        "type": "enum",
        "file": en.file,
        "start_line": en.start_line,
        "end_line": en.end_line,
        "semantic": True,
        "members": members,
    }


def _macro_symbol(macro: MacroInfo) -> dict[str, Any]:
    sym: dict[str, Any] = {
        "name": macro.name,
        "qualified_name": macro.name,
        "type": "macro",
        "file": macro.file,
        "start_line": macro.line,
        "end_line": macro.line,
        "semantic": True,
    }
    if macro.value is not None:
        sym["value"] = macro.value
    if macro.value_expr is not None:
        sym["value_expr"] = macro.value_expr
    return sym


def _merge(existing: list[dict[str, Any]], semantic: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """CodeGraph symbols 为主，semantic 补字段/追加新符号。

    - (file, name, type) 匹配：semantic 字段覆盖空值（signature/members/value），
      其余 CodeGraph 字段保留
    - 未匹配：作为新 symbol 追加（semantic=true）
    """
    by_key: dict[tuple[str, str, str], int] = {}
    for i, sym in enumerate(existing):
        by_key[(str(sym.get("file", "")), str(sym.get("name", "")), str(sym.get("type", "")))] = i

    appended: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for sem in semantic:
        key = (str(sem.get("file", "")), str(sem.get("name", "")), str(sem.get("type", "")))
        if key in by_key:
            idx = by_key[key]
            for field in ("signature", "members", "value", "value_expr"):
                if field in sem and existing[idx].get(field) in (None, ""):
                    existing[idx][field] = sem[field]
            existing[idx]["semantic"] = True
        elif key not in seen:
            appended.append(sem)
            seen.add(key)
    return existing + appended
