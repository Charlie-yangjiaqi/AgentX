"""Scope Resolver：文件 → (scope_type, scope_name) 分类（Phase 7.8）。

输入文件路径 → 输出：
    {"path": "Middlewares/LVGL/src/lv_obj.c",
     "scope_type": "third_party", "scope_name": "LVGL"}
    {"path": "User/main.c", "scope_type": "project", "scope_name": None}
    {"path": "tools/t.py", "scope_type": "ignored", "scope_name": None}

Pipeline 位置：scan → Scope Resolver → project/third_party/ignored → CodeGraph/Semantic/Module。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from agentx.scope.config import (
    SCOPE_IGNORED,
    SCOPE_PROJECT,
    SCOPE_THIRD_PARTY,
    load_scope_config,
    scope_of_file,
)

ScopeMap = dict[str, dict[str, Any]]  # path → {"scope_type", "scope_name"}


class ScopeResolver:
    """单次解析的 Scope 视图（配置缓存，避免重复读盘）。"""

    def __init__(self, project_root: Path) -> None:
        self.root = project_root.resolve()
        self.config = load_scope_config(self.root)

    def resolve(self, rel_path: str) -> dict[str, Any]:
        scope_type, scope_name = scope_of_file(rel_path, self.config)
        return {
            "path": rel_path.replace("\\", "/"),
            "scope_type": scope_type,
            "scope_name": scope_name,
        }

    def is_ignored(self, rel_path: str) -> bool:
        return scope_of_file(rel_path, self.config)[0] == SCOPE_IGNORED

    def classify_files(self, rel_paths: list[str]) -> ScopeMap:
        """批量分类：只返回非 ignored 文件（ignored 不进入 Index）。"""
        out: ScopeMap = {}
        for p in rel_paths:
            scope_type, scope_name = scope_of_file(p, self.config)
            if scope_type == SCOPE_IGNORED:
                continue
            out[p] = {"scope_type": scope_type, "scope_name": scope_name}
        return out


def resolve_project_scope(project_root: Path, rel_paths: list[str]) -> ScopeMap:
    """便捷入口：过滤 ignored 并返回 scope 标注。"""
    return ScopeResolver(project_root).classify_files(rel_paths)


__all__ = [
    "SCOPE_IGNORED",
    "SCOPE_PROJECT",
    "SCOPE_THIRD_PARTY",
    "ScopeResolver",
    "resolve_project_scope",
]
