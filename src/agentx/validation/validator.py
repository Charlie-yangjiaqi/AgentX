"""Phase 7.9 Validator：Plan 修改点是否有工程事实支撑（纯规则，零 LLM）。

三级判定（整单）：
- PASS:    每个修改点都有 Direct 证据
- WARNING: 无 BLOCK，但存在只有 Weak 证据的修改点（输出 + 建议人工确认）
- BLOCK:   任一修改点违反强制规则 → 拦截（一次 LLM 修正 → 仍 BLOCK → 人工确认）

规则：
- Rule 1: 修改目标必须存在于 Index（幻造文件/符号 → BLOCK；operation=add 例外）
- Rule 2: 函数修改需 ≥1 direct（call_graph / indirect_calls / symbol definition /
  consumers）；只有 weak → WARNING；完全无证据 → BLOCK
- Rule 3: struct 字段修改需 ≥1 direct（struct_usage / field definition）
- Rule 4: 新增接口（add）无 consumer 证据 → WARNING（"新接口暂无调用证据，需要确认"）
- Rule 5: 跨模块修改必须有传播链（联合图 BFS ≤2 可达，超过 2 层只记录 exists，
  不进入强证据）；无链 → BLOCK（unsupported inference）

联合图（传播方向 = 修改 A → 影响 B）：
- call_graph:    callee → caller（改被调函数影响调用方）
- indirect_calls: callee → 注册点文件/注册函数（改回调影响注册方——注册关系，
  不进入真实调用图，7.9 定案）
- struct_usage:  field → 读写函数；struct → 字段读写函数
- include_map:   header → 包含方
- module deps:   被依赖模块 → 依赖模块
- symbols:       symbol → 定义文件（符号改动波及所在文件）
"""

from __future__ import annotations

from typing import Any

from agentx.index.index import ProjectIndex
from agentx.query.module_query import module_of_file
from agentx.validation.evidence import ChangeEvidence, collect_change_evidence

DEFAULT_RULES: dict[str, Any] = {
    "propagation_depth": 2,  # Rule 5：传播链 BFS 深度（2 层以内为强证据）
    "require_cross_module_chain": True,  # Rule 5：跨模块修改必须有传播链
    "require_direct_evidence": True,  # Rule 2/3：函数/字段修改必须有 direct 证据
}

_FILE_PREFIX = "F:"
_MAX_EXPAND = 4000  # BFS 节点上限（防超大工程全图污染）
_ADD_OPS = {"add", "new", "create"}  # 新增目标：无历史传播链属正常（Rule 5 不适用）


def _norm_path(path: str) -> str:
    return str(path).replace("\\", "/").lstrip("./").lower()


def _file_node(path: str) -> str:
    return _FILE_PREFIX + _norm_path(path)


def _symbol_nodes(index: ProjectIndex, name: str) -> set[str]:
    """符号的图节点：精确符号名；未知符号 → 文件名尾段节点（模糊）。

    含 struct 字段：字段改动需经 field → 读写函数边传播，且锚定定义文件
    （Phase 7.9 issue A——字段聚合命中不可漏 direct / 传播链）。
    """
    out: set[str] = set()
    for s in index.symbols:
        if str(s.get("name", "")) == name:
            out.add(name)
            out.add(_file_node(str(s.get("file", ""))))
    ts = index.type_semantics or {}
    for st in ts.get("structs", []):
        if str(st.get("name", "")) == name:
            out.add(name)
            out.add(_file_node(str(st.get("file", ""))))
        for f in st.get("fields", []):
            if str(f.get("name", "")) == name:
                out.add(name)
                out.add(_file_node(str(st.get("file", ""))))
    for m in ts.get("macros", []):
        if str(m.get("name", "")) == name:
            out.add(name)
            out.add(_file_node(str(m.get("file", ""))))
    return out


def build_impact_graph(index: ProjectIndex) -> dict[str, set[str]]:
    """联合影响图：节点 = 符号名 | F:<path>；边方向 = 修改 A → 影响 B。"""
    graph: dict[str, set[str]] = {}

    def add_edge(a: str, b: str) -> None:
        if not a or not b:
            return
        graph.setdefault(a, set()).add(b)

    # 符号 → 定义文件（符号改动波及文件内使用者）
    for s in index.symbols:
        name = str(s.get("name", ""))
        f = str(s.get("file", ""))
        if name and f:
            add_edge(name, _file_node(f))

    # call_graph: callee → caller
    for e in index.call_graph:
        callee = str(e.get("callee", ""))
        caller = str(e.get("caller", ""))
        if callee and caller:
            add_edge(callee, caller)

    # indirect_calls: 改回调 → 影响注册方（注册关系，非调用）
    for e in index.indirect_calls or []:
        callee = str(e.get("callee", ""))
        f = str(e.get("file", ""))
        hint = str(e.get("caller_hint", "") or "")
        if callee and f:
            add_edge(callee, _file_node(f))
        if callee and hint:
            add_edge(callee, hint)

    # struct_usage: field → 读写函数；写方 → 读方（改写逻辑影响读者）
    usage = (index.type_semantics or {}).get("struct_usage", {}) or {}
    for field, agg in usage.items():
        readers = list(agg.get("read_by", []) or [])
        writers = list(agg.get("write_by", []) or [])
        for fn in readers + writers:
            add_edge(field, fn)
        for w in writers:
            for r in readers:
                add_edge(w, r)
    # struct → 其字段 → 字段用户（struct 改动影响所有使用方）
    for st in (index.type_semantics or {}).get("structs", []):
        sname = str(st.get("name", ""))
        for f in st.get("fields", []):
            fname = str(f.get("name", ""))
            if fname in usage:
                for fn in usage[fname].get("read_by", []) + usage[fname].get("write_by", []):
                    add_edge(sname, fn)

    # include_map: header → 包含方
    for header, includers in (index.include_map or {}).items():
        for inc in includers:
            add_edge(_file_node(str(header)), _file_node(str(inc)))

# module dependencies: 被依赖模块符号 → 依赖模块符号（模块级传播）
    mod_to_syms: dict[str, list[str]] = {}
    for m in index.modules:
        mod_to_syms[str(m.get("name", ""))] = [str(s) for s in (m.get("symbols", []) or [])]
    for m in index.modules:
        for dep in m.get("dependencies", []) or []:
            dep_mod = str(dep)
            if dep_mod in mod_to_syms:
                for cs in mod_to_syms[str(m.get("name", ""))]:
                    for ds in mod_to_syms[dep_mod]:
                        add_edge(ds, cs)  # 改被依赖模块符号 → 影响依赖模块符号

    return graph


def bfs_reachable(
    graph: dict[str, set[str]], starts: set[str], depth: int
) -> set[str]:
    """BFS ≤depth 可达节点集（含起点）。"""
    visited: set[str] = set()
    frontier: set[str] = set(starts)
    visited |= frontier
    for _ in range(depth):
        nxt: set[str] = set()
        for node in frontier:
            for t in graph.get(node, set()):
                if t not in visited:
                    nxt.add(t)
        visited |= nxt
        frontier = nxt
        if not frontier:
            break
        if len(visited) > _MAX_EXPAND:
            break
    return visited


class ValidationResult:
    """整单验证结果。"""

    def __init__(self) -> None:
        self.level: str = "pass"  # pass | warning | block
        self.changes: list[ChangeEvidence] = []
        self.reasons: list[str] = []
        self.summary: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "level": self.level,
            "summary": self.summary,
            "reasons": self.reasons,
            "changes": [c.as_dict() for c in self.changes],
        }


def _status_of(ev: ChangeEvidence) -> str:
    """单条修改点状态判定（Rule 1/2/3）。"""
    change = ev.change
    op = str(change.get("operation", "modify")).lower()
    is_add = op in {"add", "new", "create"}
    symbol = str(change.get("symbol", "")).strip()

    # Rule 1: 目标必须存在于 Index（add 例外）
    if not is_add:
        if not ev.file_found:
            ev.status = "block"
            ev.reasons.append(f"修改目标不存在: {change.get('file', '')}（Rule 1）")
            return ev.status
        if symbol and not ev.symbol_found:
            ev.status = "block"
            ev.reasons.append(f"目标符号不存在于 Index: {symbol}（Rule 1）")
            return ev.status
    elif not ev.file_found and change.get("file"):
        # add 新文件：路径必须合法（非空即可；存在性不要求）
        pass

    # Rule 2/3: 函数/字段修改需要 direct 证据
    if ev.direct:
        ev.status = "pass"
        return ev.status
    if ev.weak or is_add:
        # weak 或新增目标（无历史证据属正常）：部分依据 → 需注意
        ev.status = "warning"
        target = symbol or change.get("file", "")
        if is_add and not ev.direct:
            ev.reasons.append(f"新增目标暂无历史证据（新建接口需确认 consumer）: {target}")
        else:
            ev.reasons.append(f"仅弱证据（{', '.join(e.source for e in ev.weak)}）: {target}")
        return ev.status
    ev.status = "block"
    ev.reasons.append(f"无任何工程证据: {change.get('file', '')}{'/' + symbol if symbol else ''}")
    return ev.status


def _propagation_chain(
    index: ProjectIndex,
    graph: dict[str, set[str]],
    start: ChangeEvidence,
    target: ChangeEvidence,
    depth: int,
) -> list[str]:
    """跨模块传播链：start 的修改是否可达 target（BFS ≤depth 双向任一）。"""
    s_syms: set[str] = set()
    t_syms: set[str] = set()
    s_file = str(start.change.get("file", "")).strip()
    t_file = str(target.change.get("file", "")).strip()
    s_sym = str(start.change.get("symbol", "")).strip()
    t_sym = str(target.change.get("symbol", "")).strip()
    if s_sym:
        s_syms |= _symbol_nodes(index, s_sym)
    s_syms.add(_file_node(s_file))
    if t_sym:
        t_syms |= _symbol_nodes(index, t_sym)
    t_syms.add(_file_node(t_file))

    fwd = bfs_reachable(graph, s_syms, depth)
    if fwd & t_syms:
        return [f"{s_sym or s_file} → ... → {t_sym or t_file}（≤{depth} 层可达）"]
    back = bfs_reachable(graph, t_syms, depth)
    if back & s_syms:
        return [f"{t_sym or t_file} → ... → {s_sym or s_file}（≤{depth} 层反向可达）"]
    return []


def validate_plan(
    index: ProjectIndex,
    changes: list[dict[str, Any]],
    responsibilities: dict[str, Any] | None = None,
    rules: dict[str, Any] | None = None,
) -> ValidationResult:
    """验证 Plan 修改点集合 → 整单 PASS / WARNING / BLOCK。"""
    cfg = {**DEFAULT_RULES, **(rules or {})}
    depth = int(cfg["propagation_depth"])
    result = ValidationResult()
    if not changes:
        result.level = "warning"
        result.summary = "Plan 未声明任何修改点（changes 为空），需确认是否真的需要修改"
        return result

    evidences = [collect_change_evidence(index, c, responsibilities) for c in changes]
    for ev in evidences:
        _status_of(ev)
        result.changes.append(ev)

    # Rule 4: 新增接口无 consumer → WARNING
    usage = (index.type_semantics or {}).get("struct_usage", {}) or {}
    for ev in result.changes:
        op = str(ev.change.get("operation", "modify")).lower()
        symbol = str(ev.change.get("symbol", "")).strip()
        if op in {"add", "new", "create"} and symbol:
            # consumer 证据：被调用 / 被注册 / 被读 / 被包含
            has_consumer = any(
                e.source in {"call_graph", "indirect_calls", "struct_usage", "dependencies"}
                for e in ev.direct
            ) or any(
                symbol in (agg.get("read_by", []) or []) + (agg.get("write_by", []) or [])
                for agg in usage.values()
            )
            if not has_consumer:
                if ev.status == "pass":
                    ev.status = "warning"
                ev.reasons.append(f"新接口 {symbol} 暂无调用证据，需要确认（Rule 4）")

    # Rule 5: 跨模块修改必须有传播链（联合图 BFS ≤2）
    graph = build_impact_graph(index)
    cross_fail: list[str] = []
    for i, a in enumerate(result.changes):
        a_is_add = str(a.change.get("operation", "modify")).lower() in _ADD_OPS
        for b in result.changes[i + 1 :]:
            b_is_add = str(b.change.get("operation", "modify")).lower() in _ADD_OPS
            if a_is_add or b_is_add:
                # 新增目标无历史传播链属正常（issue B：add 不允许因 Rule 5 BLOCK）
                continue
            am = module_of_file(index, str(a.change.get("file", "")))
            bm = module_of_file(index, str(b.change.get("file", "")))
            a_mod = str(am.get("name", "")) if am else str(a.change.get("file", ""))
            b_mod = str(bm.get("name", "")) if bm else str(b.change.get("file", ""))
            if a_mod == b_mod:
                continue  # 同模块不要求链
            chain = _propagation_chain(index, graph, a, b, depth)
            if chain:
                a.propagation.extend(chain)
            else:
                a.status = "block"
                a.reasons.append(
                    f"跨模块修改无传播链: {a_mod} → {b_mod}（Rule 5，≤{depth} 层不可达）"
                )
                cross_fail.append(f"{a.change.get('file', '')} ↔ {b.change.get('file', '')}")

    # 整单判定
    blocks = [ev for ev in result.changes if ev.status == "block"]
    warnings = [ev for ev in result.changes if ev.status == "warning"]
    if blocks:
        result.level = "block"
        result.reasons = [
            f"[BLOCK] {ev.change.get('file', '')}"
            + (f"/{ev.change.get('symbol', '')}" if ev.change.get("symbol") else "")
            + f": {ev.reasons[0] if ev.reasons else '无证据'}"
            for ev in blocks
        ]
        if cross_fail:
            result.reasons.append(f"[BLOCK] 跨模块无传播链: {', '.join(cross_fail)}")
    elif warnings:
        result.level = "warning"
        result.reasons = [
            f"[WARNING] {ev.change.get('file', '')}"
            + (f"/{ev.change.get('symbol', '')}" if ev.change.get("symbol") else "")
            + f": {ev.reasons[0] if ev.reasons else '仅弱证据'}"
            for ev in warnings
        ]
    else:
        result.level = "pass"
        result.reasons = [f"{len(result.changes)} 个修改点均有事实证据"]

    n_pass = sum(1 for ev in result.changes if ev.status == "pass")
    n_warn = len(warnings)
    n_block = len(blocks)
    result.summary = (
        f"Validation: {result.level.upper()} — "
        f"{n_pass} 修改点有充分证据，{n_warn} 需注意，{n_block} 被拦截"
    )
    return result


def format_validation(result: ValidationResult) -> str:
    """验证结果展示（ASCII 安全，给 LLM 修正 / 用户确认用）。"""
    lines = [result.summary]
    for ev in result.changes:
        lines.append(f"修改: {ev.change.get('file', '')}")
        if ev.change.get("symbol"):
            lines.append(f"  符号: {ev.change['symbol']}（{ev.change.get('operation', 'modify')}）")
        lines.append(f"  状态: {ev.status.upper()}")
        for e in ev.direct:
            lines.append(f"  FACT: [{e.source}] {e.description}")
        for e in ev.weak:
            lines.append(f"  WEAK: [{e.source}] {e.description}")
        for r in ev.reasons:
            lines.append(f"  原因: {r}")
    return "\n".join(lines) or "Validation: PASS"
