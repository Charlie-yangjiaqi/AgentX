"""Type Semantic 组装层（Phase 7.7.4）：数据模型级理解事实。

输入（全部来自 AST/parser，LLM 零参与）：
- FileSemantics（structs/enums/macros/field_usage/bindings）
- index.symbols（函数符号，indirect_calls 联动用）
- 旧 type_semantics（content_hash stale 复用）

输出：type_semantics（独立事实层，不污染 symbols）：
{
  "structs": [{"name", "file", "line", "content_hash", "fields": [
      {"name", "type", "line", "is_function_pointer", "registered": [函数名]}]}],
  "enums":  [{"name", "file", "line", "content_hash", "members": [
      {"name", "value"|null, "line"}]}],
  "macros": [{"name", "kind", "value"|"value_expr", "file", "line", "content_hash"}],
  "struct_usage": {"field_name": {"read_by": [函数], "write_by": [函数]}}
}

原则：
- 事实优先：字段/enum value/macro value 全部来自源码原文，不推断
- 保守：enum value 无法确定 → null；字段类型无法确定 → 原样
- 增量：条目带来源文件 content_hash，变化才重算（不绑全局 fingerprint）
- 联动：函数指针字段 ↔ indirect_calls（字段级注册关系）
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from agentx.semantic.types import FileSemantics


def _file_hash(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()[:16]
    except OSError:
        return ""


def build_type_semantics(
    project_root: Path,
    semantics: list[FileSemantics],
    symbols: list[dict[str, Any]],
    indirect_calls: list[dict[str, Any]],
    old: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """组装 type_semantics（增量：文件 content_hash 未变则复用旧条目）。

    old：上一次的 type_semantics（index 重建时传入，未变文件条目保留）。
    """
    old = old or {}
    old_structs = {
        (str(s.get("file", "")), str(s.get("name", ""))): s for s in old.get("structs", []) if s
    }
    old_enums = {
        (str(e.get("file", "")), str(e.get("name", ""))): e for e in old.get("enums", []) if e
    }
    old_macros = {
        (str(m.get("file", "")), str(m.get("name", ""))): m for m in old.get("macros", []) if m
    }

    structs: list[dict[str, Any]] = []
    enums: list[dict[str, Any]] = []
    macros: list[dict[str, Any]] = []
    usage_entries: list[dict[str, Any]] = []

    for sem in semantics:
        h = _file_hash(project_root / sem.functions[0].file) if sem.functions else None
        # 文件 hash（用任一文件路径；semantics 按文件组织，取第一个函数/结构体路径）
        file_of = ""
        if sem.structs:
            file_of = sem.structs[0].file
        elif sem.enums:
            file_of = sem.enums[0].file
        elif sem.macros:
            file_of = sem.macros[0].file
        elif sem.field_usage:
            file_of = str(sem.field_usage[0].get("file", ""))
        if not file_of:
            continue
        h = _file_hash(project_root / file_of)

        # 逐条目 stale 复用：(file, name) 键 + 文件 hash 一致 → 保留旧条目
        for st in sem.structs:
            old_s = old_structs.get((st.file, st.name))
            if old_s and old_s.get("content_hash") == h:
                # 文件未变：复用旧条目，但 registered 是跨来源联动
                # （依赖 indirect_calls，可能独立更新）→ 始终重算
                reused = dict(old_s)
                reused["fields"] = [
                    {
                        **f,
                        "registered": _registered_callbacks(
                            st.name, f.get("name", ""), st.file, indirect_calls
                        ),
                    }
                    for f in old_s.get("fields", [])
                ]
                structs.append(reused)
                continue
            fields = [
                {
                    "name": m.name,
                    "type": m.type,
                    "line": m.line,
                    "is_function_pointer": m.is_function_pointer,
                    "registered": _registered_callbacks(st.name, m.name, st.file, indirect_calls),
                }
                for m in st.members
            ]
            structs.append(
                {
                    "name": st.name,
                    "file": st.file,
                    "line": st.start_line,
                    "content_hash": h,
                    "fields": fields,
                }
            )

        for en in sem.enums:
            old_e = old_enums.get((en.file, en.name))
            if old_e and old_e.get("content_hash") == h:
                enums.append(old_e)
                continue
            members = [
                {
                    "name": m.name,
                    "value": m.value,
                    "value_expr": m.value_expr,
                    "line": m.line,
                }
                for m in en.members
            ]
            enums.append(
                {
                    "name": en.name,
                    "file": en.file,
                    "line": en.start_line,
                    "content_hash": h,
                    "members": members,
                }
            )

        for m in sem.macros:
            old_m = old_macros.get((m.file, m.name))
            if old_m and old_m.get("content_hash") == h:
                macros.append(old_m)
                continue
            entry: dict[str, Any] = {
                "name": m.name,
                "kind": m.kind,
                "file": m.file,
                "line": m.line,
                "content_hash": h,
            }
            if m.value is not None:
                entry["value"] = m.value
            if m.value_expr is not None:
                entry["value_expr"] = m.value_expr
            macros.append(entry)

        usage_entries.extend(sem.field_usage)

    struct_usage = _aggregate_usage(usage_entries)
    return {
        "structs": structs,
        "enums": enums,
        "macros": macros,
        "struct_usage": struct_usage,
    }


def _registered_callbacks(
    struct_name: str, field_name: str, file: str, indirect_calls: list[dict[str, Any]]
) -> list[str]:
    """字段级联动：indirect_calls 中绑定到该 struct 字段的函数。

    匹配：via=field_assign 且 field == field_name 且文件 stem 相同
    （struct 定义常在 .h，绑定在对应 .c——touch.h ↔ touch.c 配对；
    不做类型推断，字段名+文件族匹配，保守。）
    """
    stem = Path(file).stem
    out: list[str] = []
    for e in indirect_calls:
        if e.get("via") != "field_assign" or e.get("field") != field_name:
            continue
        efile = str(e.get("file", ""))
        if Path(efile).stem != stem:
            continue
        callee = str(e.get("callee", ""))
        if callee and callee not in out:
            out.append(callee)
    return out


def _aggregate_usage(entries: list[dict[str, Any]]) -> dict[str, Any]:
    """字段使用聚合（字段名级，不做类型推断）：
    {"field_name": {"read_by": [函数], "write_by": [函数]}}"""
    out: dict[str, dict[str, Any]] = {}
    for e in entries:
        field = str(e.get("field", ""))
        if not field:
            continue
        fn = e.get("function")
        fn_name = str(fn) if fn else "(top-level)"
        access = str(e.get("access", "read"))
        agg = out.setdefault(field, {"read_by": [], "write_by": []})
        key = "write_by" if access == "write" else "read_by"
        if fn_name not in agg[key]:
            agg[key].append(fn_name)
    return out


def format_type_view(
    ts: dict[str, Any],
    struct_names: list[str] | None = None,
) -> str:
    """类型语义视图（Planner/查询展示；ASCII 安全）。"""
    lines: list[str] = []
    for st in ts.get("structs", []):
        if struct_names and st.get("name") not in struct_names:
            continue
        lines.append(f"struct {st.get('name')} @ {st.get('file')}:{st.get('line')}")
        for f in st.get("fields", []):
            fp = " (function pointer)" if f.get("is_function_pointer") else ""
            regs = f.get("registered") or []
            reg_note = f" 回调绑定: {', '.join(regs[:5])}" if regs else ""
            usage = ts.get("struct_usage", {}).get(f.get("name"), {})
            readers = usage.get("read_by", [])
            writers = usage.get("write_by", [])
            use_note = ""
            if readers or writers:
                use_note = f" 使用: 读={len(readers)}函数 写={len(writers)}函数"
            lines.append(f"  - {f.get('name')}: {f.get('type')}{fp}{reg_note}{use_note}")
    if not lines and struct_names:
        return ""
    return "类型语义（Type Semantic）:\n" + "\n".join(lines) if lines else ""
