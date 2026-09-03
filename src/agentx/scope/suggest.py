"""Scope 建议：首次 init/index 时自动检测建议 ignore 的目录（只提示，不自动删除）。

规则：
1. 目录含独立 Keil 工程（*.uvprojx/*.uvproj）且 CPU 与主工程不同 → 建议 ignore
2. .py 占比 > 80%（且文件数 ≥ 2）→ 建议 ignore（Python 工具目录）
3. 目录名命中 demo/example/test/documents/tools/samples 等 → 建议 ignore
"""

from __future__ import annotations

from pathlib import Path

from agentx.build.keil_parser import parse_keil_project

_SOURCE_SUFFIXES = {".c", ".h", ".cpp", ".hpp", ".cc", ".py", ".ts", ".js"}
_PY_SUFFIXES = {".py"}
_KEIL_SUFFIXES = {".uvprojx", ".uvproj"}

_NAME_HINTS = {
    "demo": "示例/Demo 目录",
    "example": "示例目录",
    "examples": "示例目录",
    "test": "测试目录",
    "tests": "测试目录",
    "documents": "文档目录",
    "docs": "文档目录",
    "documentation": "文档目录",
    "tools": "工具目录",
    "samples": "示例目录",
}


def _main_project_cpu(root: Path) -> str | None:
    """主工程（顶层优先）CPU：用于对比子目录工程是否独立。"""
    for p in root.iterdir():
        if p.is_file() and p.suffix.lower() in _KEIL_SUFFIXES:
            proj = parse_keil_project(p)
            if proj.active_target is not None and proj.target_cpu:
                return str(proj.target_cpu)
    return None


def _dir_keil_cpu(d: Path) -> str | None:
    for p in d.rglob("*"):
        if p.is_file() and p.suffix.lower() in _KEIL_SUFFIXES:
            proj = parse_keil_project(p)
            if proj.active_target is not None and proj.target_cpu:
                return str(proj.target_cpu)
    return None


def _py_ratio(d: Path) -> float:
    src = [p for p in d.rglob("*") if p.is_file() and p.suffix.lower() in _SOURCE_SUFFIXES]
    if not src:
        return 0.0
    py = sum(1 for p in src if p.suffix.lower() in _PY_SUFFIXES)
    return py / len(src)


def suggest_ignores(project_root: Path) -> list[dict[str, str]]:
    """返回建议 ignore 的目录：[{"path": "LT758_DEMO", "reason": "..."}]。"""
    root = project_root.resolve()
    suggestions: list[dict[str, str]] = []
    main_cpu = _main_project_cpu(root)

    for d in sorted(root.iterdir()):
        if not d.is_dir() or d.name.startswith("."):
            continue
        name = d.name.casefold()

        # 规则 1：独立 Keil 工程（CPU 与主工程不同）
        keil_cpu = _dir_keil_cpu(d)
        if keil_cpu is not None and main_cpu is not None and keil_cpu != main_cpu:
            suggestions.append(
                {"path": d.name, "reason": f"独立 Keil 工程（CPU {keil_cpu} ≠ 主工程 {main_cpu}）"}
            )
            continue

        # 规则 2：Python 工具目录
        ratio = _py_ratio(d)
        if ratio > 0.8 and len(list(d.rglob("*.py"))) >= 2:
            suggestions.append(
                {"path": d.name, "reason": f"Python 工具目录（.py 占比 {ratio:.0%}）"}
            )
            continue

        # 规则 3：目录名特征
        hint = _NAME_HINTS.get(name)
        if hint is not None:
            suggestions.append({"path": d.name, "reason": hint})

    return suggestions
