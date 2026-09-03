"""Build Reality 查询接口（context.py）。

- file_build：单文件编译状态（compiled / excluded / not_in_project / unknown）
- build_status：工程构建汇总（target/cpu/defines/编译统计）
- build_query：query type=build 的返回结构（exists/compiled/excluded/target/defines）
"""

from __future__ import annotations

from typing import Any

from agentx.build.models import KeilProject

STATUS_COMPILED = "compiled"
STATUS_EXCLUDED = "excluded"
STATUS_NOT_IN_PROJECT = "not_in_project"
STATUS_UNKNOWN = "unknown"


def _norm(p: str) -> str:
    return p.replace("\\", "/").strip("/").lower()


def file_in_build(project: KeilProject, file_path: str) -> str:
    """文件在 active target 中的编译状态（不编造：未知/不在工程如实返回）。"""
    if project.active_target is None:
        return STATUS_UNKNOWN
    needle = _norm(file_path)
    for f in project.active_target.compiled_files:
        norm = _norm(f.path)
        base = _norm(file_path.rsplit("/", 1)[-1])
        if norm in (needle, base) or norm.endswith("/" + needle):
            return STATUS_COMPILED
    for f in project.active_target.excluded_files:
        if _norm(f.path) == needle or _norm(f.path).endswith("/" + needle):
            return STATUS_EXCLUDED
    return STATUS_NOT_IN_PROJECT


def file_build(project: KeilProject, file_path: str) -> dict[str, Any]:
    """单文件 Build Reality 事实。"""
    status = file_in_build(project, file_path)
    result: dict[str, Any] = {
        "file": file_path,
        "exists": status in (STATUS_COMPILED, STATUS_EXCLUDED),
        "compiled": status == STATUS_COMPILED,
        "excluded": status == STATUS_EXCLUDED,
        "status": status,
        "target": project.target_name,
    }
    if project.target_cpu:
        result["cpu"] = project.target_cpu
    if project.defines:
        result["defines"] = project.defines
    return result


def build_status(project: KeilProject) -> dict[str, Any]:
    """工程构建汇总（无 Keil 工程时 build_status=unknown，不编造）。"""
    if project.active_target is None:
        return {"build_status": STATUS_UNKNOWN}
    compiled = project.active_target.compiled_files
    excluded = project.active_target.excluded_files
    out: dict[str, Any] = {
        "build_status": "valid",
        "system": "keil",
        "target": project.target_name,
        "compiled_files": [f.path for f in compiled],
        "excluded_files": [f.path for f in excluded],
        "compiled_count": len(compiled),
        "excluded_count": len(excluded),
        "project_file": project.project_file,
        "groups": [
            {"name": g.name, "files": [f.path for f in g.files]}
            for g in (project.active_target.groups or [])
        ],
    }
    if project.target_cpu:
        out["cpu"] = project.target_cpu
    if project.defines:
        out["defines"] = project.defines
    return out


def build_query(project: KeilProject, target_path: str) -> dict[str, Any]:
    """query type=build：回答"这个文件为什么没有效果"类问题。

    只给事实（exists/compiled/excluded/target/defines），不做推断。
    """
    return file_build(project, target_path)


def build_status_from_info(build_info: dict[str, Any]) -> dict[str, Any]:
    """从 Index 落库的 build_info 汇总构建状态（无工程时 build_status=unknown）。"""
    if not build_info or build_info.get("system") in (None, "unknown"):
        return {"build_status": STATUS_UNKNOWN}
    compiled = build_info.get("compiled_files") or []
    excluded = build_info.get("excluded_files") or []
    out: dict[str, Any] = {
        "build_status": "valid",
        "system": str(build_info.get("system", "unknown")),
        "target": build_info.get("target") or "",
        "compiled_count": len(compiled),
        "excluded_count": len(excluded),
        "defines": list(build_info.get("defines") or []),
        "project_file": build_info.get("project_file") or "",
    }
    if build_info.get("cpu"):
        out["cpu"] = str(build_info["cpu"])
    return out


def build_query_from_info(build_info: dict[str, Any], target_path: str) -> dict[str, Any]:
    """从 Index 落库的 build_info 做单文件 Build Query（不重新解析工程）。"""
    needle = _norm(target_path)
    base = target_path.rsplit("/", 1)[-1].lower()
    compiled = {_norm(e["file"]) for e in (build_info.get("compiled_files") or [])}
    excluded = {_norm(e["file"]) for e in (build_info.get("excluded_files") or [])}
    if not compiled and not excluded:
        return {
            "file": target_path,
            "build_status": STATUS_UNKNOWN,
            "exists": False,
            "compiled": False,
            "excluded": False,
        }
    in_compiled = _in_set(needle, base, compiled)
    in_excluded = _in_set(needle, base, excluded)
    result: dict[str, Any] = {
        "file": target_path,
        "exists": in_compiled or in_excluded,
        "compiled": in_compiled and not in_excluded,
        "excluded": in_excluded and not in_compiled,
        "status": _status_of(in_compiled, in_excluded),
        "target": str(build_info.get("target") or ""),
    }
    if build_info.get("cpu"):
        result["cpu"] = str(build_info["cpu"])
    if build_info.get("defines"):
        result["defines"] = list(build_info["defines"])
    return result


def _in_set(needle: str, base: str, pool: set[str]) -> bool:
    """路径命中判定：完全匹配、路径尾匹配或子串包含。"""
    if needle in pool or base in pool:
        return True
    return any(needle in c or c.endswith("/" + needle) for c in pool)


def _status_of(in_compiled: bool, in_excluded: bool) -> str:
    """编译/排除状态优先级：compiled > excluded > not_in_project。"""
    if in_compiled and not in_excluded:
        return STATUS_COMPILED
    if in_excluded:
        return STATUS_EXCLUDED
    return STATUS_NOT_IN_PROJECT
