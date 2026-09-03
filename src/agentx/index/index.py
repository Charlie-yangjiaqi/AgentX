"""Project Index：AgentX 对项目的长期认知资产。

位置：<项目根>/<项目名>_codebase_index/index.json
硬规则：任何被使用的 Index 必须能证明对应当前项目状态（Fingerprint）。
状态：VALID / STALE / MISSING / CORRUPTED。
"""

from __future__ import annotations

import enum
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from agentx.state.models import AgentXModel

INDEX_FILENAME = "index.json"
SCHEMA_VERSION = "2"
INDEX_VERSION = (
    "1.6"  # 认知版本：1.2 符号/调用 → 1.3 工程理解 → 1.4 语义 → 1.5 模块 → 1.6 三层 Scope
)
INDEX_DIR_SUFFIX = "_codebase_index"


class IndexStatus(enum.StrEnum):
    VALID = "VALID"
    STALE = "STALE"
    MISSING = "MISSING"
    CORRUPTED = "CORRUPTED"


class IndexFileMeta(AgentXModel):
    path: str
    status: str = "active"  # active | orphaned
    compiled: bool = False  # 兼容旧字段（migration 语义）
    compile_status: str = "unknown"  # compiled | not_compiled | excluded | unknown
    build_source: str | None = None
    content_hash: str | None = None
    referenced: bool = True
    # Phase 7.8：三层 Scope（旧 Index 无此字段 → 默认 project，兼容读取）
    scope_type: str = "project"  # project | third_party
    scope_name: str | None = None  # third_party 的模块名（如 LVGL）


class ProjectIndex(AgentXModel):
    """Project Index 文件内容（含 CodeGraph/Build Reality 融合认知）。"""

    project_fingerprint: str
    index_version: str
    generated_at: datetime
    schema_version: str = SCHEMA_VERSION
    file_count: int = 0
    files: list[IndexFileMeta] = []
    modules: list[dict[str, Any]] = []
    symbols: list[dict[str, Any]] = []
    dependencies: list[dict[str, Any]] = []
    call_graph: list[dict[str, Any]] = []
    indirect_calls: list[dict[str, Any]] = []  # Phase 7.7.3：函数注册/绑定事实（不承诺调用）
    type_semantics: dict[str, Any] = {}  # Phase 7.7.4：类型语义（structs/enums/macros/usage）
    build_info: dict[str, Any] = {}
    tests: dict[str, Any] = {}
    relationships: list[dict[str, Any]] = []
    include_map: dict[str, list[str]] = {}
    plan_summary: str | None = None
    codegraph_source: str | None = None
    project_understanding: dict[str, Any] | None = None
    capabilities: dict[str, Any] = {}  # 能力状态（semantic/module enabled+reason），区别于 errors
    # Phase 8.1：scope 是 Index 语义的一级依赖——记录生成时用的 scope 配置指纹，
    # scope 变化必须强制 reclassify + enrich（不靠源码增删启发式）。
    scope_fingerprint: str | None = None
    errors: list[str] = []


def index_dir(project_root: Path) -> Path:
    """索引目录：<项目根>/<项目名>_codebase_index/。"""
    root = project_root.resolve()
    return root / f"{root.name}{INDEX_DIR_SUFFIX}"


def index_path(project_root: Path) -> Path:
    return index_dir(project_root) / INDEX_FILENAME


def index_exclude_name(project_root: Path) -> str:
    """指纹计算需要排除索引目录自身，避免索引更新改变指纹（死循环）。"""
    return f"{project_root.resolve().name}{INDEX_DIR_SUFFIX}"


def load_index(project_root: Path) -> ProjectIndex | None:
    """读取 Index；文件不存在或解析失败返回 None。"""
    p = index_path(project_root)
    if not p.exists():
        return None
    try:
        return ProjectIndex.model_validate_json(p.read_text(encoding="utf-8-sig"))
    except Exception:
        return None


def _read_index(project_root: Path) -> tuple[ProjectIndex | None, bool]:
    """读取 Index 并区分状态：返回 (index, 文件是否存在且可解析)。

    - (None, True)  → MISSING（文件不存在）
    - (None, False) → CORRUPTED（存在但解析失败）
    - (index, True) → 存在且可解析（VALID 或 STALE 由指纹决定）
    """
    p = index_path(project_root)
    if not p.exists():
        return None, True
    try:
        return ProjectIndex.model_validate_json(p.read_text(encoding="utf-8-sig")), True
    except Exception:
        return None, False


def save_index(project_root: Path, index: ProjectIndex) -> Path:
    p = index_path(project_root)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(index.model_dump_json(indent=2), encoding="utf-8")
    return p


def _fingerprint(project_root: Path) -> str:
    from agentx.index.fingerprint import compute_fingerprint as _cf

    return _cf(project_root, extra_excludes={index_exclude_name(project_root)})


def index_status(project_root: Path) -> tuple[IndexStatus, str]:
    """判断 Index 状态：VALID / STALE / MISSING / CORRUPTED。

    返回 (状态, 说明)。指纹一致才算 VALID——不一致就是 STALE。
    """
    index, ok = _read_index(project_root)
    if index is None:
        return IndexStatus.MISSING if ok else IndexStatus.CORRUPTED, (
            "项目没有 Index" if ok else "Index 无法解析"
        )
    if index.schema_version != SCHEMA_VERSION:
        return IndexStatus.CORRUPTED, "schema 版本不兼容"
    try:
        current = _fingerprint(project_root)
    except OSError as e:
        return IndexStatus.CORRUPTED, f"无法计算指纹: {e}"
    if index.project_fingerprint == current:
        return IndexStatus.VALID, f"指纹一致 ({current})"
    return IndexStatus.STALE, f"指纹不一致: Index={index.project_fingerprint} 当前={current}"


def create_index(
    project_root: Path,
    *,
    files: list[dict[str, Any]] | None = None,
    build_info: dict[str, Any] | None = None,
    symbols: list[dict[str, Any]] | None = None,
    call_graph: list[dict[str, Any]] | None = None,
    include_map: dict[str, list[str]] | None = None,
    codegraph_source: str | None = None,
    errors: list[str] | None = None,
) -> ProjectIndex:
    """创建/重建 Index：记录当前指纹与融合认知。"""
    root = project_root.resolve()
    fingerprint = _fingerprint(root)
    from agentx.index.fingerprint import relevant_files as _rf

    all_files = _rf(root, extra_excludes={index_exclude_name(root)})
    if files is None:
        metas = [IndexFileMeta(path=f, status="active", compiled=False) for f in all_files]
    else:
        metas = [_meta_from_dict(f) for f in files]
    return ProjectIndex(
        project_fingerprint=fingerprint,
        index_version=INDEX_VERSION,
        generated_at=datetime.now(UTC),
        file_count=len(metas),
        files=metas,
        symbols=symbols or [],
        call_graph=call_graph or [],
        build_info=build_info or {},
        include_map=include_map or {},
        codegraph_source=codegraph_source,
        errors=errors or [],
    )


def _meta_from_dict(data: dict[str, Any]) -> IndexFileMeta:
    """把理解层的文件条目转成 IndexFileMeta，标记编译/孤儿/Scope 状态。"""
    return IndexFileMeta(
        path=str(data.get("path", "")),
        status=str(data.get("status", "active")),
        compiled=bool(data.get("compiled", False)),
        compile_status=str(data.get("compile_status", "unknown")),
        build_source=str(data.get("build_source")) if data.get("build_source") else None,
        content_hash=str(data.get("content_hash")) if data.get("content_hash") else None,
        referenced=bool(data.get("referenced", True)),
        scope_type=str(data.get("scope_type", "project") or "project"),
        scope_name=str(data.get("scope_name")) if data.get("scope_name") else None,
    )


def refresh_index(project_root: Path, index: ProjectIndex | None = None) -> ProjectIndex:
    """更新 Index 到当前项目状态（骨架部分），保留已有认知内容。"""
    root = project_root.resolve()
    fingerprint = _fingerprint(root)
    from agentx.index.fingerprint import relevant_files as _rf

    all_files = _rf(root, extra_excludes={index_exclude_name(root)})
    if index is None:
        return create_index(root)
    index.project_fingerprint = fingerprint
    index.index_version = INDEX_VERSION
    index.generated_at = datetime.now(UTC)
    index.file_count = len(all_files)
    if not index.files:
        index.files = [IndexFileMeta(path=f, status="active", compiled=False) for f in all_files]
    return index


def _json_default(obj: Any) -> Any:
    if isinstance(obj, datetime):
        return obj.isoformat()
    raise TypeError(f"不可序列化: {type(obj)}")
