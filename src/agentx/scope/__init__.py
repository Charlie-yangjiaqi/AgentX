"""Scope Control（Phase 7.8）：三层输入范围模型 project / third_party / ignore。

- config：.agentxscope.yaml 解析与判定（优先级 ignore > third_party > project）
- resolver：文件 → (scope_type, scope_name) 分类
- detector：agentx init 自动发现建议
- ignore：旧 .agentxignore 兼容（单层 ignore）
"""

from __future__ import annotations

from agentx.scope.config import (
    SCOPE_CONFIG_FILENAME,
    SCOPE_IGNORED,
    SCOPE_PROJECT,
    SCOPE_THIRD_PARTY,
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
    "SCOPE_CONFIG_FILENAME",
    "LEGACY_IGNORE_FILENAME",
    "load_scope_config",
    "scope_of_file",
    "ScopeResolver",
    "resolve_project_scope",
    "detect_third_party",
    "suggest_scopes",
    "is_ignored",
    "load_ignore_patterns",
    "scope_filter",
]
