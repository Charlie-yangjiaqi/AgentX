"""Project Scope Control（Index Pipeline Reliability，V1）。

.agentxignore（项目根）——只做 ignore（默认全部包含，与 .gitignore 语义一致）：

    # 注释
    ignore:
      - LT758_DEMO/**
      - tools/**
      - Documents/**
      - *.py

支持：目录（含前缀匹配）、文件、glob pattern（fnmatch 语义）。
所有扫描入口（指纹 / filescan / include / CodeGraph db 读取 / semantic）
必须经过 scope_filter，保证被忽略目录的内容不进入任何 Index 数据。
"""

from __future__ import annotations

from pathlib import Path

SCOPE_FILENAME = ".agentxignore"
LEGACY_IGNORE_FILENAME = ".agentxignore"


def parse_ignore_file(text: str) -> list[str]:
    """解析 .agentxignore：取 ignore: 段下的 `- pattern` 行（V1 只支持 ignore）。"""
    patterns: list[str] = []
    in_ignore = False
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("ignore:"):
            in_ignore = True
            rest = stripped[len("ignore:") :].strip()
            if rest.startswith("-"):
                patterns.append(rest[1:].strip())
            continue
        if in_ignore and stripped.startswith("-"):
            patterns.append(stripped[1:].strip())
        else:
            in_ignore = False  # 其他段（V1 不支持）退出 ignore 段
    return [p for p in patterns if p]


def load_ignore_patterns(project_root: Path) -> list[str]:
    """读取项目 .agentxignore；文件缺失返回空列表（默认全包含）。"""
    p = project_root.resolve() / SCOPE_FILENAME
    if not p.is_file():
        return []
    try:
        return parse_ignore_file(p.read_text(encoding="utf-8-sig"))
    except OSError:
        return []


def is_ignored(rel_path: str, patterns: list[str]) -> bool:
    """相对路径（/ 分隔）是否命中任一 ignore pattern。

    - 目录 pattern（LT758_DEMO）匹配其下所有文件
    - 无斜杠 glob（*.py）按任意深度 basename 匹配（gitignore 语义，Phase 8.1）
    - 含斜杠 glob（tools/**）按 fnmatch/前缀匹配
    """
    from agentx.scope.config import _ignore_match  # noqa: PLC2701

    return any(_ignore_match(rel_path, raw) for raw in patterns)


def scope_filter(project_root: Path, rel_paths: list[str]) -> list[str]:
    """Scope 过滤：保留未被 ignore 的路径（无 .agentxignore 时原样返回）。"""
    patterns = load_ignore_patterns(project_root)
    if not patterns:
        return list(rel_paths)
    return [p for p in rel_paths if not is_ignored(p, patterns)]


def ignored_dirs(project_root: Path) -> list[str]:
    """当前被忽略的顶层目录（诊断/Quality Report 用）。"""
    patterns = load_ignore_patterns(project_root)
    out: list[str] = []
    for raw in patterns:
        pat = raw.replace("\\", "/").strip("/")
        name = pat.split("/", 1)[0]
        if name and name not in out and "/" not in pat:
            out.append(name)
    return sorted(out)
