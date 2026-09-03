"""Scope Detector：agentx init 自动发现（三类建议，只提示不删除，Phase 7.8）。

检测规则：
- third_party 候选：目录名/路径段命中已知第三方特征（middlewares/thirdparty/
  cmsis/freertos/lvgl/hal 等），或被业务代码 include/调用
- ignore 候选：独立 Keil 工程（CPU 与主工程不同）/ .py 占比 > 80% /
  demo/example/test/documents/tools 等目录名
- 其余目录默认 project
"""

from __future__ import annotations

from pathlib import Path

from agentx.build.keil_parser import parse_keil_project

_SOURCE_SUFFIXES = {".c", ".h", ".cpp", ".hpp", ".cc", ".py", ".ts", ".js"}
_PY_SUFFIXES = {".py"}
_KEIL_SUFFIXES = {".uvprojx", ".uvproj"}

# 第三方库目录特征（路径段/目录名，小写匹配）
THIRD_PARTY_DIR_HINTS = {
    "middlewares",
    "middleware",
    "thirdparty",
    "third_party",
    "cmsis",
    "freertos",
    "rtos",
    "fatfs",
    "lvgl",
    "lwip",
    "segger",
    "touchgfx",
    "emwin",
    "stm32cube_fw",
    "components",
    "external",
}

# 第三方库前缀特征（目录名开头）
THIRD_PARTY_PREFIX_HINTS = (
    "lv_",
    "stm32f",
    "stm32h",
    "stm32g",
    "stm32l",
    "ff_",
)

_NAME_IGNORE_HINTS = {
    "demo": "示例/Demo 目录",
    "examples": "示例目录",
    "example": "示例目录",
    "test": "测试目录",
    "tests": "测试目录",
    "documents": "文档目录",
    "docs": "文档目录",
    "documentation": "文档目录",
    "tools": "工具目录",
    "samples": "示例目录",
    "backup": "备份目录",
    "build": "构建输出目录",
    "dist": "构建输出目录",
    "out": "构建输出目录",
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


def _is_third_party_dir(d: Path) -> bool:
    name = d.name.casefold()
    return name in THIRD_PARTY_DIR_HINTS or name.startswith(THIRD_PARTY_PREFIX_HINTS)


def detect_third_party(project_root: Path) -> list[dict[str, str]]:
    """检测可能的第三方库目录：[{"path", "name", "reason"}]。

    一级目录命中第三方特征时，若其下含更具体的第三方子目录
    （Middlewares/LVGL、Middlewares/FreeRTOS）→ 列出子目录（模块粒度更准）。
    """
    root = project_root.resolve()
    out: list[dict[str, str]] = []
    for d in sorted(root.iterdir()):
        if not d.is_dir() or d.name.startswith("."):
            continue
        if _is_third_party_dir(d):
            # 父目录为泛第三方目录（Middlewares）：优先列出具体子库
            subs = [
                sub
                for sub in sorted(d.iterdir())
                if sub.is_dir() and not sub.name.startswith(".") and _is_third_party_dir(sub)
            ]
            if subs:
                for sub in subs:
                    out.append(
                        {
                            "path": f"{d.name}/{sub.name}",
                            "name": sub.name,
                            "reason": "第三方库目录特征",
                        }
                    )
            else:
                out.append({"path": d.name, "name": d.name, "reason": "第三方库目录特征"})
        else:
            # 二级检测：非第三方父目录下的第三方子目录
            for sub in sorted(d.iterdir()):
                if sub.is_dir() and not sub.name.startswith(".") and _is_third_party_dir(sub):
                    out.append(
                        {
                            "path": f"{d.name}/{sub.name}",
                            "name": sub.name,
                            "reason": "第三方库目录特征",
                        }
                    )
    return out


def suggest_scopes(project_root: Path) -> dict[str, list[dict[str, str]]]:
    """完整建议：{"third_party": [...], "ignore": [...]}（只提示，不删除）。"""
    root = project_root.resolve()
    third_party = detect_third_party(root)
    main_cpu = _main_project_cpu(root)

    ignore: list[dict[str, str]] = []
    for d in sorted(root.iterdir()):
        if not d.is_dir() or d.name.startswith("."):
            continue
        name = d.name.casefold()

        # 独立 Keil 工程（CPU 与主工程不同）
        keil_cpu = _dir_keil_cpu(d)
        if keil_cpu is not None and main_cpu is not None and keil_cpu != main_cpu:
            ignore.append(
                {"path": d.name, "reason": f"独立 Keil 工程（CPU {keil_cpu} ≠ 主工程 {main_cpu}）"}
            )
            continue

        # Python 工具目录
        ratio = _py_ratio(d)
        if ratio > 0.8 and len(list(d.rglob("*.py"))) >= 2:
            ignore.append({"path": d.name, "reason": f"Python 工具目录（.py 占比 {ratio:.0%}）"})
            continue

        # 目录名特征
        hint = _NAME_IGNORE_HINTS.get(name)
        if hint is not None:
            ignore.append({"path": d.name, "reason": hint})

    return {"third_party": third_party, "ignore": ignore}
