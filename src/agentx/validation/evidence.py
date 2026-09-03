"""Phase 7.9 Evidence Collection：修改方案的事实依据收集（零 LLM，确定性）。

证据只允许来自 Index（禁止 LLM 判断，不能让 AI 验证自己）：

Direct Evidence（强）：
- symbol definition：符号在 Index 中定义（symbols / type_semantics）
- call_graph：调用关系（called_by / calls）
- indirect_calls：注册/绑定关系（registered_by；不承诺真实调用——7.7.3 语义）
- struct_usage：字段读写（read_by / write_by）
- include dependency：头文件被包含（included_by）
- consumers：模块依赖/被依赖（module 层事实）

Weak Evidence（弱，不能单独支撑修改）：
- 文件名匹配（路径尾段相似）
- 符号名匹配（大小写/子串相似）
- responsibility inference（模块职责描述，理解层推断——7.8 约定仅推断）
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from agentx.index.index import ProjectIndex
from agentx.query.module_query import module_of_file

# 修改操作：add 允许目标尚不存在（新建接口/文件）
_ADD_OPS = {"add", "new", "create"}


def norm_path(path: str) -> str:
    """路径归一化：反斜杠 → 斜杠、小写、去 ./ 前缀（对比用）。"""
    return str(path).replace("\\", "/").lstrip("./").lower()


def _basename(path: str) -> str:
    return Path(str(path).replace("\\", "/")).name


class EvidenceItem:
    """一条证据：kind=direct（强）| weak（弱）。"""

    __slots__ = ("source", "description", "kind")

    def __init__(self, source: str, description: str, kind: str = "direct") -> None:
        self.source = source
        self.description = description
        self.kind = kind

    def as_dict(self) -> dict[str, str]:
        return {"source": self.source, "description": self.description, "kind": self.kind}


class ChangeEvidence:
    """单个修改点的验证证据集合。"""

    def __init__(self, change: dict[str, Any]) -> None:
        self.change = change
        self.file_found = False
        self.file_meta: dict[str, Any] | None = None
        self.file_exact = False
        self.symbol_found = False
        self.symbol_meta: dict[str, Any] | None = None
        self.direct: list[EvidenceItem] = []
        self.weak: list[EvidenceItem] = []
        self.propagation: list[str] = []
        self.status: str = "pass"  # pass | warning | block
        self.reasons: list[str] = []
        self.rule_hits: list[str] = []

    def as_dict(self) -> dict[str, Any]:
        return {
            "file": self.change.get("file", ""),
            "symbol": self.change.get("symbol", ""),
            "operation": self.change.get("operation", "modify"),
            "status": self.status,
            "file_found": self.file_found,
            "symbol_found": self.symbol_found,
            "direct": [e.as_dict() for e in self.direct],
            "weak": [e.as_dict() for e in self.weak],
            "propagation": self.propagation,
            "reasons": self.reasons,
            "rule_hits": self.rule_hits,
        }


def find_file(
    index: ProjectIndex, raw: str
) -> tuple[dict[str, Any] | None, bool]:
    """在 Index 中定位文件：精确匹配（exact=True）或路径尾段匹配（弱）。

    返回 (file_meta | None, exact)。
    """
    target = norm_path(raw)
    if not target:
        return None, False
    base = _basename(target).lower()
    exact = None
    tail: dict[str, Any] | None = None
    for f in index.files:
        p = norm_path(f.path)
        if p == target:
            exact = {"path": f.path, "status": f.status}
        elif tail is None and p.endswith("/" + base):
            tail = {"path": f.path, "status": f.status}
    if exact is not None:
        return exact, True
    return tail, False


def find_symbol(
    index: ProjectIndex, name: str, kind_hint: str = ""
) -> dict[str, Any] | None:
    """Index 中精确定位符号（symbols 表优先；type_semantics 兜底）。"""
    target = name.strip()
    if not target:
        return None
    for s in index.symbols:
        if str(s.get("name", "")) == target:
            return {
                "name": s.get("name"),
                "type": s.get("type", ""),
                "file": s.get("file", ""),
                "start_line": s.get("start_line"),
            }
    ts = index.type_semantics or {}
    for st in ts.get("structs", []):
        if str(st.get("name", "")) == target:
            return _semantic_hit("struct", st)
    for en in ts.get("enums", []):
        if str(en.get("name", "")) == target:
            return _semantic_hit("enum", en)
    for m in ts.get("macros", []):
        if str(m.get("name", "")) == target:
            return _semantic_hit("macro", m)
    return None


def _semantic_hit(kind: str, item: dict[str, Any]) -> dict[str, Any]:
    """把 type_semantics 条目归一为符号命中结果。"""
    return {
        "name": item.get("name"),
        "type": kind,
        "file": item.get("file"),
        "start_line": item.get("line"),
    }


def _weak_symbol_match(index: ProjectIndex, name: str) -> dict[str, Any] | None:
    """符号名模糊匹配（大小写不敏感；不足 4 字符不匹配，避免误配）。"""
    target = name.strip().lower()
    if len(target) < 4:
        return None
    for s in index.symbols:
        n = str(s.get("name", "")).lower()
        if n == target:
            continue  # 精确匹配已在上层处理
        if n and (n.endswith(target) or target.endswith(n)):
            return {
                "name": s.get("name"),
                "type": s.get("type", ""),
                "file": s.get("file", ""),
                "weak": True,
            }
    return None


def _struct_field_usage(
    index: ProjectIndex, field: str
) -> tuple[dict[str, Any] | None, list[str], list[str]]:
    """字段级事实：字段名命中 struct members 或 struct_usage → 定义 + 读写函数。

    定义与聚合必须同时保留：字段可同时出现在 struct_usage（聚合 key）与
    structs[].fields（定义）。只取其一（旧实现命中 agg 即 return）会丢失
    field definition 或 usage 证据 → 误判 weak/block（Phase 7.9 issue A）。
    """
    ts = index.type_semantics or {}
    usage = ts.get("struct_usage", {}) or {}
    agg = usage.get(field) or {}
    readers = list(agg.get("read_by", []) or [])
    writers = list(agg.get("write_by", []) or [])
    fdef: dict[str, Any] | None = None
    for st in ts.get("structs", []):
        for f in st.get("fields", []):
            if str(f.get("name", "")) == field:
                fdef = {"struct": st.get("name"), "file": st.get("file"), "line": f.get("line")}
                break
        if fdef is not None:
            break
    if fdef is not None or readers or writers:
        return fdef, readers, writers
    return None, [], []


def _file_functions(index: ProjectIndex, path: str) -> set[str]:
    """文件内函数符号名集合。"""
    target = norm_path(path)
    return {
        str(s.get("name", ""))
        for s in index.symbols
        if str(s.get("type", "")) == "function" and norm_path(str(s.get("file", ""))) == target
    }


def collect_change_evidence(
    index: ProjectIndex,
    change: dict[str, Any],
    responsibilities: dict[str, Any] | None = None,
) -> ChangeEvidence:
    """收集单个修改点的全部证据（Direct + Weak），不判定状态（validator 负责）。"""
    ev = ChangeEvidence(change)
    file_raw = str(change.get("file", "")).strip()
    symbol = str(change.get("symbol", "")).strip()
    op = str(change.get("operation", "modify")).lower()
    is_add = op in _ADD_OPS

    # ---- Rule 1: 目标必须存在于 Index（add 例外：新建目标允许不存在） ----
    if file_raw:
        meta, exact = find_file(index, file_raw)
        ev.file_meta = meta
        ev.file_found = meta is not None
        ev.file_exact = exact
    if symbol and not is_add:
        smeta = find_symbol(index, symbol)
        if smeta is None:
            # 字段级：字段名（struct member / struct_usage key）也算定义
            fdef, readers, writers = _struct_field_usage(index, symbol)
            if fdef is not None or readers or writers:
                ev.symbol_found = True
                ev.symbol_meta = {"type": "field", **fdef} if fdef else {"type": "field"}
            else:
                ev.symbol_found = False
        else:
            ev.symbol_found = True
            ev.symbol_meta = smeta

    # ---- Direct: symbol definition ----
    if ev.symbol_found and ev.symbol_meta is not None:
        sm = ev.symbol_meta
        loc = f"{sm.get('file', '')}:{sm.get('start_line', '')}"
        if sm.get("type") == "field":
            ev.direct.append(EvidenceItem("symbols", f"字段 {symbol} 定义于 {loc}"))
            ev.rule_hits.append("rule2")
        else:
            ev.direct.append(
                EvidenceItem(
                    "symbols", f"符号 {symbol} 定义于 {loc}（type={sm.get('type', '?')}）"
                )
            )
            ev.rule_hits.append("rule2" if sm.get("type") == "function" else "rule3")

    # ---- Direct: call_graph（调用关系） ----
    calls = index.call_graph
    if symbol:
        callers = sorted(
            {str(e.get("caller", "")) for e in calls if str(e.get("callee", "")) == symbol}
        )
        callees = sorted(
            {str(e.get("callee", "")) for e in calls if str(e.get("caller", "")) == symbol}
        )
        if callers:
            ev.direct.append(
                EvidenceItem("call_graph", f"{symbol} 被 {', '.join(callers[:5])} 调用")
            )
            ev.rule_hits.append("rule2")
        if callees:
            ev.direct.append(
                EvidenceItem("call_graph", f"{symbol} 调用 {', '.join(callees[:5])}")
            )
            ev.rule_hits.append("rule2")
    elif ev.file_found and ev.file_exact:
        fns = _file_functions(index, file_raw)
        if fns:
            edge_fns = {
                str(e.get("caller", ""))
                for e in calls
                if str(e.get("caller", "")) in fns or str(e.get("callee", "")) in fns
            }
            if edge_fns:
                joined = ", ".join(sorted(edge_fns)[:3])
                ev.direct.append(
                    EvidenceItem(
                        "call_graph",
                        f"文件内 {len(edge_fns)} 个函数参与调用关系（如 {joined}）",
                    )
                )
                ev.rule_hits.append("rule2")

    # ---- Direct: indirect_calls（注册/绑定；registered_by，不承诺调用） ----
    indirect = index.indirect_calls or []
    if symbol:
        regs = [e for e in indirect if str(e.get("callee", "")) == symbol]
        if regs:
            spots = []
            for e in regs[:5]:
                owner = str(e.get("caller_hint", "") or "")
                spot = str(e.get("file", ""))
                spots.append(f"{owner}@{spot}" if owner else spot)
            ev.direct.append(
                EvidenceItem(
                    "indirect_calls",
                    f"{symbol} 被注册/绑定: {', '.join(spots)}（注册关系，非调用）",
                )
            )
            ev.rule_hits.append("rule2")
    elif ev.file_found:
        regs = [e for e in indirect if norm_path(str(e.get("file", ""))) == norm_path(file_raw)]
        if regs:
            callees = sorted({str(e.get("callee", "")) for e in regs})
            ev.direct.append(
                EvidenceItem(
                    "indirect_calls",
                    f"文件内注册/绑定 {len(callees)} 个回调（如 {', '.join(callees[:3])}）",
                )
            )
            ev.rule_hits.append("rule2")

    # ---- Direct: struct_usage（字段读写） ----
    usage = (index.type_semantics or {}).get("struct_usage", {}) or {}
    if symbol:
        fdef, readers, writers = _struct_field_usage(index, symbol)
        if fdef is not None or readers or writers:
            if readers:
                joined = ", ".join(readers[:5])
                ev.direct.append(
                    EvidenceItem("struct_usage", f"字段 {symbol} 被读: {joined}")
                )
            if writers:
                joined = ", ".join(writers[:5])
                ev.direct.append(
                    EvidenceItem("struct_usage", f"字段 {symbol} 被写: {joined}")
                )
            if readers or writers:
                ev.rule_hits.append("rule3")
        elif ev.symbol_meta and ev.symbol_meta.get("type") == "struct":
            fields = [f for f in usage if f]
            used_fields = [
                f
                for f in fields
                if any(
                    str(w) == symbol
                    for w in usage[f].get("read_by", []) + usage[f].get("write_by", [])
                )
            ]
            if used_fields:
                msg = f"struct {symbol} 字段被 {len(used_fields)} 处读写"
                ev.direct.append(EvidenceItem("struct_usage", msg))
                ev.rule_hits.append("rule3")
    elif ev.file_found and ev.file_exact:
        fns = _file_functions(index, file_raw)
        used_fields = [
            f
            for f, agg in usage.items()
            if any(fn in agg.get("read_by", []) or fn in agg.get("write_by", []) for fn in fns)
        ]
        if used_fields:
            ev.direct.append(
                EvidenceItem("struct_usage", f"文件内函数读写字段: {', '.join(used_fields[:5])}")
            )
            ev.rule_hits.append("rule3")

    # ---- Direct: include dependency（头文件被包含） ----
    include_map = index.include_map or {}
    if ev.file_found and ev.file_exact and file_raw.lower().endswith((".h", ".hpp")):
        includers = include_map.get(file_raw, []) or include_map.get(norm_path(file_raw), [])
        if not includers:
            for key, vals in include_map.items():
                if norm_path(key) == norm_path(file_raw):
                    includers = vals
                    break
        if includers:
            joined = ", ".join(str(i) for i in includers[:5])
            ev.direct.append(
                EvidenceItem("dependencies", f"头文件被包含: {joined}")
            )
            ev.rule_hits.append("rule4")

    # ---- Direct: consumers（模块依赖方；需符号锚点，模块消费不能单独支撑文件级修改） ----
    if ev.file_meta is not None and ev.symbol_found:
        mod = module_of_file(index, str(ev.file_meta.get("path", "")))
        if mod is not None:
            consumers = mod.get("consumers") or []
            if consumers:
                ev.direct.append(
                    EvidenceItem("consumers", f"模块 {mod['name']} 被 {len(consumers)} 个模块依赖")
                )
                ev.rule_hits.append("rule2")

    # ---- Weak: 文件存在性（只能作为 weak——存在 ≠ 修改合理，Phase 7.9 issue C） ----
    if file_raw and ev.file_meta is not None:
        if ev.file_exact:
            ev.weak.append(
                EvidenceItem("file_exists", f"文件存在（精确匹配）: {ev.file_meta['path']}", "weak")
            )
        else:
            ev.weak.append(
                EvidenceItem("naming", f"文件路径匹配（尾段相似）: {ev.file_meta['path']}", "weak")
            )

    # ---- Weak: 符号名匹配 ----
    if symbol and not ev.symbol_found:
        wm = _weak_symbol_match(index, symbol)
        if wm is not None:
            ev.weak.append(
                EvidenceItem("naming", f"符号名相似: {wm['name']}（{wm.get('type', '?')}）", "weak")
            )

    # ---- Weak: responsibility inference（理解层，仅推断） ----
    if ev.symbol_found and ev.symbol_meta is not None and ev.file_meta is not None:
        mod = module_of_file(index, str(ev.file_meta.get("path", "")))
        if mod is not None and responsibilities:
            entry = responsibilities.get(str(mod.get("name", ""))) or {}
            resp = str(entry.get("responsibility", ""))
            if resp:
                ev.weak.append(
                    EvidenceItem(
                        "responsibility",
                        f"模块职责: {resp[:60]}（理解层推断，非事实）",
                        "weak",
                    )
                )

    return ev


def evidence_summary(ev: ChangeEvidence) -> str:
    """单条修改点的验证展示（ASCII 安全）。"""
    lines = [f"修改: {ev.change.get('file', '')}"]
    if ev.change.get("symbol"):
        lines.append(f"符号: {ev.change['symbol']}（{ev.change.get('operation', 'modify')}）")
    for e in ev.direct:
        lines.append(f"  FACT: [{e.source}] {e.description}")
    for e in ev.weak:
        lines.append(f"  WEAK: [{e.source}] {e.description}")
    return "\n".join(lines)
