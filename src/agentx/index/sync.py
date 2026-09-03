"""IndexSyncManager：代码变化后维护 Index，保持项目数字孪生最新。

流程：Diff Collector → Impact Analyzer（分级）→ Index Sync → Fingerprint Update。

Change Level：
- L0 注释/格式        → 更新 fingerprint + file hash（不重建知识）
- L1 函数内部修改      → 同上
- L2 签名/symbol 变化  → CodeGraph sync → 重新落库（enrich）
- L3 增删文件/include  → CodeGraph sync → 重新落库
- L4 构建配置变化       → CodeGraph sync + Build Reality 刷新 → 重新落库

原则：
- sync 即更新 fingerprint（fingerprint 描述事实，不描述质量）
- 小改动绝不全量重扫；保守策略：无法确定 → 升级处理
- git diff 优先，无 git 降级为文件 hash 比对
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
    """任务前置检查：Fingerprint Check → STALE 时 Index Sync。

    返回 (status, reason, sync_result)。VALID → 直接返回；STALE →
    执行 sync_index（外部变化时生成报告）；MISSING/CORRUPTED → 原样返回。
    """
    from agentx.index.index import index_status

    root = project_root.resolve()
    status, reason = index_status(root)
    if status.value != "STALE":
        return status.value, reason, None
    result = sync_index(root, origin=origin, progress=progress)
    return status.value, f"Index 已同步（{result['message']}）", result


def sync_index(
    project_root: Path,
    diff: str | None = None,
    origin: str = "unknown",
    progress: Any = None,
    scope_selections: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Index Sync：按 Change Level 分级维护 Index，sync 即更新 fingerprint。

    检测到非 AgentX 执行产生的变化（origin != agentx_execution）时，
    生成用户可见变更报告（.agentx/change_report.md + .json）。

    Scope 前置条件：rebuild（enrich → create_index）前必须通过 check_scope_init；
    无配置且有建议且未确认 → 返回 action="scope_required"（不重建）。
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
        # Phase 7.9.2 体验：MISSING 不 skip——统一 bootstrap（ensure_index 含 scope gate，
        # 此处 gate 已通过/已确认）→ 骨架 + enrich 补全认知
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

    # 旧指纹 = Index 记录的（对应上一次认知状态）；新指纹 = 当前磁盘
    fingerprint_before = index.project_fingerprint
    if diff is not None:
        level, changed, classified = _classify_diff_files(diff, index)
    else:
        level, changed, classified = classify_file_changes(root, index)

    if progress is not None:
        progress(f"Index Sync: {level}（{', '.join(changed[:5]) or '无变化'}）")

    if level in {ChangeLevel.L0, ChangeLevel.L1}:
        # 增量：更新 fingerprint + file hash，保留认知（symbols/call_graph/understanding）
        refreshed = refresh_index(root, index)
        for f in refreshed.files:
            h = _file_sha256(root, f.path)
            if h is not None:
                f.content_hash = h
        save_index(root, refreshed)
        action = "incremental"
        message = f"{level}：更新 fingerprint + file hash，保留认知"
    else:
        # 大变化：CodeGraph sync（pendingChanges 跳过优化）→ enrich 全量落库
        index, graph = enrich_index(root)
        action = "rebuild"
        message = (
            f"{level}：CodeGraph sync + 重新落库"
            f"（{graph.source}，{index.file_count} 文件，{len(index.symbols)} 符号）"
        )
        if progress is not None:
            progress(message)

    fingerprint_after = _cf(root, extra_excludes={index_exclude_name(root)})
    result: dict[str, Any] = {
        "level": str(level),
        "action": action,
        "changed_files": changed,
        "message": message,
        "origin": origin,
        "fingerprint_before": fingerprint_before,
        "fingerprint_after": fingerprint_after,
    }
    # 外部/未知来源变化 → 用户可见变更报告（agentx_execution 静默）
    if changed and origin != "agentx_execution":
        report_dir = write_change_report(
            root,
            origin=origin,
            level=str(level),
            action=action,
            modified=classified["modified"],
            added=classified["added"],
            removed=classified["removed"],
            fingerprint_before=fingerprint_before,
            fingerprint_after=fingerprint_after,
            message=message,
        )
        result["report_dir"] = str(report_dir)
    return result
