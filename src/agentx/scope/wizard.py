"""Scope Setup Wizard：首次建立 Index 前的 Scope 引导（Phase 7.8 引导层）。

- scope_config_exists：是否已有 scope 配置（.agentxscope.yaml 或 legacy .agentxignore）
- wizard_result：由建议 + 逐项决策 + 手动路径计算最终选择（纯逻辑，CLI 注入决策）
- build_scope_yaml：生成 .agentxscope.yaml 内容（与 scope/config.py 解析兼容，
  不改变 matcher 语义）
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from agentx.scope.config import LEGACY_IGNORE_FILENAME, SCOPE_CONFIG_FILENAME

DecisionFn = Callable[[str, str], bool]


def scope_config_exists(project_root: Path) -> bool:
    """已有 .agentxscope.yaml 或 legacy .agentxignore → 跳过向导。"""
    root = project_root.resolve()
    return (root / SCOPE_CONFIG_FILENAME).is_file() or (root / LEGACY_IGNORE_FILENAME).is_file()


def wizard_result(
    suggestions: dict[str, list[dict[str, str]]],
    decide: DecisionFn,
    extra_ignore: list[str] | None = None,
    extra_third_party: list[str] | None = None,
    extra_provider: Callable[[str], list[str]] | None = None,
) -> dict[str, Any] | None:
    """计算向导最终选择。

    - decide(kind, path)：逐项 Y/n 决策（kind="third_party"/"ignore"，True=采纳）
    - extra_ignore / extra_third_party：手动增加路径（规范化，去重）
    - extra_provider(kind)：惰性回调（"ignore"/"third_party"），在逐项决策完成后
      询问手动路径（CLI 交互顺序：先逐项确认、后手动增加）
    - 全部取消且无额外路径 → 返回 None（不写文件）
    """
    chosen_third: list[dict[str, str]] = [
        s
        for s in suggestions.get("third_party", [])
        if decide("third_party", str(s.get("path", "")))
    ]
    chosen_ignore: list[dict[str, str]] = [
        s for s in suggestions.get("ignore", []) if decide("ignore", str(s.get("path", "")))
    ]
    extra_ig = list(extra_ignore) if extra_ignore else []
    extra_tp = list(extra_third_party) if extra_third_party else []
    if extra_provider is not None:
        extra_ig += list(extra_provider("ignore"))
        extra_tp += list(extra_provider("third_party"))
    for path in _norm_extra(extra_ig):
        chosen_ignore.append({"path": path, "reason": "手动添加"})
    for path in _norm_extra(extra_tp):
        chosen_third.append({"path": path, "name": path.rsplit("/", 1)[-1], "reason": "手动添加"})
    if not chosen_third and not chosen_ignore:
        return None
    return {"third_party": chosen_third, "ignore": chosen_ignore}


def _norm_extra(paths: list[str] | None) -> list[str]:
    out: list[str] = []
    for raw in paths or []:
        p = str(raw).strip().replace("\\", "/").strip("/")
        if p and p not in out:
            out.append(p)
    return out


def build_scope_yaml(chosen: dict[str, Any]) -> str:
    """生成 .agentxscope.yaml 内容（与 parse_scope_config 兼容）。"""
    lines = ["# AgentX Scope（Phase 7.8 三层：project / third_party / ignore）"]
    third_party = chosen.get("third_party") or []
    ignore = chosen.get("ignore") or []
    if third_party:
        lines.append("third_party:")
        for s in third_party:
            lines.append(f"  - path: {s['path']}")
            lines.append(f"    name: {s.get('name') or s['path'].rsplit('/', 1)[-1]}")
    if ignore:
        lines.append("ignore:")
        for s in ignore:
            lines.append(f"  - {s['path']}/**")
    return "\n".join(lines) + "\n"


def write_scope_config(project_root: Path, text: str) -> Path:
    """写入 .agentxscope.yaml。"""
    root = project_root.resolve()
    target = root / SCOPE_CONFIG_FILENAME
    target.write_text(text, encoding="utf-8")
    return target
