"""Scope Initializer Service（Phase 7.8 引导层统一入口）。

CLI 与 MCP 共用，解决"首次建立/重建 Index 前无 scope 确认"的系统性问题：
- check_scope_init：Scope 门禁（只绑定 scope 配置，不绑定 Index 状态——
  已有 Index 但 scope 丢失同样拦截）
- apply_scope_selections：确认后生成 .agentxscope.yaml（格式与 matcher 兼容）

CLI：交互 wizard（逐项 Y/n + 手动路径）；MCP：非交互（scope_required 结构化
返回 → Reasonix 确认 → 带 scope_selections 再次调用 → 继续建 Index）。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from agentx.scope.wizard import build_scope_yaml, scope_config_exists, write_scope_config

GATE_STATUS = "scope_required"
GATE_REASON = "first_project_index_without_scope"

# 会建立/重建 Index 的 MCP action（query/build_status 纯读，不 gate）
GATED_ACTIONS = frozenset({"plan", "auto", "sync", "understand", "status"})


def check_scope_init(project_root: Path) -> dict[str, Any] | None:
    """首次初始化门禁。

    规则（不绑定 Index 状态）：
    - 已有 .agentxscope.yaml / .agentxignore → None（跳过，不管 Index 状态）
    - 无配置且无建议 → None（干净项目，不打扰）
    - 无配置且有建议 → ScopeGate：
        {status, reason, message,
         suggestions:{ignore:["docs/**",...], third_party:["Middlewares/LVGL",...]},
         detail:{ignore:[{path,reason}], third_party:[{path,name,reason}]}（CLI 展示用）}
    """
    if scope_config_exists(project_root):
        return None
    from agentx.scope.detector import suggest_scopes

    suggestions = suggest_scopes(project_root)
    third_party = suggestions.get("third_party", [])
    ignore = suggestions.get("ignore", [])
    if not third_party and not ignore:
        return None
    return {
        "status": GATE_STATUS,
        "reason": GATE_REASON,
        "message": "Need user confirmation before index build",
        "suggestions": {
            "ignore": [f"{s['path']}/**" for s in ignore],
            "third_party": [s["path"] for s in third_party],
        },
        "detail": {"ignore": ignore, "third_party": third_party},
    }


def apply_scope_selections(project_root: Path, selections: dict[str, Any] | None) -> Path:
    """确认后应用选择，生成 .agentxscope.yaml（返回文件路径）。

    selections = {"ignore": ["docs/**"|"docs", ...],
                  "third_party": ["Middlewares/LVGL" | {"path","name"}, ...]}
    显式确认（含空 selections）→ 写入（锁定"已初始化"，之后不再打扰）。
    MCP 无 stdin，确认语义 = 显式携带 scope_selections 再次调用。
    """
    chosen_third: list[dict[str, str]] = []
    chosen_ignore: list[dict[str, str]] = []
    if selections:
        for raw in selections.get("ignore") or []:
            p = str(raw).strip().replace("\\", "/").strip("/")
            if p.endswith("/**"):
                p = p[: -len("/**")]
            if p:
                chosen_ignore.append({"path": p, "reason": "手动添加"})
        for raw in selections.get("third_party") or []:
            if isinstance(raw, dict):
                path = str(raw.get("path", "")).strip("/")
                name = str(raw.get("name") or "").strip() or path.rsplit("/", 1)[-1]
            else:
                path = str(raw).strip().replace("\\", "/").strip("/")
                name = path.rsplit("/", 1)[-1]
            if path:
                chosen_third.append({"path": path, "name": name, "reason": "手动添加"})
    return write_scope_config(
        project_root, build_scope_yaml({"third_party": chosen_third, "ignore": chosen_ignore})
    )
