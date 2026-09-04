"""Project Fingerprint：Index 与当前项目状态的可验证映射。

硬规则：任何被 AgentX 使用的 Index 都必须能证明它对应当前项目状态。
Fingerprint = hash(相关文件路径 + 文件内容 + 重要项目配置)。
"""

from __future__ import annotations

import hashlib
from pathlib import Path

# 参与指纹的构建/配置文件名
CONFIG_FILES = {
    "Makefile",
    "CMakeLists.txt",
    "pyproject.toml",
    "Cargo.toml",
    "package.json",
    "go.mod",
    "pom.xml",
    "build.gradle",
    "meson.build",
    "configure.ac",
}

# 参与指纹的源码扩展名
SOURCE_EXTS = {
    ".c",
    ".h",
    ".hpp",
    ".cc",
    ".cpp",
    ".py",
    ".ts",
    ".tsx",
    ".js",
    ".jsx",
    ".rs",
    ".go",
    ".java",
    ".kt",
    ".swift",
    ".rb",
    ".cs",
    ".toml",
    ".yaml",
    ".yml",
}

# 代码内容扩展名（Freshness 判定用）：排除配置文件型（yaml/toml/json）。
# 这些文件的变化才可能进入"代码修改"决策；纯配置（scope yaml / pyproject.toml）
# 由各自的指纹（scope_fingerprint / build fp）独立归因，不混进 source_fingerprint。
CODE_SOURCE_EXTS = SOURCE_EXTS - {".toml", ".yaml", ".yml"}

# 排除目录
EXCLUDE_DIRS = {
    ".git",
    ".agentx",
    ".venv",
    "node_modules",
    "build",
    "dist",
    "target",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
}


def _walk_code_files(
    project_root: Path, extra_excludes: set[str] | None = None
) -> list[Path]:
    """递归收集参与 source 指纹的代码文件（scope-agnostic：不应用 scope ignore）。

    Phase 8.2：source_fingerprint 必须与 scope 独立归因——scope 配置变化不得伪装成
    源码变化。因此此处不调用 ScopeResolver（scope 归因由 scope_fingerprint 单独承担）。
    """
    root = project_root.resolve()
    excludes = EXCLUDE_DIRS | (extra_excludes or set())
    files: list[Path] = []
    for p in root.rglob("*"):
        if not p.is_file():
            continue
        if any(part in excludes for part in p.parts):
            continue
        if p.suffix in CODE_SOURCE_EXTS:
            files.append(p)
    files.sort(key=lambda p: str(p).lower())
    return files


def compute_source_fingerprint(
    project_root: Path, max_files: int = 2000, extra_excludes: set[str] | None = None
) -> str:
    """代码内容指纹：源码文件内容（scope-agnostic，独立归因）。

    与 compute_fingerprint 的区别：
    - 不含配置文件内容（scope yaml / uvprojx / Makefile 等）——那些分别由
      scope_fingerprint / build_scope_fingerprint 独立归因
    - 不含 scope ignore 过滤——scope 变化（增删 ignore/third_party）不得改变
      source_fingerprint（Phase 8.2 独立归因原则）
    用于 Freshness 的 source 变化幅度判定。
    """
    from agentx.index.index import index_exclude_name

    root = project_root.resolve()
    files = _walk_code_files(
        root, extra_excludes=(extra_excludes or set()) | {index_exclude_name(root)}
    )
    digester = hashlib.sha256()
    for p in files[:max_files]:
        rel = str(p.relative_to(root)).replace("\\", "/")
        digester.update(rel.encode("utf-8"))
        try:
            data = p.read_bytes()
        except OSError:
            data = b""
        digester.update(hashlib.sha256(data).digest())
    return digester.hexdigest()[:8]


def compute_fingerprint(
    project_root: Path, max_files: int = 2000, extra_excludes: set[str] | None = None
) -> str:
    """计算当前项目指纹。

    相关文件 = 源码文件 + 构建配置文件（排除 .agentx / .git / 依赖目录 /
    索引目录 <项目名>_codebase_index 等，以及 Scope 声明 ignore 的文件）。
    只取内容哈希，不存路径清单——路径变化同样导致指纹变化。
    """
    from agentx.scope.resolver import ScopeResolver

    root = project_root.resolve()
    excludes = EXCLUDE_DIRS | (extra_excludes or set())
    resolver = ScopeResolver(root)
    digester = hashlib.sha256()
    files: list[Path] = []
    for p in root.rglob("*"):
        if not p.is_file():
            continue
        if any(part in excludes for part in p.parts):
            continue
        if p.suffix in SOURCE_EXTS or p.name in CONFIG_FILES:
            rel = str(p.relative_to(root)).replace("\\", "/")
            if resolver.is_ignored(rel):
                continue
            files.append(p)
    files.sort(key=lambda p: str(p).lower())
    for p in files[:max_files]:
        rel = str(p.relative_to(root)).replace("\\", "/")
        digester.update(rel.encode("utf-8"))
        try:
            data = p.read_bytes()
        except OSError:
            data = b""
        digester.update(hashlib.sha256(data).digest())
    return digester.hexdigest()[:8]


def relevant_files(project_root: Path, extra_excludes: set[str] | None = None) -> list[str]:
    """当前参与指纹的文件清单（相对路径），供 Index 记录 file_count 等。

    Scope 配置文件（.agentxscope.yaml/.agentxignore）不进入 Index files
    （指纹计算仍包含它们，配置变化会触发重建）。
    """
    from agentx.scope.config import LEGACY_IGNORE_FILENAME, SCOPE_CONFIG_FILENAME
    from agentx.scope.resolver import ScopeResolver

    root = project_root.resolve()
    excludes = EXCLUDE_DIRS | (extra_excludes or set())
    resolver = ScopeResolver(root)
    out: list[str] = []
    for p in root.rglob("*"):
        if not p.is_file():
            continue
        if any(part in excludes for part in p.parts):
            continue
        if p.name in (SCOPE_CONFIG_FILENAME, LEGACY_IGNORE_FILENAME):
            continue
        if p.suffix in SOURCE_EXTS or p.name in CONFIG_FILES:
            rel = str(p.relative_to(root)).replace("\\", "/")
            if resolver.is_ignored(rel):
                continue
            out.append(rel)
    out.sort()
    return out
