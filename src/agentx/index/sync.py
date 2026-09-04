"""IndexSyncManager：代码变化后维护 Index（Phase 8.2 Freshness 决策模型）。

核心原则：
- 小变化（1~N 个源码文件）→ 文件级增量更新（incremental_update），自动执行
- 中变化（scope 小影响 / build 小变化 / 少量模块关系）→ 自动 enrich（Level 2）
- 大变化（scope/build 大规模 / source>阈值 / 公共头大改 / 无法可靠增量）
  → 只标记 REINDEX_REQUIRED，不自动执行，由用户决定 reindex
- CODE_WRITE 永不隐式触发 full reindex；sync/status/query 的自动维护 ≤ Level 2

git diff 优先；无 git 降级为文件 hash 比对。
"""

from __future__ import annotations

import enum
import hashlib
import re
from pathlib import Path
from typing import Any

from agentx.index.index import ProjectIndex, load_index, refresh_index, save_index

# 构建配置文件（变化 → L4）
_BUILD_CONFIG_NAMES = {
    "Makefile",
    "CMakeLists.txt",
    "build.ninja",
    "meson.build",
    "compile_commands.json",
}
_BUILD_CONFIG_SUFFIXES = {".uvprojx", ".uvproj", ".ewp", ".ioc"}

_DIFF_NEW_FILE_RE = re.compile(r"^new file mode|^diff --git a/(\S+) b/(\S+)$")
_DIFF_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


def _is_build_config(rel_path: str) -> bool:
    base = rel_path.split("/")[-1]
    return base in _BUILD_CONFIG_NAMES or base.rsplit(".", 1)[-1].lower() in _BUILD_CONFIG_SUFFIXES


class ChangeLevel(enum.StrEnum):
    L0 = "L0"
    L1 = "L1"
    L2 = "L2"
    L3 = "L3"
    L4 = "L4"


def _file_sha256(root: Path, rel: str) -> str | None:
    try:
        return hashlib.sha256((root / rel).read_bytes()).hexdigest()[:16]
    except OSError:
        return None


# 注释/纯格式变更判定（git diff 的 +/- 行不改变任何代码语义）


def _diff_changed_files(diff: str) -> set[str]:
    """git diff → 出现变化的文件（b/ 侧路径）。"""
    files: set[str] = set()
    for raw in diff.splitlines():
        m = re.search(r"^diff --git a/\S+ b/(\S+)$", raw)
        if m:
            files.add(m.group(1))
    return files


def _diff_is_comment_only(diff: str) -> bool:

    def _lines_are_cosmetic(lines: list[str]) -> bool:
        for ln in lines:
            if not ln:
                continue
            # 删除注释块开始/结束、纯空行、纯括号行、纯注释 → cosmetic
            if ln.startswith("//") or ln.startswith("/*") or ln.startswith("*"):
                continue
            if ln.startswith("*/"):
                continue
            if ln.startswith("#") and not ln.lstrip("#").strip().startswith("include"):
                # 预处理条件编译指令（#if/#ifdef/#endif）视为非语义；#include 是语义
                return False
            if not ln.strip():
                continue
            if set(ln.strip()) <= {"}", "{", ";", " ", "\t"}:
                continue
            return False
        return True

    current: list[str] = []
    for raw in diff.splitlines():
        if raw.startswith("diff --git"):
            # flush previous
            if current and not _lines_are_cosmetic(current):
                return False
            current = []
            continue
        if raw.startswith("+++ b/") or raw.startswith("--- a/"):
            continue
        if raw.startswith("@@"):
            continue
        if raw.startswith("+") or raw.startswith("-"):
            if raw.startswith("+++") or raw.startswith("---"):
                continue
            current.append(raw[1:].strip())
    return not (current and not _lines_are_cosmetic(current))


def classify_diff(diff: str, index: ProjectIndex) -> tuple[ChangeLevel, list[str]]:
    """git diff → Change Level + 受影响文件。

    规则：
    - 无 diff → L0
    - 构建配置文件变化 → L4
    - 新增文件 → L3
    - 修改行触及符号定义行（函数签名）→ L2
    - include 行变化 → L3
    - 其余行级修改 → L1
    - 保守兜底：识别不清 → L3
    """
    if not diff or not diff.strip():
        return ChangeLevel.L0, []

    changed_files: set[str] = set()
    level = ChangeLevel.L1
    touched_old_lines: list[tuple[str, int]] = []  # (文件, 被修改的 old 行号)
    new_files: set[str] = set()

    current_file: str | None = None
    old_line = 0

    def _mark(line_no: int) -> None:
        if current_file:
            touched_old_lines.append((current_file, line_no))

    for raw in diff.splitlines():
        if raw.startswith("diff --git"):
            current_file = None
            m = _DIFF_NEW_FILE_RE.search(raw)
            if m:
                current_file = m.group(2)
                if _is_build_config(current_file) and level < ChangeLevel.L4:
                    level = ChangeLevel.L4
            continue
        if raw.startswith("new file mode"):
            if current_file:
                new_files.add(current_file)
            continue
        if raw.startswith("+++ b/") or raw.startswith("--- a/"):
            continue
        if current_file is None:
            if raw.startswith("+++ b/"):
                current_file = raw[6:]
            continue
        if raw.startswith("@@") and current_file:
            m = _DIFF_HUNK_RE.match(raw)
            old_line = int(m.group(1)) if m else 0
            continue
        if raw.startswith("+") and not raw.startswith("+++"):
            # 新增内容（new 侧）——include 变化 → L3
            if re.search(r"#\s*include\s*[<\"']", raw) and level < ChangeLevel.L3:
                level = ChangeLevel.L3
            continue
        if raw.startswith("-") and not raw.startswith("---"):
            # 删除/修改（old 侧）：记录被修改的 old 行号
            _mark(old_line)
            old_line += 1
        elif raw.startswith(" ") and not raw.startswith("+++"):
            old_line += 1

    # 修改行触及符号定义行（函数签名）→ L2
    for file, line_no in touched_old_lines:
        for sym in index.symbols:
            if str(sym.get("file", "")) != file:
                continue
            start = sym.get("start_line")
            if start is not None and int(start) == line_no:
                if level < ChangeLevel.L2:
                    level = ChangeLevel.L2
                changed_files.add(file)

    if new_files:
        changed_files.update(new_files)
        if level < ChangeLevel.L3:
            level = ChangeLevel.L3

    return level, sorted(changed_files)


def classify_file_changes(
    root: Path, index: ProjectIndex
) -> tuple[ChangeLevel, list[str], dict[str, list[str]]]:
    """无 git 降级：文件 hash 比对 → 变化文件（含分类）。

    只能判断"哪些文件变了"，无法区分 L1/L2 → 保守 L3（CodeGraph sync
    的 pendingChanges 机制只同步变化文件，成本可控）。
    构建配置文件变化 → L4。返回 (level, changed, {modified, added, removed})。
    """
    from agentx.index.fingerprint import SOURCE_EXTS
    from agentx.index.fingerprint import relevant_files as _rf
    from agentx.index.index import index_exclude_name

    modified: list[str] = []
    for f in index.files:
        current = _file_sha256(root, f.path)
        if current is not None and current != f.content_hash:
            modified.append(f.path)
    # 构建配置文件变化（无 git 时靠 config_hashes 记录检测）→ L4
    old_configs = index.build_info.get("config_hashes") or {}
    for name, old_hash in old_configs.items():
        current = _file_sha256(root, name)
        if current is not None and current != old_hash and name not in modified:
            modified.append(name)
    # 新增/删除源码文件（无 git 时 index.files 不含新文件，无法 hash 比对）
    current_src = {
        f
        for f in _rf(root, extra_excludes={index_exclude_name(root)})
        if Path(f).suffix.lower() in SOURCE_EXTS
    }
    known_src = {f.path for f in index.files if Path(f.path).suffix.lower() in SOURCE_EXTS}
    added = sorted(current_src - known_src)
    removed = sorted(known_src - current_src)
    changed = modified + added + removed
    if not changed:
        return ChangeLevel.L0, [], {"modified": [], "added": [], "removed": []}
    if any(_is_build_config(p) for p in changed):
        return ChangeLevel.L4, changed, {"modified": modified, "added": added, "removed": removed}
    return ChangeLevel.L3, changed, {"modified": modified, "added": added, "removed": removed}


def _classify_diff_files(
    diff: str, index: ProjectIndex
) -> tuple[ChangeLevel, list[str], dict[str, list[str]]]:
    """git diff → (level, changed, {modified, added, removed})。"""
    level, changed = classify_diff(diff, index)
    modified: list[str] = []
    added: list[str] = []
    removed: list[str] = []
    current_file: str | None = None
    is_new = False
    is_deleted = False
    for raw in diff.splitlines():
        if raw.startswith("diff --git"):
            if current_file:
                (added if is_new else removed if is_deleted else modified).append(current_file)
            current_file = None
            is_new = is_deleted = False
            m = re.search(r"^diff --git a/(\S+) b/(\S+)$", raw)
            if m:
                current_file = m.group(2)
            continue
        if raw.startswith("new file mode"):
            is_new = True
            continue
        if raw.startswith("deleted file mode"):
            is_deleted = True
            continue
    if current_file:
        (added if is_new else removed if is_deleted else modified).append(current_file)
    return level, changed, {"modified": modified, "added": added, "removed": removed}


def ensure_synced(
    project_root: Path,
    origin: str = "unknown",
    progress: Any = None,
) -> tuple[str, str, dict[str, Any] | None]:
    """任务前置检查：Freshness 判定 + 允许的自动更新（≤ Level 2）。

    返回 (index_status, reason, sync_result)：
    - VALID          → 直接返回
    - STALE(小/中)   → 自动增量更新后返回新状态（通常 VALID）；保留旧 Index 时返回 STALE
    - REINDEX_REQUIRED → 不自动重建，返回 STALE（调用方据 sync_result 判定）
    - MISSING/CORRUPTED → 原样返回
    """
    from agentx.index.index import index_status

    root = project_root.resolve()
    status, reason = index_status(root)
    if status.value != "STALE":
        return status.value, reason, None
    # 只做 freshness 判定；小变化自动增量；REQUIRED 不越权
    verdict = sync_index(root, origin=origin, progress=progress)
    if verdict.get("action") == "scope_required":
        return status.value, verdict.get("message", reason), verdict
    if verdict.get("action") == "reindex_required":
        return status.value, verdict.get("message", f"需完整重建: {reason}"), verdict
    # 自动更新（incremental/reclassify/fingerprint_only/created）后重查真实状态
    after, after_reason = index_status(root)
    return after.value, verdict.get("message", after_reason), verdict


def sync_index(
    project_root: Path,
    diff: str | None = None,
    origin: str = "unknown",
    progress: Any = None,
    scope_selections: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Index Sync（Phase 8.2 决策模型）：按变化幅度分级维护，绝不隐式 full reindex。

    决策（evaluate_index_state 判定后落地）：
    - REINDEX_REQUIRED（scope 大影响 / build 大变化 / source 大变化）→ 只标记，
      不重建，由用户决定（action=reindex_required）
    - scope 小影响 → 自动 reclassify + enrich（action=reclassify，AUTO 级）
    - source 小变化 → 文件级增量 incremental_update（action=incremental）
    - L0（无实际变化 / 注释）→ 刷新 fingerprint + hash（action=fingerprint_only）
    - MISSING → bootstrap 创建

    不做：任何"代码变化 → 全量 enrich"的隐式路径（那是 reindex 的职责）。
    """
    from agentx.index.fingerprint import compute_fingerprint as _cf
    from agentx.index.index import index_exclude_name
    from agentx.index.report import write_change_report
    from agentx.plan.service import enrich_index  # 延迟导入避免循环
    from agentx.scope.initializer import apply_scope_selections, check_scope_init

    root = project_root.resolve()
    gate = check_scope_init(root)
    if gate is not None and scope_selections is None:
        return {
            "level": "L0",
            "action": "scope_required",
            "changed_files": [],
            "message": gate["message"],
            "origin": origin,
            "scope_status": "scope_required",
            "scope_reason": gate["reason"],
            "suggestions": gate["suggestions"],
        }
    if gate is not None:
        apply_scope_selections(root, scope_selections)

    index = load_index(root)
    if index is None:
        # MISSING：bootstrap 统一（scope gate 已通过/已确认）→ 骨架 + enrich
        from agentx.plan.service import ensure_index as _ensure_index

        _ensure_index(root, scope_selections=scope_selections)
        enriched, _ = enrich_index(root)
        return {
            "level": "L0",
            "action": "created",
            "changed_files": [],
            "message": f"Index 不存在，已创建（{enriched.file_count} 文件）",
            "origin": origin,
            "index_after": {"status": "VALID", "file_count": enriched.file_count},
        }

    fingerprint_before = index.project_fingerprint
    # 权威 change set：hash 比对（哪些文件内容真变了——增量需要精确清单）。
    # git diff 只用于额外判定 L0（纯注释/格式变化）。
    level, _, classified = _classify_diff_files(diff, index) if diff is not None else (
        ChangeLevel.L0,
        [],
        {"modified": [], "added": [], "removed": []},
    )
    changed_level, changed, hash_classified = classify_file_changes(root, index)
    # hash 比对结果优先（更完整：增删改）；git 分类用于注释判定
    if changed_level != ChangeLevel.L0:
        classified = hash_classified
    modified, added, removed = (
        classified["modified"],
        classified["added"],
        classified["removed"],
    )
    changed = sorted(set(modified) | set(added) | set(removed))

    if progress is not None:
        progress(f"Index Sync: 评估 {len(changed)} 个变化文件")

    from agentx.index.freshness import (
        REINDEX_REQUIRED,
        evaluate_index_state,
    )

    # L0 判定：git diff 可用且纯注释/格式 → 直接刷指纹返回 VALID
    comment_only = False
    if changed and diff is not None:
        comment_only = _diff_is_comment_only(diff)
    if comment_only:
        refreshed = refresh_index(root, index)
        for f in refreshed.files:
            h = _file_sha256(root, f.path)
            if h is not None:
                f.content_hash = h
        save_index(root, refreshed)
        return {
            "level": "L0",
            "action": "fingerprint_only",
            "changed_files": changed,
            "message": "注释/格式变化，仅刷新 fingerprint（保留全部认知）",
            "origin": origin,
            "index_freshness": {
                "state": "VALID",
                "recommend_reindex": False,
                "requires_confirmation": False,
                "reason": "comment-only change",
            },
        }

    verdict = evaluate_index_state(root, index, changed=changed)

    # ---- 完整重建判定（不自动执行，只标记）----
    if verdict["state"] == REINDEX_REQUIRED:
        fingerprint_after = _cf(root, extra_excludes={index_exclude_name(root)})
        result: dict[str, Any] = {
            "level": "L4",
            "action": "reindex_required",
            "changed_files": changed,
            "message": verdict["reason"],
            "origin": origin,
            "fingerprint_before": fingerprint_before,
            "fingerprint_after": fingerprint_after,
            "index_freshness": verdict,
            "requires_confirmation": True,
        }
        # 外部变化 → 用户可见报告（agentx_execution 静默）
        if changed and origin != "agentx_execution":
            result["report_dir"] = str(
                write_change_report(
                    root,
                    origin=origin,
                    level=str(level),
                    action="reindex_required",
                    modified=classified["modified"],
                    added=classified["added"],
                    removed=classified["removed"],
                    fingerprint_before=fingerprint_before,
                    fingerprint_after=fingerprint_after,
                    message=verdict["reason"],
                )
            )
        return result

    # ---- scope 小影响：自动 reclassify + enrich（Level 2 自动）----
    if index.scope_fingerprint is not None:
        from agentx.scope.config import compute_scope_fingerprint

        if index.scope_fingerprint != compute_scope_fingerprint(root):
            index, graph = enrich_index(root)
            action = "reclassify"
            message = (
                "Scope 变化（小影响）：已自动重新分类 + enrich"
                f"（{graph.source}，{index.file_count} 文件，{len(index.symbols)} 符号）"
            )
            if progress is not None:
                progress(message)
            result = {
                "level": "L2",
                "action": action,
                "changed_files": changed,
                "message": message,
                "origin": origin,
                "scope_changed": True,
                "scope_fingerprint": index.scope_fingerprint,
                "index_after": {"status": "VALID", "file_count": index.file_count},
                "index_freshness": {
                    "state": "AUTO_UPDATED",
                    "recommend_reindex": False,
                    "requires_confirmation": False,
                    "reason": "scope change applied automatically (reclassify)",
                },
            }
            return result

    # ---- source 小变化：文件级增量 ----
    if not changed:
        # 无实际变化 → 只刷指纹（保留认知）
        refreshed = refresh_index(root, index)
        for f in refreshed.files:
            h = _file_sha256(root, f.path)
            if h is not None:
                f.content_hash = h
        save_index(root, refreshed)
        return {
            "level": "L0",
            "action": "fingerprint_only",
            "changed_files": [],
            "message": "无实际变化，仅刷新 fingerprint",
            "origin": origin,
            "index_freshness": {
                "state": "VALID",
                "recommend_reindex": False,
                "requires_confirmation": False,
                "reason": "no source change",
            },
        }

    # 文件级增量：只重扫变化文件（CodeGraph sync）→ semantic/type/module 局部刷新
    from agentx.index.incremental import incremental_update

    verdict_detail = incremental_update(
        root,
        modified=classified["modified"],
        added=classified["added"],
        removed=classified["removed"],
        changed=changed,
    )
    if verdict_detail.get("state") == REINDEX_REQUIRED:
        # 增量自身判定不可靠 → 升级（不写 Index）
        fingerprint_after = _cf(root, extra_excludes={index_exclude_name(root)})
        return {
            "level": "L4",
            "action": "reindex_required",
            "changed_files": changed,
            "message": verdict_detail["reason"],
            "origin": origin,
            "fingerprint_before": fingerprint_before,
            "fingerprint_after": fingerprint_after,
            "index_freshness": verdict_detail,
            "requires_confirmation": True,
        }

    fingerprint_after = _cf(root, extra_excludes={index_exclude_name(root)})
    result = {
        "level": str(level),
        "action": "incremental",
        "changed_files": changed,
        "message": verdict_detail.get(
            "reason", f"{level}：增量更新已应用（保留认知）"
        ),
        "origin": origin,
        "fingerprint_before": fingerprint_before,
        "fingerprint_after": fingerprint_after,
        "index_freshness": verdict_detail,
    }
    # 外部/未知来源变化 → 用户可见变更报告（agentx_execution 静默）
    if changed and origin != "agentx_execution":
        report_dir = write_change_report(
            root,
            origin=origin,
            level=str(level),
            action="incremental",
            modified=classified["modified"],
            added=classified["added"],
            removed=classified["removed"],
            fingerprint_before=fingerprint_before,
            fingerprint_after=fingerprint_after,
            message=verdict_detail.get("reason", "incremental update applied"),
        )
        result["report_dir"] = str(report_dir)
    return result
