"""Scope Initializer Service 测试（CLI 与 MCP 共用门禁）。

- MCP 首次进入无 scope 项目（有建议）→ scope_required，不建 Index
- MCP 带 scope_selections 确认 → 生成 .agentxscope.yaml → Index 正常生成
- 已存在 scope → MCP 直接执行原流程
- gate 不绑定 Index 状态（有 Index 但 scope 丢失仍拦截）
- CLI 行为不变（由 test_scope_wizard.py 全量覆盖）
"""

from __future__ import annotations

from pathlib import Path

import pytest

import agentx.mcp.server as mcp_server
from agentx.app.application import Application
from agentx.index.index import IndexStatus, index_status
from agentx.providers.mock import MockProvider, text_response
from agentx.scope.config import SCOPE_CONFIG_FILENAME, parse_scope_config, scope_of_file
from agentx.scope.initializer import (
    GATE_REASON,
    GATE_STATUS,
    apply_scope_selections,
    check_scope_init,
)
from tests.helpers import EXPLORE_RESPONSE


def _make_project(tmp_path: Path, with_suggestions: bool = True) -> None:
    (tmp_path / "main.c").write_text("int main(void) { return 0; }\n", encoding="utf-8")
    if with_suggestions:
        (tmp_path / "build").mkdir()
        (tmp_path / "build" / "out.c").write_text("int b;\n", encoding="utf-8")
        (tmp_path / "docs").mkdir()
        (tmp_path / "docs" / "readme.md").write_text("doc", encoding="utf-8")
        (tmp_path / "Middlewares").mkdir()
        (tmp_path / "Middlewares" / "LVGL").mkdir()
        (tmp_path / "Middlewares" / "LVGL" / "lv_conf.h").write_text(
            "#ifndef LV\n#define LV\n#endif\n", encoding="utf-8"
        )


def _app_with_mock(tmp_path: Path, providers: dict[str, MockProvider]) -> Application:
    app = Application(tmp_path)
    for role, provider in providers.items():
        app.orchestrator.agents[role].provider = provider
    return app


def _plan_json() -> str:
    return (
        '{"summary": "实现参数事务", "steps": [{"action": "实现", "files": ["main.c"]}], '
        '"files_involved": ["main.c"], "risks": [], "verification": "echo ok"}'
    )


# ---------- 逻辑层 ----------


def test_ensure_index_gate_direct(tmp_path: Path) -> None:
    """绕过测试：直接调用 ensure_index()，无 scope → 触发 scope_required，Index 为 None。"""
    from agentx.plan.service import ensure_index

    _make_project(tmp_path)
    status, reason, index = ensure_index(tmp_path)
    assert reason == "scope_required"
    assert index is None
    assert index_status(tmp_path)[0] == IndexStatus.MISSING


def test_ensure_index_gate_direct_with_selections(tmp_path: Path) -> None:
    """直接调用 ensure_index() 带 scope_selections → 生成配置并建立 Index。"""
    from agentx.plan.service import ensure_index

    _make_project(tmp_path)
    status, reason, index = ensure_index(
        tmp_path, scope_selections={"ignore": ["build/**"], "third_party": ["Middlewares/LVGL"]}
    )
    assert index is not None
    assert (tmp_path / SCOPE_CONFIG_FILENAME).exists()
    assert index_status(tmp_path)[0] == IndexStatus.VALID


def test_ensure_index_with_existing_scope(tmp_path: Path) -> None:
    """已有 scope → 直接建立 Index，不触发 gate。"""
    from agentx.plan.service import ensure_index

    _make_project(tmp_path)
    apply_scope_selections(tmp_path, {"ignore": ["build/**"], "third_party": []})
    status, reason, index = ensure_index(tmp_path)
    assert index is not None
    assert index_status(tmp_path)[0] == IndexStatus.VALID


def test_gate_when_no_scope_and_suggestions(tmp_path: Path) -> None:
    _make_project(tmp_path)
    gate = check_scope_init(tmp_path)
    assert gate is not None
    assert gate["status"] == GATE_STATUS
    assert gate["reason"] == GATE_REASON
    assert gate["message"]
    assert "build/**" in gate["suggestions"]["ignore"]
    assert "docs/**" in gate["suggestions"]["ignore"]
    assert "Middlewares/LVGL" in gate["suggestions"]["third_party"]
    detail_ig = {s["path"] for s in gate["detail"]["ignore"]}
    assert "build" in detail_ig and "docs" in detail_ig


def test_gate_skipped_when_scope_exists(tmp_path: Path) -> None:
    _make_project(tmp_path)
    apply_scope_selections(tmp_path, {"ignore": ["build/**"], "third_party": ["Middlewares/LVGL"]})
    assert check_scope_init(tmp_path) is None


def test_gate_skipped_when_no_suggestions(tmp_path: Path) -> None:
    _make_project(tmp_path, with_suggestions=False)
    assert check_scope_init(tmp_path) is None


def test_gate_not_bound_to_index_state(tmp_path: Path) -> None:
    """有 Index 但 scope 丢失 → 仍拦截（不做"已有 Index 就有配置"假设）。"""
    from agentx.index.index import create_index, save_index

    _make_project(tmp_path)
    save_index(tmp_path, create_index(tmp_path))
    assert index_status(tmp_path)[0] == IndexStatus.VALID
    gate = check_scope_init(tmp_path)
    assert gate is not None
    assert gate["status"] == GATE_STATUS


def test_apply_writes_config_and_matcher_works(tmp_path: Path) -> None:
    _make_project(tmp_path)
    target = apply_scope_selections(
        tmp_path,
        {
            "ignore": ["build/**", "docs"],
            "third_party": ["Middlewares/LVGL", {"path": "SDK/vendor", "name": "vendor"}],
        },
    )
    assert target == tmp_path / SCOPE_CONFIG_FILENAME
    cfg = parse_scope_config(target.read_text(encoding="utf-8"))
    assert scope_of_file("build/out.c", cfg)[0] == "ignored"
    assert scope_of_file("docs/readme.md", cfg)[0] == "ignored"
    assert scope_of_file("Middlewares/LVGL/lv_conf.h", cfg) == ("third_party", "LVGL")
    assert scope_of_file("SDK/vendor/x.h", cfg) == ("third_party", "vendor")
    assert scope_of_file("main.c", cfg)[0] == "project"


def test_apply_empty_selections_writes_lock(tmp_path: Path) -> None:
    """显式确认（空 selections）→ 写入空配置（锁定已初始化，不再打扰）。"""
    _make_project(tmp_path)
    target = apply_scope_selections(tmp_path, {"ignore": [], "third_party": []})
    assert target.is_file()
    assert check_scope_init(tmp_path) is None


# ---------- MCP 集成 ----------


@pytest.mark.asyncio
async def test_mcp_scope_required_no_index_build(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """MCP 首次进入无 scope 项目 → scope_required（service 层 gate），不建 Index。"""
    _make_project(tmp_path)
    app = _app_with_mock(tmp_path, {})
    monkeypatch.setattr(mcp_server, "_app", lambda path: app)

    from agentx.plan.service import create_index

    calls: list = []
    monkeypatch.setattr(
        "agentx.plan.service.create_index", lambda *a, **k: calls.append(a) or create_index(*a, **k)
    )

    result = await mcp_server.agentx(str(tmp_path), "任务", action="plan")
    assert result["result"]["status"] == GATE_STATUS
    assert result["result"]["reason"] == GATE_REASON
    assert "build/**" in result["result"]["suggestions"]["ignore"]
    assert "Middlewares/LVGL" in result["result"]["suggestions"]["third_party"]
    assert index_status(tmp_path)[0] == IndexStatus.MISSING
    assert not (tmp_path / SCOPE_CONFIG_FILENAME).exists()
    assert calls == []  # create_index 未被调用


@pytest.mark.asyncio
async def test_mcp_auto_scope_required_no_index_build(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """MCP auto 无 scope → scope_required，不进入 review/verify，Index 未建立。"""
    _make_project(tmp_path)
    app = _app_with_mock(tmp_path, {})
    monkeypatch.setattr(mcp_server, "_app", lambda path: app)

    result = await mcp_server.agentx(str(tmp_path), "任务", action="auto")
    assert result["result"]["phase"] == "scope_required"
    assert result["result"]["plan"]["status"] == GATE_STATUS
    assert index_status(tmp_path)[0] == IndexStatus.MISSING


@pytest.mark.asyncio
async def test_mcp_apply_scope_then_index_builds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """MCP 带 scope_selections 确认 → 生成配置 → Index 正常生成且 scope 生效。"""
    _make_project(tmp_path)
    app = _app_with_mock(
        tmp_path,
        {
            "plan": MockProvider().respond(
                text_response(EXPLORE_RESPONSE), text_response(_plan_json())
            )
        },
    )
    monkeypatch.setattr(mcp_server, "_app", lambda path: app)

    result = await mcp_server.agentx(
        str(tmp_path),
        "任务",
        action="plan",
        scope_selections={"ignore": ["build/**"], "third_party": ["Middlewares/LVGL"]},
    )
    assert (tmp_path / SCOPE_CONFIG_FILENAME).exists()
    assert result["result"]["index_status"] == "VALID"
    index_files = {f.path for f in _load_index(tmp_path).files}
    assert not any(f.startswith("build/") for f in index_files)
    assert any(f.startswith("Middlewares/LVGL/") for f in index_files)
    assert not any(f.startswith("docs/") for f in index_files)  # docs 未选 → 全 project 分析


def _load_index(project_root: Path):
    from agentx.index.index import load_index

    return load_index(project_root)


@pytest.mark.asyncio
async def test_mcp_scope_exists_runs_original(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """已存在 scope → MCP 直接执行原流程（无 scope_required）。"""
    _make_project(tmp_path)
    apply_scope_selections(tmp_path, {"ignore": ["build/**"], "third_party": []})
    app = _app_with_mock(
        tmp_path,
        {
            "plan": MockProvider().respond(
                text_response(EXPLORE_RESPONSE), text_response(_plan_json())
            )
        },
    )
    monkeypatch.setattr(mcp_server, "_app", lambda path: app)

    result = await mcp_server.agentx(str(tmp_path), "任务", action="plan")
    assert result["result"]["index_status"] == "VALID"
    assert "status" not in result["result"] or result["result"].get("status") != GATE_STATUS


@pytest.mark.asyncio
async def test_mcp_read_only_actions_not_gated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """query 等纯读 action 不 gate（保持原行为：Index 缺失时报错，不建）。"""
    _make_project(tmp_path)
    app = _app_with_mock(tmp_path, {})
    monkeypatch.setattr(mcp_server, "_app", lambda path: app)

    result = await mcp_server.agentx(str(tmp_path), "任务", action="query")
    assert "Index 不存在" in result["result"]["error"]
    assert result["result"].get("status") != GATE_STATUS


@pytest.mark.asyncio
async def test_mcp_sync_gated_then_continues(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """sync 也在 gate 列表：无配置+建议 → scope_required；确认后继续。"""
    _make_project(tmp_path)
    app = _app_with_mock(tmp_path, {})
    monkeypatch.setattr(mcp_server, "_app", lambda path: app)

    result = await mcp_server.agentx(str(tmp_path), "任务", action="sync")
    assert result["result"]["action"] == "scope_required"
    assert result["result"]["scope_status"] == GATE_STATUS

    result2 = await mcp_server.agentx(
        str(tmp_path), "任务", action="sync", scope_selections={"ignore": ["build/**"]}
    )
    assert (tmp_path / SCOPE_CONFIG_FILENAME).exists()
    assert "level" in result2["result"]
