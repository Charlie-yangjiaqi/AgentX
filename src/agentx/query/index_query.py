"""Project Knowledge Query 共享底座（零 LLM、零扫描）。

feature / symbol / architecture 三个 Query 全部基于这里的原语，
保证只有一份查询逻辑、一个认知来源。

原语全部只读 ProjectIndex 结构（files/symbols/call_graph/include_map/build_info/
project_understanding），不触碰文件系统。
"""

from __future__ import annotations

from typing import Any

from agentx.index.index import ProjectIndex
from agentx.understanding.query import _path_hit, _symbol_hit

# 匹配层级（高 → 低）
HIT_SYMBOL = "symbol"
HIT_FILE = "file"


def _basename(p: str) -> str:
    return p.replace("\\", "/").rsplit("/", 1)[-1]


def find_symbols(index: ProjectIndex, keywords: list[str]) -> dict[str, dict[str, Any]]:
    """符号直接命中（最高优先级）：name → 符号条目（含 hit/match_level）。"""
    out: dict[str, dict[str, Any]] = {}
    for s in index.symbols:
        kw = _symbol_hit(s, keywords)
        if kw:
            entry = dict(s)
            entry["hit"] = kw
            entry["match_level"] = HIT_SYMBOL
            out[str(s.get("name", ""))] = entry
    return out


def find_files(index: ProjectIndex, keywords: list[str]) -> dict[str, str]:
    """文件直接命中：path → 命中关键词。"""
    out: dict[str, str] = {}
    for f in index.files:
        kw = _path_hit(f.path, keywords)
        if kw:
            out[f.path] = kw
    return out


def expand_call_edges(index: ProjectIndex, hit_symbol_names: set[str]) -> list[dict[str, Any]]:
    """调用图扩展：与命中符号相邻的边。"""
    edges: list[dict[str, Any]] = []
    for e in index.call_graph:
        caller, callee = str(e.get("caller", "")), str(e.get("callee", ""))
        if caller in hit_symbol_names or callee in hit_symbol_names:
            edges.append(e)
    return edges


def file_of_symbol(index: ProjectIndex, name: str) -> str | None:
    for s in index.symbols:
        if s.get("name") == name:
            f = s.get("file")
            return str(f) if f else None
    return None


def merge_edge_files(
    index: ProjectIndex,
    edges: list[dict[str, Any]],
    hit_symbol_names: set[str],
    hit_files: dict[str, str],
) -> None:
    """调用边另一端符号所在文件并入 hit_files（原地修改）。"""
    for e in edges:
        for side in ("caller", "callee"):
            name = str(e.get(side, ""))
            if name in hit_symbol_names:
                continue
            fpath = file_of_symbol(index, name)
            if fpath and fpath not in hit_files:
                hit_files[fpath] = f"via:{side}"


def expand_includes(
    index: ProjectIndex, hit_files: dict[str, str]
) -> tuple[dict[str, str], dict[str, str]]:
    """include/dependency 扩展：返回 (被依赖文件, 依赖方文件)，短名兜底匹配。"""
    hit_basenames = {_basename(p) for p in hit_files}
    dep_targets: dict[str, str] = {}
    dep_sources: dict[str, str] = {}
    for src, targets in index.include_map.items():
        src_in_hit = src in hit_files or _basename(src) in hit_basenames
        if src_in_hit:
            for t in targets:
                if (
                    t not in hit_files
                    and _basename(t) not in hit_basenames
                    and t not in dep_targets
                ):
                    dep_targets[t] = "included_by"
        else:
            for t in targets:
                if (
                    (t in hit_files or _basename(t) in hit_basenames)
                    and src not in hit_files
                    and src not in dep_sources
                ):
                    dep_sources[src] = "includes"
    return dep_targets, dep_sources


def file_meta(index: ProjectIndex) -> dict[str, Any]:
    return {f.path: f for f in index.files}


def primary_compile_status(paths: list[str], meta: dict[str, Any]) -> str:
    """主文件的 compile_status：compiled > not_compiled > excluded > unknown。"""
    statuses = [str(meta[p].compile_status) for p in paths if p in meta]
    if not statuses:
        return "unknown"
    for preferred in ("compiled", "not_compiled", "excluded"):
        if preferred in statuses:
            return preferred
    return "unknown"


def callers_of(index: ProjectIndex, symbol_name: str) -> list[str]:
    """调用方（符号级）。"""
    return [str(e.get("caller", "")) for e in index.call_graph if e.get("callee") == symbol_name]


def callees_of(index: ProjectIndex, symbol_name: str) -> list[str]:
    """被调用方（符号级）。"""
    return [str(e.get("callee", "")) for e in index.call_graph if e.get("caller") == symbol_name]


def caller_files_of(index: ProjectIndex, symbol_name: str) -> list[str]:
    """调用方文件（文件级，去重保序）。"""
    seen: set[str] = set()
    out: list[str] = []
    for caller in callers_of(index, symbol_name):
        f = file_of_symbol(index, caller)
        if f and f not in seen:
            seen.add(f)
            out.append(f)
    return out


def related_modules(index: ProjectIndex, hit_files: dict[str, str]) -> list[str]:
    """关联模块：消费方/相邻文件的模块名（去重，不含命中文件自身）。"""
    hit_basenames = {_basename(p) for p in hit_files}
    hit_dirs = {p.replace("\\", "/").rsplit("/", 1)[0] for p in hit_files}
    seen: set[str] = set()
    out: list[str] = []
    # 被命中文件 include 的文件 / include 命中文件的文件
    for src, targets in index.include_map.items():
        if src in hit_files or _basename(src) in hit_basenames:
            for t in targets:
                name = _basename(t).rsplit(".", 1)[0]
                if name and name not in seen and t not in hit_files:
                    seen.add(name)
                    out.append(name)
        else:
            for t in targets:
                if t in hit_files or _basename(t) in hit_basenames:
                    name = _basename(src).rsplit(".", 1)[0]
                    if name and name not in seen and src not in hit_files:
                        seen.add(name)
                        out.append(name)
    # 相邻目录的同名族（如 KEY/ 目录 → key）
    for d in sorted(hit_dirs):
        name = d.rsplit("/", 1)[-1].lower()
        if name and name not in seen:
            seen.add(name)
            out.append(name)
    return out[:10]


def build_facts(index: ProjectIndex, paths: list[str]) -> dict[str, Any]:
    """Build Reality 事实：compile_status + target（Index 落库值）。"""
    meta = file_meta(index)
    build: dict[str, Any] = {}
    build_info = index.build_info or {}
    build["compile_status"] = primary_compile_status(paths, meta)
    target = build_info.get("target")
    if target:
        build["target"] = str(target)
    cpu = build_info.get("cpu")
    if cpu:
        build["cpu"] = str(cpu)
    defines = build_info.get("defines")
    if defines:
        build["defines"] = list(defines)
    return build


def no_evidence(reason: str) -> dict[str, Any]:
    """Index 证据不足：不编造、不扫描。"""
    return {
        "confidence": "low",
        "files": [],
        "symbols": [],
        "call_chain": [],
        "related_modules": [],
        "build": {},
        "evidence": [],
        "summary": "",
        "recommended_action": {"type": "read_source"},
        "reason": [reason],
    }
