"""Scope Control（Phase 7.8）：三层输入范围模型 project / third_party / ignore。

- config：.agentxscope.yaml 解析与判定（优先级 ignore > third_party > project）
- resolver：文件 → (scope_type, scope_name) 分类
- detector：agentx init 自动发现建议
- ignore：旧 .agentxignore 兼容（单层 ignore）
- build_scope（Phase 7.10）：Build Reality > Scope 目录规则 > 文件发现；
  以 Keil Active Target source list 为主 Index 工程边界，未参与编译的
  自有代码标记 non_build，不默认进主 project。
"""

from __future__ import annotations

from agentx.scope.build_scope import (
    KeilBuildView,
    build_boundary_files,
    build_scope_summary,
    classify_build_scope,
    find_keil_project,
    resolve_keil_build,
)
from agentx.scope.config import (
    SCOPE_CONFIG_FILENAME,
    SCOPE_IGNORED,
    SCOPE_NON_BUILD,
    SCOPE_PROJECT,
    SCOPE_THIRD_PARTY,
    compute_scope_fingerprint,
    load_scope_config,
    scope_of_file,
)
from agentx.scope.detector import detect_third_party, suggest_scopes
from agentx.scope.ignore import (
    LEGACY_IGNORE_FILENAME,
    is_ignored,
    load_ignore_patterns,
    scope_filter,
)
from agentx.scope.resolver import ScopeResolver, resolve_project_scope

__all__ = [
    "SCOPE_IGNORED",
    "SCOPE_PROJECT",
    "SCOPE_THIRD_PARTY",
    "SCOPE_NON_BUILD",
    "SCOPE_CONFIG_FILENAME",
    "LEGACY_IGNORE_FILENAME",
    "load_scope_config",
    "scope_of_file",
    "compute_scope_fingerprint",
    "ScopeResolver",
    "resolve_project_scope",
    "detect_third_party",
    "suggest_scopes",
    "is_ignored",
    "load_ignore_patterns",
    "scope_filter",
    "KeilBuildView",
    "find_keil_project",
    "resolve_keil_build",
    "build_boundary_files",
    "classify_build_scope",
    "build_scope_summary",
]
