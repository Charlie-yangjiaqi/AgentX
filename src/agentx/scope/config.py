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
SCOPE_NON_BUILD = "non_build"  # Phase 7.10：自有代码但不在当前 Keil Target（≠第三方≠ignore）


def _norm_base(raw: str) -> str:
    """归一化第三方/ignore 路径基准：去引号、正斜杠、去尾 /**（glob 写法兼容）。"""
    base = _unquote(raw).replace("\\", "/").strip("/")
    if base.endswith("/**"):
        base = base[: -len("/**")].rstrip("/")
    return base


def _unquote(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        return value[1:-1]
    return value


def normalize_scope_path(raw: str) -> str:
    """第三方/ignore 路径归一化：去引号、正斜杠、去首尾斜杠与尾 /**。

    写入 .agentxscope.yaml 前调用，保证第三方的 dir/** 与裸 dir 都落成 dir，
    matcher 前缀语义一致（Phase 7.10 修复：/** 形式此前导致第三方匹配失效）。
    """
    return _norm_base(raw)


def _parse_third_party(item: Any) -> dict[str, str] | None:
    if isinstance(item, str):
        item = _unquote(item)
        if ":" in item:  # 内联 `- path: X` 形式
            key, _, value = item.partition(":")
            if key.strip() == "path":
                item = _unquote(value)
            else:
                return None
        path = normalize_scope_path(item)
        if not path:
            return None
        return {"path": path, "name": path.rsplit("/", 1)[-1]}
    if isinstance(item, dict):
        path = normalize_scope_path(str(item.get("path", "")))
        if not path:
            return None
        name = str(item.get("name") or "").strip()
        # 兼容历史错误 name（旧 wizard 从 "Middlewares/**" 推导出 "**"）→ 由路径重推
        if not name or name == "**":
            name = path.rsplit("/", 1)[-1]
        return {"path": path, "name": name}
    return None


def parse_scope_config(text: str, legacy_ignore: str | None = None) -> dict[str, Any]:
    """解析 .agentxscope.yaml（简单 YAML 子集）；兼容旧 .agentxignore。

    支持 build 段（Phase 7.10）：build: { target: LVGL } —— 用户确认的
    Keil Active Target，供 Build Scope 决策。
    """
    config: dict[str, Any] = {
        "project_include": [],
        "project_include_set": False,
        "third_party": [],
        "ignore": [],
        "build_target": None,
    }

    third_party: list[dict[str, str]] = []
    ignore: list[str] = []
    project_include: list[str] = []
    section: str | None = None
    third_item: dict[str, str] | None = None

    def _flush_third() -> None:
        nonlocal third_item
        if third_item is not None:
            path = _norm_base(str(third_item.get("path", "")))
            if path:
                name = str(third_item.get("name") or "").strip()
                if not name or name == "**":  # 兼容历史错误 name（旧 wizard 产物）
                    name = path.rsplit("/", 1)[-1]
                third_item = {"path": path, "name": name}
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
            elif name == "build":
                _flush_third()
                section = "build"
            continue
        if section is None:
            continue
        if section == "build":
            # build: { target: LVGL }（单行内联）或 build:\n  target: LVGL
            if ":" in stripped:
                key, _, value = stripped.partition(":")
                if key.strip() == "target":
                    val = _unquote(value).strip()
                    if val:
                        config["build_target"] = val
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


def compute_scope_fingerprint(project_root: Path) -> str:
    """Scope 配置指纹：hash(ignore + third_party + project_include + build_target)。

    scope 是 Index 语义的一级依赖（Phase 8.1）。指纹变化 = 索引边界变化，
    必须强制 reclassify + enrich；不能依赖"源码增删"启发式判断。
    配置不存在 → 稳定空值指纹（确定性）。
    """
    import hashlib
    import json

    cfg = load_scope_config(project_root)
    canonical = {
        "ignore": sorted(cfg.get("ignore", [])),
        "third_party": sorted(
            f"{t.get('path', '')}|{t.get('name', '')}" for t in cfg.get("third_party", [])
        ),
        "project_include": sorted(cfg.get("project_include", [])),
        "project_include_set": bool(cfg.get("project_include_set")),
        "build_target": cfg.get("build_target"),
    }
    blob = json.dumps(canonical, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:8]


def _ignore_match(rel_path: str, pattern: str) -> bool:
    """ignore 模式匹配（gitignore 语义，Phase 8.1）：

    - 目录 pattern（LT758_DEMO / LT758_DEMO/**）→ 前缀（含子目录）
    - 无斜杠 glob（*.py、*.py/**）→ 匹配任意深度的 basename
      （*.py 命中 User/tool.py；*.py/** 归一为 *.py 同样命中）
    - 其余按 fnmatch
    """
    p = rel_path.replace("\\", "/")
    pat = pattern.replace("\\", "/").strip("/")
    if not pat:
        return False
    # 尾随 /** ：作用于其前缀（目录树或名称 glob）
    if pat.endswith("/**"):
        base = pat[: -len("/**")].rstrip("/")
        if base and "/" not in base and "*" in base:
            pat = base  # *.py/** → 名称 glob 任意深度
        else:
            return p.startswith(base + "/") or p == base
    if fnmatch.fnmatch(p, pat):
        return True
    if pat.endswith("/**"):
        return p.startswith(pat[: -len("/**")].rstrip("/") + "/")
    if "/" not in pat and "*" not in pat:
        # 裸目录/文件名：精确或前缀（目录下所有内容）
        return p == pat or p.startswith(pat + "/")
    if "/" not in pat:
        # 无斜杠 glob：命中任意深度的同名/同模式 basename（gitignore 语义）
        return fnmatch.fnmatch(p.rsplit("/", 1)[-1], pat)
    return bool(fnmatch.fnmatch(p, pat + "/*"))


def _glob_match(rel_path: str, pattern: str) -> bool:
    return _ignore_match(rel_path, pattern)


def scope_of_file(rel_path: str, config: dict[str, Any]) -> tuple[str, str | None]:
    """文件分类：返回 (scope_type, scope_name)。ignored 文件 scope_name=None。

    优先级：ignore > third_party > project（Build Scope 位于更上层，见 build_scope）。

    third_party 兼容两种书写：裸目录（Middlewares/LVGL）与 glob 写法
    （Middlewares/LVGL/** 归一化后同样命中）。归一化在 parse 时完成，
    此处对历史/外部配置再兜底一次。
    """
    p = rel_path.replace("\\", "/").strip("/")
    for pat in config.get("ignore", []):
        if _glob_match(p, pat):
            return SCOPE_IGNORED, None
    for tp in config.get("third_party", []):
        base = _norm_base(str(tp.get("path", "")))
        if not base:
            continue
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
