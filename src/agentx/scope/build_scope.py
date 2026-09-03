"""Build Scope：以 Keil 当前 Target 实际编译文件为主 Index 工程边界（Phase 7.10）。

核心原则：Build Reality > Scope 目录规则 > 普通文件发现。
一个自有源码文件是否属于"当前正在构建的固件"，由 Keil Active Target 的
source list 决定，而不是"它在这个仓库/目录里"。

分类（确定性，优先级从高到低，对每个文件只取一次）：
1. ignored            → 不进入 Index（用户显式排除，最高优先）
2. third_party        → third_party（用户显式边界，即使出现在 Target 也保持）
3. active build file  → project（当前 Target 实际编译，主 Index 最高可信来源）
4. 其他自有源码       → non_build（不属于当前 Target 的主工程代码；
                        ≠第三方 ≠ignore，保留在 files[] 但非主边界）

Build 边界 = Keil Active Target compiled_files（工程相对路径）∪ 其传递
include 的头文件（CodeGraph include_map，保证 build 源 include 的 API 头
不被误排到 non_build）。

Target 解析失败 / 多 Target 无法确定 → resolved=False / ambiguity=True，
调用方必须返回 build_target_required / build_scope_unknown，不得静默伪装。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from agentx.scope.config import (
    SCOPE_IGNORED,
    SCOPE_NON_BUILD,
    SCOPE_PROJECT,
    SCOPE_THIRD_PARTY,
    scope_of_file,
)

_KEIL_EXTS = {".uvprojx", ".uvproj"}
_SOURCE_EXTS = {".c", ".cc", ".cpp", ".cxx"}


def find_keil_project(project_root: Path) -> Path | None:
    """定位主 Keil 工程：顶层 > 常见工程子目录 > 递归（浅优先）。"""
    root = project_root.resolve()
    for p in root.iterdir():
        if p.is_file() and p.suffix.lower() in _KEIL_EXTS:
            return p
    for sub in ("Projects", "project", "prj", "MDK-ARM", "Keil", "EWARM", "build"):
        d = root / sub
        if d.is_dir():
            for p in d.iterdir():
                if p.is_file() and p.suffix.lower() in _KEIL_EXTS:
                    return p
    best: Path | None = None
    best_depth = 10**9
    for p in root.rglob("*"):
        if p.is_file() and p.suffix.lower() in _KEIL_EXTS:
            depth = len(p.relative_to(root).parts)
            if depth < best_depth:
                best, best_depth = p, depth
    return best


@dataclass
class KeilBuildView:
    """当前工程的 Keil 解析视图（供 Build Scope 决策）。"""

    project_file: Path | None = None
    system: str = "unknown"  # keil | cmake | ...（本层聚焦 keil）
    targets: list[str] = field(default_factory=list)
    target: str | None = None  # 选定的 Active Target
    resolved: bool = False  # Active Target 是否确定
    ambiguity: bool = False  # 多 Target 且未选定 → 需用户确认
    build_files: list[str] = field(default_factory=list)  # compiled（工程相对）
    excluded_files: list[str] = field(default_factory=list)
    has_build: bool = False


def resolve_keil_build(project_root: Path, target_name: str | None = None) -> KeilBuildView:
    """解析主 Keil 工程的 Active Target source list。

    - 显式 target_name → 确定（不存在则 resolved=False）
    - scope config 已配置 build.target → 用之（Phase 7.10 持久化选择）
    - 单 Target → 自动确定
    - 多 Target 无 SelectTargetNo/无显式/无配置 → ambiguity=True（要求用户确认）
    """
    from agentx.build import parse_keil_project
    from agentx.scope.config import load_scope_config

    root = project_root.resolve()
    keil = find_keil_project(root)
    view = KeilBuildView()
    if keil is None:
        return view
    view.project_file = keil
    if target_name is None:
        configured = load_scope_config(root).get("build_target")
        if configured:
            target_name = str(configured)
    project = parse_keil_project(keil, project_root=root, target_name=target_name)
    view.targets = [t.name for t in project.targets]
    if not project.targets:
        return view  # 解析失败：resolved=False（调用方降级处理）

    if target_name is not None:
        view.target = target_name if target_name in view.targets else None
        view.resolved = view.target is not None
    elif project.active_target is not None:
        # parser 已按 SelectTargetNo > 第一个 选定；若多 Target 且无
        # SelectTargetNo，parser 默认取第一个 → 此处视为 ambiguity（不自动猜）。
        if len(project.targets) > 1:
            # 无法从配置确定"当前"Target：即使 parser 取了第一个，也不静默用。
            # 只有显式 SelectTargetNo 或单 Target 才可信。
            view.target = project.active_target.name
            view.resolved = False
            view.ambiguity = True
            return view
        view.target = project.active_target.name
        view.resolved = True
    view.has_build = view.resolved and project.active_target is not None
    if project.active_target is not None:
        view.build_files = [f.path for f in project.active_target.compiled_files if f.path]
        view.excluded_files = [f.path for f in project.active_target.excluded_files if f.path]
    view.system = "keil"
    return view


def build_boundary_files(
    view: KeilBuildView,
    include_map: dict[str, list[str]],
    available_paths: set[str],
) -> set[str]:
    """Build 边界文件集：active target compiled ∪ 其传递 include 的头文件。

    include_map：codegraph 的 src → [被 include 文件]（含 .c 相互 include 的 h）。
    available_paths：磁盘上真实存在的工程相对路径（边界内的头必须真实存在，
    避免 include 名字与实际文件对不上时把不存在路径纳入主边界）。
    """
    if not view.resolved:
        return set()
    boundary: set[str] = set(view.build_files)
    # 头文件跟随：compile 的 .c 的同名 .h 一定进边界（Keil 未必列全头文件）
    for bf in list(boundary):
        stem = Path(bf)
        if stem.suffix.lower() in _SOURCE_EXTS:
            h = str(stem.with_suffix(".h")).replace("\\", "/")
            if h in available_paths:
                boundary.add(h)
    # 传递 include：从 build 源出发，沿 include_map 收集头
    frontier = list(boundary)
    while frontier:
        nxt: list[str] = []
        for f in frontier:
            for inc in include_map.get(f, []) or []:
                if inc in available_paths and inc not in boundary:
                    boundary.add(inc)
                    nxt.append(inc)
        frontier = nxt
    return boundary


def classify_build_scope(
    project_root: Path,
    rel_paths: list[str],
    view: KeilBuildView,
    include_map: dict[str, list[str]],
) -> dict[str, dict[str, Any]]:
    """批量分类：path → {scope_type, scope_name}。

    每个文件只按一次优先级判定（ignored > third_party > build-project > non_build）。
    返回所有非 ignored 文件（ignored 不进入 Index，调用方据此丢弃）。
    """
    from agentx.scope.resolver import ScopeResolver

    resolver = ScopeResolver(project_root)
    cfg = resolver.config
    out: dict[str, dict[str, Any]] = {}
    # 仅当 build 确定时才启用 build 边界；否则退回纯 scope 目录规则
    boundary: set[str] = set()
    if view.resolved:
        available = {p for p in rel_paths if p}
        boundary = build_boundary_files(view, include_map, available)
    for p in rel_paths:
        p_norm = p.replace("\\", "/")
        scope_type, scope_name = scope_of_file(p_norm, cfg)
        if scope_type == SCOPE_IGNORED:
            continue  # ignored 不进入 Index
        if scope_type == SCOPE_THIRD_PARTY:
            out[p_norm] = {"scope_type": SCOPE_THIRD_PARTY, "scope_name": scope_name}
            continue
        # 到此处：scope 目录规则说 project（白名单模式下未被 ignore 即已命中 include）。
        if view.resolved:
            if p_norm in boundary:
                out[p_norm] = {"scope_type": SCOPE_PROJECT, "scope_name": None}
            else:
                out[p_norm] = {"scope_type": SCOPE_NON_BUILD, "scope_name": None}
        else:
            out[p_norm] = {"scope_type": SCOPE_PROJECT, "scope_name": None}
    return out


def build_scope_summary(
    view: KeilBuildView,
    classified: dict[str, dict[str, Any]],
    ignored_count: int,
) -> dict[str, Any]:
    """Scope Wizard / 报告用汇总。"""
    project_count = sum(1 for v in classified.values() if v["scope_type"] == SCOPE_PROJECT)
    non_build_count = sum(1 for v in classified.values() if v["scope_type"] == SCOPE_NON_BUILD)
    third_count = sum(1 for v in classified.values() if v["scope_type"] == SCOPE_THIRD_PARTY)
    return {
        "build_system": view.system,
        "build_source": "keil" if view.project_file else None,
        "target": view.target,
        "targets": view.targets,
        "resolved": view.resolved,
        "ambiguity": view.ambiguity,
        "build_files": len(view.build_files),
        "project": project_count,
        "non_build": non_build_count,
        "third_party": third_count,
        "ignored": ignored_count,
    }
