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
BUILD_TARGET_STATUS = "build_target_required"

# 会建立/重建 Index 的 MCP action（query/build_status 纯读，不 gate）
GATED_ACTIONS = frozenset({"plan", "auto", "sync", "understand", "status"})


def check_build_target_init(project_root: Path) -> dict[str, Any] | None:
    """Build Target 门禁（Phase 7.10）：多 Target 且未确认 → build_target_required。

    规则：
    - 无 Keil 工程 / 单 Target → None（自动，不打扰）
    - 已配置 build.target（scope config）→ None
    - 多 Target 且未配置 → Gate：
        {status: build_target_required, reason, message,
         build_targets: ["LVGL","Debug","Demo"], detail:{...}}
    不自动猜 Target（用户必须确认分析哪个固件）。
    """
    from agentx.scope.build_scope import resolve_keil_build
    from agentx.scope.config import load_scope_config

    root = project_root.resolve()
    view = resolve_keil_build(root)
    if view.project_file is None or not view.ambiguity:
        return None  # 无 Keil 工程或单 Target：无需确认
    cfg = load_scope_config(root)
    if cfg.get("build_target"):
        return None  # 已配置 target
    return {
        "status": BUILD_TARGET_STATUS,
        "reason": "keil_multi_target_unselected",
        "message": "Keil 工程含多个 Target，需确认当前分析目标固件",
        "build_targets": view.targets,
        "build_files": len(view.build_files),
        "project_file": str(view.project_file),
    }


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


def _selections_payload(selections: dict[str, Any] | None) -> dict[str, Any]:
    """把 scope_selections 转成 build_scope_yaml 的 chosen 结构（含归一化）。

    selections 传 None 视为空（显式确认：写空配置锁定初始化）。
    """
    chosen_third: list[dict[str, str]] = []
    chosen_ignore: list[dict[str, str]] = []
    if selections:
        from agentx.scope.config import normalize_scope_path

        for raw in selections.get("ignore") or []:
            p = normalize_scope_path(str(raw))
            if p:
                chosen_ignore.append({"path": p, "reason": "手动添加"})
        for raw in selections.get("third_party") or []:
            if isinstance(raw, dict):
                path = normalize_scope_path(str(raw.get("path", "")))
                name = str(raw.get("name") or "").strip() or path.rsplit("/", 1)[-1]
            else:
                path = normalize_scope_path(str(raw))
                name = path.rsplit("/", 1)[-1]
            if path:
                chosen_third.append({"path": path, "name": name, "reason": "手动添加"})
    payload: dict[str, Any] = {"third_party": chosen_third, "ignore": chosen_ignore}
    if selections and selections.get("build_target"):
        payload["build_target"] = str(selections["build_target"]).strip()
    return payload


def apply_scope_selections(
    project_root: Path, selections: dict[str, Any] | None
) -> Path:
    """确认后应用选择，生成 .agentxscope.yaml（返回文件路径）。

    selections = {"ignore": ["docs/**"|"docs", ...],
                  "third_party": ["Middlewares/LVGL" | {"path","name"}, ...],
                  "build_target": "LVGL"}   # 可选：Phase 7.10 Keil Target
    显式确认（含空 selections）→ 写入（锁定"已初始化"，之后不再打扰）。
    MCP 无 stdin，确认语义 = 显式携带 scope_selections 再次调用。
    """
    return write_scope_config(project_root, build_scope_yaml(_selections_payload(selections)))


def preview_scope_change(project_root: Path, selections: dict[str, Any]) -> dict[str, Any]:
    """Scope 修改影响预览（Phase 8.1，不落盘）：新旧 scope 对源文件分类差异统计。

    返回 {before:{project,third_party,non_build,ignored}, after:{...},
          moves:[{file, from, to}]（前 50 条）, moved_count}
    用磁盘相关源文件清单做差异，不依赖 Index（Index 可能还没反映新 scope）。
    """
    from agentx.index.fingerprint import relevant_files
    from agentx.scope.config import parse_scope_config, scope_of_file
    from agentx.scope.resolver import ScopeResolver
    from agentx.scope.wizard import build_scope_yaml

    root = project_root.resolve()
    old_cfg = ScopeResolver(root).config
    payload_text = build_scope_yaml(_selections_payload(selections))
    new_cfg = parse_scope_config(payload_text)

    def _counts(cfg: dict[str, Any], files: list[str]) -> dict[str, int]:
        c: dict[str, int] = {"project": 0, "third_party": 0, "non_build": 0, "ignored": 0}
        for f in files:
            st, _ = scope_of_file(f, cfg)
            c[st] = c.get(st, 0) + 1
        return c

    files = relevant_files(root)
    before = _counts(old_cfg, files)
    after = _counts(new_cfg, files)
    moves: list[dict[str, Any]] = []
    for f in files:
        b, _ = scope_of_file(f, old_cfg)
        a, _ = scope_of_file(f, new_cfg)
        if b != a:
            moves.append({"file": f, "from": b, "to": a})
    return {
        "before": before,
        "after": after,
        "moved_count": len(moves),
        "moves": moves[:50],
    }
