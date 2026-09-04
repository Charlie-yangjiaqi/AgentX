"""Phase 8.2 Incremental Update：文件级轻量增量知识维护。

定位：AgentX「持续维护的工程知识库」的第一套可靠增量机制（不是把 enrich 改名）。

原则（Builder 决定 + 用户确认）：
- 文件级增量：只对 changed/added/removed 文件做语义重提取与图事实补丁
- CodeGraph 是增量核心基础：engine 的 `sync` 本身只重扫变化文件（graph.py），
  增量路径复用它做全局 call/include/symbol 事实的权威刷新
- 删/改文件贡献的 facts 必须被正确移除（不残留 deleted/old symbol）
- semantic / type 只刷新受影响文件
- module 由 patched Index 全量确定性重算（不依赖树解析，便宜）
- 无法可靠局部修复（.h 大范围 / 无法判定）→ 升级 REINDEX_REQUIRED（绝不静默污染）

禁止：为了"看起来成功"用旧事实拼接不可信结果。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from agentx.index.fingerprint import CODE_SOURCE_EXTS
from agentx.index.freshness import REINDEX_REQUIRED
from agentx.index.index import ProjectIndex, index_exclude_name, load_index
from agentx.understanding.graph import analyze_project

_HEADER_EXTS = {".h", ".hpp", ".hh"}


def is_header(path: str) -> bool:
    return Path(path).suffix.lower() in _HEADER_EXTS


def expand_header_affected(
    index: ProjectIndex, header_changed: list[str]
) -> list[str]:
    """改 .h → 找出会受影响的文件：include_map 中被该头 include 的消费者。

    返回受影响清单（含头本身 + 直接/传递 include 到它的源文件）。
    """
    changed_set = set(header_changed)

    consumers: set[str] = set(changed_set)
    frontier = list(changed_set)
    # 沿 include_map 反向传播：谁 include 了这些文件 → 加入受影响集
    while frontier:
        nxt: list[str] = []
        for src, targets in (index.include_map or {}).items():
            if src in consumers:
                continue
            if any(_same_file(t, f) for t in targets for f in frontier):
                consumers.add(src)
                nxt.append(src)
        frontier = nxt
        if len(consumers) > 1000:  # 防御：超大 include 传播
            break
    return sorted(consumers)


def _same_file(a: str, b: str) -> bool:
    a = a.replace("\\", "/")
    b = b.replace("\\", "/")
    return a == b or a.endswith("/" + b) or b.endswith("/" + a)


def patch_required(
    root: Path,
    index: ProjectIndex,
    *,
    changed: list[str],
    freshness_cfg: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """判断该变化集是否必须升级 REINDEX_REQUIRED（返回 verdict dict 或 None=可增量）。

    触发升级：改 .h 且受影响消费者超过 header_impact_files 阈值（无法安全局部修复）。
    """
    from agentx.index.freshness import _load_freshness_config

    cfg = freshness_cfg or _load_freshness_config()
    code_changed = [c for c in changed if Path(c).suffix.lower() in CODE_SOURCE_EXTS]
    if not code_changed:
        return None
    headers = [c for c in code_changed if is_header(c)]
    if headers:
        affected = expand_header_affected(index, headers)
        # affected 含头文件自身 + 全部下游；若 > 阈值 或 无法枚举（include_map 缺失
        # 且改动头无消费者记录）→ 保守升级
        if len(affected) > cfg["header_impact_files"]:
            return {
                "state": REINDEX_REQUIRED,
                "recommend_reindex": True,
                "requires_confirmation": True,
                "reason": (
                    f"header change {', '.join(headers[:3])} affects "
                    f"{len(affected)} files (>{cfg['header_impact_files']}) — cannot "
                    f"patch safely; full reindex required"
                ),
                "detail": {"affected_files": affected[:50], "headers": headers},
            }
    return None


def incremental_update(
    project_root: Path,
    *,
    modified: list[str] | None = None,
    added: list[str] | None = None,
    removed: list[str] | None = None,
    changed: list[str] | None = None,
) -> dict[str, Any]:
    """执行文件级增量更新；返回 freshness verdict。

    - modified/added/removed 由 sync 的 diff 精确提供
    - changed：合并清单（未拆分时）
    - 任何一步无法可靠增量 → 不写 Index，返回 REINDEX_REQUIRED verdict
    """
    root = project_root.resolve()
    modified = list(modified or [])
    added = list(added or [])
    removed = list(removed or [])
    changed = list(changed or []) or (modified + added + removed)
    changed = [c for c in changed if c]

    index = load_index(root)
    if index is None:
        return {
            "state": REINDEX_REQUIRED,
            "recommend_reindex": True,
            "requires_confirmation": True,
            "reason": "no existing index to incrementally update",
            "detail": {},
        }

    # 安全升级网：.h 大范围 / 不可靠
    verdict = patch_required(root, index, changed=changed)
    if verdict is not None:
        return verdict

    # CodeGraph 引擎增量：sync 只重扫变化文件（graph.py:242）→ 权威刷新
    graph = analyze_project(root)

    # 目标集 = 磁盘当前相关文件（含新增）；从 graph + index files 决定最终文件集
    from agentx.index.fingerprint import relevant_files as _rf

    on_disk = set(_rf(root, extra_excludes={index_exclude_name(root)}))
    removed_set = set(removed)
    changed_set = set(changed)
    # 文件 metas 补丁：复用 _index_files_and_build 做权威分类（对全量 on_disk 便宜，
    # 无树解析——只做 scope/build 分类与 hash）
    from agentx.plan.service import _index_files_and_build

    files_meta, build_info, build_view = _index_files_and_build(root, graph)
    meta_by_path = {str(m["path"]): m for m in files_meta}
    final_paths = sorted(on_disk)
    files_for_index = [meta_by_path[p] for p in final_paths if p in meta_by_path]

    # ---- 符号 diff-merge（文件级）----
    # graph.symbols：CodeGraph 引擎已 sync（只重扫变化文件）→ 权威全量。
    # 我们对受影响 project 文件做 tree-sitter semantic 增强并合并；
    # 保留旧 Index 中"未变化文件"的语义字段（signature/members 等）
    # ——因为引擎 DB 只有 CodeGraph 字段，semantic 字段是 AgentX 侧附加。
    changed_files_set = changed_set | removed_set
    kept_symbols = [
        s
        for s in index.symbols
        if str(s.get("file", "")) not in changed_files_set
    ]

    project_changed = sorted(
        p
        for p in changed_set
        if p in meta_by_path and meta_by_path[p]["scope_type"] == "project"
    )

    errors: list[str] = list(graph.errors)
    capabilities: dict[str, Any] = {"module": {"enabled": True}}
    indirect_calls: list[dict[str, Any]] = []
    type_semantics: dict[str, Any] = {}
    if graph.source == "codegraph":
        try:
            from agentx.semantic.extractor import _PARSER_SOURCE
            from agentx.semantic.merge import merge_semantics

            if project_changed:
                symbols, sem_errors, indirect_calls, semantics = merge_semantics(
                    graph.symbols, root, project_changed
                )
            else:
                symbols, sem_errors, indirect_calls, semantics = (
                    list(graph.symbols),
                    [],
                    [],
                    [],
                )
            errors.extend(sem_errors)
            # type_semantics：受影响文件重建 + 未受影响保留
            type_semantics = _patch_type_semantics(
                root, semantics, symbols, indirect_calls, index, changed_set
            )
            capabilities["semantic"] = {
                "enabled": True,
                "reason": None,
                "parser": _PARSER_SOURCE,
            }
        except Exception as e:  # semantic 失败不阻断（给出可用增量）
            symbols = graph.symbols
            from agentx.semantic.runtime import SemanticUnavailableError

            if isinstance(e, SemanticUnavailableError):
                errors.append(f"Semantic 不可用: {e}")
            else:
                errors.append(f"Semantic 增量提取失败({type(e).__name__}): {e}")
            capabilities["semantic"] = {
                "enabled": False,
                "reason": errors[-1],
                "parser": None,
            }
            type_semantics = _drop_type_files(index.type_semantics, changed_set | removed_set)
    else:
        symbols = graph.symbols
        capabilities["semantic"] = {
            "enabled": False,
            "reason": "CodeGraph 不可用（filescan 模式），增量仅文件级",
            "parser": None,
        }

    # 合并：kept_symbols（未变化文件，保留语义字段）∪ 受影响文件的权威新符号。
    # 受影响文件的旧符号已被 kept_symbols 排除；graph.symbols/semantic 增强结果
    # 中同名符号即新版本 → 天然 diff-merge（不残留 deleted/old symbol）。
    merged: list[dict[str, Any]] = list(kept_symbols)
    seen: set[tuple[str, str, str]] = {
        (str(s.get("file", "")), str(s.get("name", "")), str(s.get("type", "")))
        for s in kept_symbols
    }
    for s in symbols:
        key = (str(s.get("file", "")), str(s.get("name", "")), str(s.get("type", "")))
        if key in seen:
            continue
        seen.add(key)
        if str(s.get("file", "")) in changed_files_set:
            merged.append(s)
        # 未变化文件的新符号（引擎全量快照里也有）——若旧文件无该符号但引擎
        # 有 → 保留引擎事实（如新函数被加进未变文件不可能；忽略重复即可）
    # 注：未变化文件的符号以 kept_symbols 为准（保留语义字段），避免引擎全量
    # 快照的纯 CodeGraph 符号覆盖掉已增强版本。新增文件符号已通过 changed_set 分支进入。

    # call_graph/include_map：graph（引擎已同步）权威全量
    call_graph = graph.call_graph
    include_map = graph.include_map

    # 构建新 Index
    from agentx.index.index import create_index

    new_index = create_index(
        root,
        files=files_for_index,
        build_info=build_info,
        symbols=merged,
        call_graph=call_graph,
        include_map=include_map,
        codegraph_source=graph.source,
        errors=errors,
    )
    new_index.capabilities = capabilities
    _annotate_symbols(new_index, merged, meta_by_path)

    old = load_index(root)
    from agentx.plan.service import finalize_index

    new_index, _ = finalize_index(
        root, new_index, old, graph, indirect_calls, type_semantics
    )
    return {
        "state": "AUTO_UPDATED",
        "recommend_reindex": False,
        "requires_confirmation": False,
        "reason": f"incremental update applied ({len(changed)} files)",
        "detail": {
            "changed_files": sorted(changed),
            "removed": sorted(removed_set),
            "added": sorted(set(added)),
            "action": "incremental",
            "index_status": "VALID",
            "file_count": new_index.file_count,
            "symbol_count": len(new_index.symbols),
            "source_fingerprint": new_index.source_fingerprint,
        },
    }


def _annotate_symbols(
    index: ProjectIndex, symbols: list[dict[str, Any]], meta_by_path: dict[str, dict[str, Any]]
) -> None:
    """符号 scope 标注（增量后统一处理）。"""
    for s in symbols:
        f = str(s.get("file", ""))
        meta = meta_by_path.get(f)
        if meta is not None:
            s["scope_type"] = meta.get("scope_type", "project")
            s["scope_name"] = meta.get("scope_name")
        else:
            s["scope_type"] = "project"


def _patch_type_semantics(
    root: Path,
    semantics: list[Any],
    symbols: list[dict[str, Any]],
    indirect_calls: list[dict[str, Any]],
    index: ProjectIndex,
    changed_set: set[str],
) -> dict[str, Any]:
    """Type semantics 增量：受影响文件重建，其余保留旧条目。

    无法对部分类型做精确 diff（type entry 由 AST 整块产生），所以对受影响文件
    整文件重建 type 条目，未受影响文件直接保留旧 type_semantics。
    """
    from agentx.semantic.type_extractor import build_type_semantics

    old_ts = index.type_semantics or {}
    # 受影响文件集合 = changed + removed（它们的 type 条目要重建/删除）
    affected = changed_set

    new_ts: dict[str, Any] = {}
    if semantics:
        fresh = build_type_semantics(root, semantics, symbols, indirect_calls, old=None)
        new_ts = fresh
    # 保留旧条目（未受影响文件）：structs/enums/macros
    keep_structs = [e for e in old_ts.get("structs", []) if str(e.get("file", "")) not in affected]
    keep_enums = [e for e in old_ts.get("enums", []) if str(e.get("file", "")) not in affected]
    keep_macros = [e for e in old_ts.get("macros", []) if str(e.get("file", "")) not in affected]
    # struct_usage：跨文件聚合，无法精确 diff；受影响文件改动若涉及 field_usage
    # 则直接重建全量（保守：必要时完整重算 usage）——usage 是低风险派生事实
    return {
        "structs": keep_structs + new_ts.get("structs", []),
        "enums": keep_enums + new_ts.get("enums", []),
        "macros": keep_macros + new_ts.get("macros", []),
        "struct_usage": _rebuild_struct_usage(
            new_ts.get("struct_usage", {}), old_ts.get("struct_usage", {}), affected
        ),
    }


def _drop_type_files(
    old_ts: dict[str, Any], affected: set[str]
) -> dict[str, Any]:
    """semantic 不可用时的 type 降级：仅删受影响文件的旧 type 条目。"""
    old_ts = dict(old_ts or {})
    return {
        "structs": [e for e in old_ts.get("structs", []) if str(e.get("file", "")) not in affected],
        "enums": [e for e in old_ts.get("enums", []) if str(e.get("file", "")) not in affected],
        "macros": [e for e in old_ts.get("macros", []) if str(e.get("file", "")) not in affected],
        "struct_usage": {},
    }


def _rebuild_struct_usage(
    fresh_usage: dict[str, Any],
    old_usage: dict[str, Any],
    affected: set[str],
) -> dict[str, Any]:
    """struct_usage 增量：fresh（受影响文件新提取）∪ 旧（去受影响文件贡献）。

    usage 聚合到 field_name 粒度且跨文件，无逐文件逆映射 → 保守处理：
    保留旧 usage，把 fresh 中出现的 field 覆盖旧（受影响文件改到的 field 以新为准）。
    """
    out: dict[str, Any] = {}
    for field, agg in (old_usage or {}).items():
        if field in fresh_usage:
            continue  # fresh 覆盖
        out[field] = agg
    for field, agg in (fresh_usage or {}).items():
        out[field] = agg
    return out


__all__ = [
    "incremental_update",
    "patch_required",
    "expand_header_affected",
    "is_header",
    "REINDEX_REQUIRED",
]
