"""Index Freshness（Phase 8.2）：判断当前 Index 对工程变化是否仍可信，以及该自动
更新还是要求用户完整重建。

三类可独立归因指纹（全部来自磁盘与 Index 记录的对照）：
- scope_fingerprint          ：scope 配置（ignore/third_party/include/build.target）
- source_fingerprint         ：代码内容（排除配置）
- build_scope_fingerprint    ：active target 规范化编译边界

状态机（顺序判定，返回优先级最高的状态）：
    build_scope_fingerprint 显著变化            → REINDEX_REQUIRED
    scope_fingerprint 变化 + 分类影响大           → REINDEX_REQUIRED
    scope_fingerprint 变化（分类影响小）          → （自动 reclassify 级别，见 Level 2）
    source_fingerprint 变化：
        影响文件数 > large_source_change         → REINDEX_REQUIRED
        含高风险结构文件（公共 .h 大改 / core typedef）→ REINDEX_REQUIRED
        小改动                                   → STALE_RECOMMENDED（可增量）
    完全一致                                    → VALID

阈值配置（config.json > 环境变量 > 默认）：
    freshness.source_large_files     默认 100
    freshness.scope_impact_files     默认 50
    freshness.scope_impact_ratio     默认 0.10
    freshness.build_impact_files     默认 50
    freshness.build_impact_ratio     默认 0.20
    freshness.header_impact_files    默认 5   # 公共 .h 改动文件数上限

关键原则（Phase 8.2）：
- 数量/比例阈值 + 变化类型 + 工程影响范围 三者联合，阈值不是唯一条件
- 宁可把少数复杂变化升级为 REINDEX_REQUIRED，不用不完整增量污染 Index
- AUTO_UPDATED 是“动作后状态”（sync 成功增量后返回），不是静止判定态
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from agentx.index.fingerprint import CODE_SOURCE_EXTS, compute_source_fingerprint
from agentx.scope.build_scope import compute_build_scope_fingerprint
from agentx.scope.config import compute_scope_fingerprint

# 状态常量
VALID = "VALID"
STALE_RECOMMENDED = "STALE_RECOMMENDED"
AUTO_UPDATED = "AUTO_UPDATED"
REINDEX_REQUIRED = "REINDEX_REQUIRED"

_STATES = (VALID, STALE_RECOMMENDED, AUTO_UPDATED, REINDEX_REQUIRED)

# 高风险结构关键词：这些文件改动即使数量少也可能升级 REINDEX_REQUIRED。
# 规则：改动的代码文件里 任一 basename 匹配 且 文件数 ≤ header_impact 阈值 且 为 .h，
# 视为"公共头大改"（保守：无法判断是否公共 → 优先升级）。
_HIGH_RISK_SUFFIXES = {".h", ".hpp", ".hh"}
_HIGH_RISK_NAME_TOKENS = ("config", "types", "typedef", "common", "defs", "hal", "port")


def default_freshness_config() -> dict[str, Any]:
    return {
        "source_large_files": 100,
        "scope_impact_files": 50,
        "scope_impact_ratio": 0.10,
        # ratio 路径的最低绝对移动数下限：避免小项目（文件总数少）中"改动比例高但
        # 绝对数小"被误判 REINDEX_REQUIRED（spec 意图：影响*大量*文件才重建）
        "scope_impact_ratio_min_files": 20,
        "build_impact_files": 50,
        "build_impact_ratio": 0.20,
        "header_impact_files": 5,
    }


def _load_freshness_config() -> dict[str, Any]:
    import os

    from agentx.config.config import load_config

    raw: dict[str, Any] = {}
    try:
        cfg = load_config()
        f = cfg.freshness or {}
        raw = dict(f) if isinstance(f, dict) else {}
    except Exception:
        pass
    defaults = default_freshness_config()

    def _get(key: str, env: str) -> float:
        v = os.environ.get(env)
        if v is None:
            try:
                return float(raw.get(key, defaults[key]))
            except (TypeError, ValueError):
                return float(defaults[key])
        try:
            return float(v)
        except ValueError:
            return float(defaults[key])

    out: dict[str, Any] = {}
    out["source_large_files"] = int(_get("source_large_files", "AGENTX_FRESHNESS_SOURCE_LARGE"))
    out["scope_impact_files"] = int(
        _get("scope_impact_files", "AGENTX_FRESHNESS_SCOPE_IMPACT_FILES")
    )
    out["scope_impact_ratio"] = _get(
        "scope_impact_ratio", "AGENTX_FRESHNESS_SCOPE_IMPACT_RATIO"
    )
    out["scope_impact_ratio_min_files"] = int(
        _get(
            "scope_impact_ratio_min_files",
            "AGENTX_FRESHNESS_SCOPE_IMPACT_RATIO_MIN_FILES",
        )
    )
    out["build_impact_files"] = int(
        _get("build_impact_files", "AGENTX_FRESHNESS_BUILD_IMPACT_FILES")
    )
    out["build_impact_ratio"] = _get(
        "build_impact_ratio", "AGENTX_FRESHNESS_BUILD_IMPACT_RATIO"
    )
    out["header_impact_files"] = int(
        _get("header_impact_files", "AGENTX_FRESHNESS_HEADER_IMPACT")
    )
    return out


def freshness_config() -> dict[str, Any]:
    """返回当前生效的 freshness 阈值配置。"""
    return _load_freshness_config()


def current_source_fingerprint(project_root: Path) -> str:
    return compute_source_fingerprint(project_root)


def current_scope_fingerprint(project_root: Path) -> str:
    return compute_scope_fingerprint(project_root)


def current_build_fingerprint(project_root: Path) -> str | None:
    return compute_build_scope_fingerprint(project_root)


def changed_source_files(project_root: Path, index: Any) -> list[str]:
    """无 git 时：按 Index files 记录的 content_hash 比对，找出变化的代码文件。

    Index 中每个文件的 content_hash 与磁盘当前 hash 不一致 → changed。
    只统计代码文件（.c/.h/.cpp/.hpp/...），配置类（scope yaml）由各自指纹处理。
    """
    import hashlib

    root = project_root.resolve()
    changed: list[str] = []
    for f in index.files:
        path = str(getattr(f, "path", ""))
        if Path(path).suffix.lower() not in CODE_SOURCE_EXTS:
            continue
        try:
            current = hashlib.sha256((root / path).read_bytes()).hexdigest()[:16]
        except OSError:
            current = None
        old = getattr(f, "content_hash", None)
        if current is not None and current != old:
            changed.append(path)
    return sorted(changed)


def _file_is_high_risk_header(rel_path: str) -> bool:
    """改动文件是否高风险公共头（保守升级）。"""
    p = rel_path.replace("\\", "/")
    base = p.rsplit("/", 1)[-1].lower()
    if Path(p).suffix.lower() not in _HIGH_RISK_SUFFIXES:
        return False
    if any(tok in base for tok in _HIGH_RISK_NAME_TOKENS):
        return True
    # 顶层或浅层头文件（无深层目录）视为可能被广泛 include
    parts = p.split("/")
    return len(parts) <= 2


def classify_source_change(
    project_root: Path,
    index: Any,
    changed: list[str] | None = None,
    cfg: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """源代码变化 → freshness 判定（不含 scope/build 归因）。

    changed：调用方传入精确变更清单（git diff）时用它；否则用 hash 比对。
    返回 {state, recommend_reindex, requires_confirmation, reason, detail}。
    只判定 source 层：build/scope 变化由 evaluate_index_state 先归因，不在此重复。
    """
    cfg = cfg or _load_freshness_config()
    root = project_root.resolve()
    if changed is None:
        changed = changed_source_files(root, index)
    code_changed = [c for c in changed if Path(c).suffix.lower() in CODE_SOURCE_EXTS]

    if not code_changed:
        return {
            "state": VALID,
            "recommend_reindex": False,
            "requires_confirmation": False,
            "reason": "no source change",
            "detail": {"changed_files": [], "large": False, "high_risk": False},
        }

    n = len(code_changed)
    large = n > cfg["source_large_files"]
    # 高风险公共头 / 核心类型：即使小改动也升级（数量不是唯一判据）
    high_risk_headers = [c for c in code_changed if _file_is_high_risk_header(c)]
    high_risk = len(high_risk_headers) > cfg["header_impact_files"]

    if large or high_risk:
        reasons: list[str] = []
        if large:
            reasons.append(f"{n} files changed (> threshold {cfg['source_large_files']})")
        if high_risk:
            reasons.append(f"{len(high_risk_headers)} public header(s) changed: "
                           f"{', '.join(high_risk_headers[:5])}")
        return {
            "state": REINDEX_REQUIRED,
            "recommend_reindex": True,
            "requires_confirmation": True,
            "reason": "; ".join(reasons),
            "detail": {
                "changed_files": code_changed,
                "large": large,
                "high_risk": high_risk,
                "high_risk_files": high_risk_headers,
            },
        }

    return {
        "state": STALE_RECOMMENDED,
        "recommend_reindex": False,
        "requires_confirmation": False,
        "reason": f"source changed ({n} files)",
        "detail": {
            "changed_files": code_changed,
            "large": False,
            "high_risk": False,
        },
    }


def evaluate_index_state(
    project_root: Path, index: Any, changed: list[str] | None = None
) -> dict[str, Any]:
    """Freshness 主入口：对当前 Index 做完整状态判定（4 态）。

    顺序归因：build_scope → scope → source → VALID。
    返回的 detail 含各指纹当前/记录值，供调用方展示。
    """
    root = project_root.resolve()
    cfg = _load_freshness_config()

    # --- Build Boundary ---
    cur_build = compute_build_scope_fingerprint(root)
    if (
        index.build_scope_fingerprint is not None
        and cur_build is not None
        and index.build_scope_fingerprint != cur_build
    ):
        bs = index.build_info.get("build_scope") if isinstance(index.build_info, dict) else {}
        bs = bs if isinstance(bs, dict) else {}
        old_count = int(bs.get("build_files") or 0) if bs.get("build_files") is not None else 0
        from agentx.scope.build_scope import resolve_keil_build

        view = resolve_keil_build(root)
        cur_count = len(view.build_files) if view.resolved else 0
        delta = abs(cur_count - old_count)
        base = max(old_count, 1)
        ratio = delta / base
        if delta > cfg["build_impact_files"] or ratio > cfg["build_impact_ratio"]:
            return {
                "state": REINDEX_REQUIRED,
                "recommend_reindex": True,
                "requires_confirmation": True,
                "reason": (
                    f"build boundary changed significantly "
                    f"({old_count} → {cur_count} compiled files)"
                ),
                "detail": {
                    "build_scope_fingerprint": cur_build,
                    "old_build_scope_fingerprint": index.build_scope_fingerprint,
                    "old_build_files": old_count,
                    "current_build_files": cur_count,
                    "delta": delta,
                },
            }
        # 小 build 变化：交给 source 增量层（reindex 前不单独标 REQUIRED）

    # --- Scope ---
    cur_scope = compute_scope_fingerprint(root)
    if index.scope_fingerprint is not None and index.scope_fingerprint != cur_scope:
        # 分类影响幅度：old（Index 记录）vs new（当前 scope+build 重新分类）。
        # 源 = Index 文件 ∪ 磁盘相关文件；被新 scope 排除（ignored）→ 视为移动。
        from agentx.index.fingerprint import relevant_files as _rf
        from agentx.index.index import index_exclude_name
        from agentx.scope.build_scope import classify_build_scope, resolve_keil_build

        old_scope = {str(f.path): str(getattr(f, "scope_type", "project")) for f in index.files}
        disk_files = _rf(root, extra_excludes={index_exclude_name(root)})
        candidates = sorted(set(old_scope) | set(disk_files))
        view = resolve_keil_build(root)
        classified = classify_build_scope(root, candidates, view, index.include_map)
        moves = 0
        for p in candidates:
            st = classified.get(p, {}).get("scope_type")
            old_st = old_scope.get(p)
            # 旧有分类但新分类缺失（被 ignore）或分类不同 → move
            if st is None:
                if old_st is not None:
                    moves += 1
            elif st != old_st:
                moves += 1
        base = max(len(index.files), 1)
        ratio = moves / base
        ratio_large = (
            moves / base > cfg["scope_impact_ratio"]
            and moves >= cfg["scope_impact_ratio_min_files"]
        )
        if moves > cfg["scope_impact_files"] or ratio_large:
            return {
                "state": REINDEX_REQUIRED,
                "recommend_reindex": True,
                "requires_confirmation": True,
                "reason": (
                    f"scope change impacts {moves} files "
                    f"(> threshold {cfg['scope_impact_files']} / "
                    f"{cfg['scope_impact_ratio']:.0%})"
                ),
                "detail": {
                    "scope_fingerprint": cur_scope,
                    "moved_files": moves,
                    "ratio": round(ratio, 3),
                },
            }
        return {
            "state": STALE_RECOMMENDED,
            "recommend_reindex": False,
            "requires_confirmation": False,
            "reason": "scope change (small impact) — auto reclassify eligible",
            "detail": {
                "scope_fingerprint": cur_scope,
                "old_scope_fingerprint": index.scope_fingerprint,
                "moved_files": moves,
            },
        }

    # --- Source ---
    cur_source = compute_source_fingerprint(root)
    if index.source_fingerprint is not None and index.source_fingerprint != cur_source:
        source_verdict = classify_source_change(root, index, changed=changed, cfg=cfg)
        source_verdict["detail"]["source_fingerprint"] = cur_source
        source_verdict["detail"]["old_source_fingerprint"] = index.source_fingerprint
        return source_verdict

    return {
        "state": VALID,
        "recommend_reindex": False,
        "requires_confirmation": False,
        "reason": "index matches current project state",
        "detail": {},
    }


def freshness_block(project_root: Path, index: Any = None) -> dict[str, Any]:
    """REINDEX_REQUIRED 时的统一描述（供 plan/auto 硬停、READ 提示）。"""
    if index is None:
        from agentx.index.index import load_index

        index = load_index(project_root)
    if index is None:
        return {
            "state": REINDEX_REQUIRED,
            "recommend_reindex": True,
            "requires_confirmation": True,
            "reason": "no index available (missing/corrupted)",
            "detail": {},
        }
    return evaluate_index_state(project_root, index)


__all__ = [
    "VALID",
    "STALE_RECOMMENDED",
    "AUTO_UPDATED",
    "REINDEX_REQUIRED",
    "freshness_config",
    "current_source_fingerprint",
    "current_scope_fingerprint",
    "current_build_fingerprint",
    "changed_source_files",
    "classify_source_change",
    "evaluate_index_state",
    "freshness_block",
]
