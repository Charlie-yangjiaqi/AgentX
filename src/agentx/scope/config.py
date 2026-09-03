"""Scope Config：.agentxscope.yaml 解析与判定（Phase 7.8 三层输入范围模型）。

格式：
    project:
      include:
        - "User/**"
        - "Drivers/**"

    third_party:
      - path: "Middlewares/LVGL"
        name: "LVGL"
      - path: "Middlewares/FreeRTOS"
        name: "FreeRTOS"

    ignore:
      - "LT758_DEMO/**"
      - "tools/**"
      - "*.py"

优先级：ignore > third_party > project；一个文件只能属于一个 scope。

- project 未配置 include → 默认全部（除 third_party/ignore）为 project
- project 配置了 include → 白名单模式：仅匹配的文件为 project，
  其余不匹配任何段的文件不进入 Index
- 无 .agentxscope.yaml 时兼容旧 .agentxignore（仅 ignore 层）
"""

from __future__ import annotations

import fnmatch
from pathlib import Path
from typing import Any

SCOPE_CONFIG_FILENAME = ".agentxscope.yaml"
LEGACY_IGNORE_FILENAME = ".agentxignore"

SCOPE_PROJECT = "project"
SCOPE_THIRD_PARTY = "third_party"
SCOPE_IGNORED = "ignored"


def _unquote(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        return value[1:-1]
    return value


def _parse_third_party(item: Any) -> dict[str, str] | None:
    if isinstance(item, str):
        item = _unquote(item)
        if ":" in item:  # 内联 `- path: X` 形式
            key, _, value = item.partition(":")
            if key.strip() == "path":
                item = _unquote(value)
            else:
                return None
        path = item.strip("/")
        if not path:
            return None
        return {"path": path, "name": path.rsplit("/", 1)[-1]}
    if isinstance(item, dict):
        path = str(item.get("path", "")).strip("/")
        if not path:
            return None
        name = str(item.get("name") or path.rsplit("/", 1)[-1]).strip()
        return {"path": path, "name": name}
    return None


def parse_scope_config(text: str, legacy_ignore: str | None = None) -> dict[str, Any]:
    """解析 .agentxscope.yaml（简单 YAML 子集）；兼容旧 .agentxignore。"""
    config: dict[str, Any] = {
        "project_include": [],
        "project_include_set": False,
        "third_party": [],
        "ignore": [],
    }

    third_party: list[dict[str, str]] = []
    ignore: list[str] = []
    project_include: list[str] = []
    section: str | None = None
    third_item: dict[str, str] | None = None

    def _flush_third() -> None:
        nonlocal third_item
        if third_item is not None:
            third_party.append(third_item)
            third_item = None

    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.endswith(":") and not stripped.startswith("-"):
            name = stripped[:-1].strip()
            if name in ("project", "third_party", "ignore"):
                _flush_third()
                section = name
            continue
        if section is None:
            continue
        if stripped.startswith("-"):
            item = stripped[1:].strip()
            if section == "ignore":
                ignore.append(_unquote(item))
            elif section == "project":
                project_include.append(_unquote(item))
            elif section == "third_party":
                _flush_third()
                parsed = _parse_third_party(item)
                if parsed is not None:
                    third_item = parsed
            continue
        if section == "third_party" and ":" in stripped:
            key, _, value = stripped.partition(":")
            key, value = key.strip(), value.strip()
            if third_item is None:
                third_item = {}
            if key in ("path", "name"):
                third_item[key] = _unquote(value)
    _flush_third()

    config["project_include"] = project_include
    config["project_include_set"] = bool(text.strip() and "project:" in text)
    config["third_party"] = third_party
    config["ignore"] = ignore
    if legacy_ignore is not None:
        from agentx.scope.ignore import parse_ignore_file

        config["ignore"].extend(parse_ignore_file(legacy_ignore))
    return config


def load_scope_config(project_root: Path) -> dict[str, Any]:
    """读取项目 Scope 配置；.agentxscope.yaml 优先，兼容旧 .agentxignore。"""
    root = project_root.resolve()
    cfg_path = root / SCOPE_CONFIG_FILENAME
    if cfg_path.is_file():
        try:
            return parse_scope_config(cfg_path.read_text(encoding="utf-8-sig"))
        except OSError:
            pass
    legacy = root / LEGACY_IGNORE_FILENAME
    if legacy.is_file():
        try:
            return parse_scope_config("", legacy.read_text(encoding="utf-8-sig"))
        except OSError:
            pass
    return parse_scope_config("")


def _glob_match(rel_path: str, pattern: str) -> bool:
    p = rel_path.replace("\\", "/")
    pat = pattern.replace("\\", "/").strip("/")
    if not pat:
        return False
    if fnmatch.fnmatch(p, pat):
        return True
    if pat.endswith("/**"):
        return p.startswith(pat[: -len("/**")].rstrip("/") + "/")
    if "/" not in pat and "*" not in pat:
        return p == pat or p.startswith(pat + "/")
    return fnmatch.fnmatch(p, pat + "/*")


def scope_of_file(rel_path: str, config: dict[str, Any]) -> tuple[str, str | None]:
    """文件分类：返回 (scope_type, scope_name)。ignored 文件 scope_name=None。

    优先级：ignore > third_party > project。
    """
    p = rel_path.replace("\\", "/").strip("/")
    for pat in config.get("ignore", []):
        if _glob_match(p, pat):
            return SCOPE_IGNORED, None
    for tp in config.get("third_party", []):
        base = tp.get("path", "").strip("/")
        if p == base or p.startswith(base + "/"):
            return SCOPE_THIRD_PARTY, tp.get("name")
    if config.get("project_include_set"):
        for pat in config.get("project_include", []):
            if _glob_match(p, pat):
                return SCOPE_PROJECT, None
        return SCOPE_IGNORED, None  # 白名单模式：include 之外不进入
    return SCOPE_PROJECT, None


def ignored_paths(rel_path: str, config: dict[str, Any]) -> bool:
    return scope_of_file(rel_path, config)[0] == SCOPE_IGNORED


def ignored_top_dirs(config: dict[str, Any]) -> list[str]:
    """当前被忽略的顶层目录（诊断/Quality Report 用）。"""
    out: list[str] = []
    for raw in config.get("ignore", []):
        name = raw.replace("\\", "/").strip("/").split("/", 1)[0]
        if name and name not in out and "/" not in raw.strip("/"):
            out.append(name)
    return sorted(out)
