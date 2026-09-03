"""Phase 8.1 权限模型：INDEX_WRITE 与 CODE_WRITE 分离 + scope_fingerprint 强制重建。

验收：
1. 宿主修改 ignore → scope_update 允许（INDEX_WRITE，不需代码审批）
2. 宿主修改 third_party → 允许
3. 修改 scope 后立即 reindex → 使用新配置（scope_fingerprint 变化强制 enrich）
4. reindex 不触发 CODE_WRITE approval（operation_class=INDEX_WRITE, changes_code=False）
5. refresh_understanding 不触发 CODE_WRITE approval
6. 用户源码修改 → plan/auto 仍触发现有 Decision Boundary（changes_code=True）
7. INDEX_WRITE 与 CODE_WRITE 混合 → capabilities 返回分类供宿主拆分
8. scope_update 返回 scope_changed + preview；reindex 返回 scope_summary
"""

from __future__ import annotations

from pathlib import Path

import pytest

import agentx.mcp.server as mcp_server
from agentx.app.application import Application
from agentx.providers.mock import MockProvider, text_response
from agentx.scope.config import SCOPE_CONFIG_FILENAME

# 350A 验收在 test_build_scope.py 之外的测试不依赖真实 CodeGraph；
# 这里用小工程 + monkeypatch analyze_project（test_scope_initializer 模式）。


def _make_project(tmp_path: Path) -> Path:
    root = tmp_path / "工程"
    (root / "User").mkdir(parents=True)
    (root / "User" / "main.c").write_text("int main(void) { return 0; }\n", encoding="utf-8")
    (root / "User" / "tool.py").write_text("print(1)\n", encoding="utf-8")
    (root / "User" / "app.c").write_text("int app_fn(void) { return 1; }\n", encoding="utf-8")
    return root


def _app_for(root: Path) -> Application:
    app = Application(root)
    app.orchestrator.agents["plan"].provider = MockProvider().respond(
        text_response(
            '{"architecture_summary": "HMI", "startup_flow": ["main"], '
            '"core_modules": ["User/main.c"], "critical_files": []}'
        ),
        text_response(
            '{"summary": "ok", "files_involved": ["User/main.c"], "verification": "echo ok"}'
        ),
    )
    return app


@pytest.fixture(autouse=True)
def _isolate_config(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        "agentx.config.config.default_config_path",
        lambda: tmp_path / "agentx_test_config.json",
    )


# ---------- 1/2. scope_update 允许 ignore/third_party ----------


@pytest.mark.asyncio
async def test_scope_update_ignore_allowed(tmp_path, monkeypatch) -> None:
    root = _make_project(tmp_path)
    app = _app_for(root)
    monkeypatch.setattr(mcp_server, "_app", lambda path: app)
    out = await mcp_server.agentx(
        str(root), "忽略 python", action="scope_update",
        scope_selections={"ignore": ["*.py"], "third_party": []},
    )
    assert out["operation_class"] == "INDEX_WRITE"
    assert out["changes_code"] is False
    res = out["result"]
    assert res["status"] == "updated"
    assert res["scope_changed"] is True
    text = (root / SCOPE_CONFIG_FILENAME).read_text(encoding="utf-8")
    assert "*.py" in text  # ignore 已写入（normalize 后为 *.py）

    # 影响预览
    assert res["preview"]["after"]["ignored"] > 0


@pytest.mark.asyncio
async def test_scope_update_third_party_allowed(tmp_path, monkeypatch) -> None:
    root = _make_project(tmp_path)
    (root / "Middlewares" / "LVGL").mkdir(parents=True)
    (root / "Middlewares" / "LVGL" / "lv_core.c").write_text(
        "int lv_init(void){return 0;}\n", encoding="utf-8"
    )
    app = _app_for(root)
    monkeypatch.setattr(mcp_server, "_app", lambda path: app)
    out = await mcp_server.agentx(
        str(root), "标记 LVGL", action="scope_update",
        scope_selections={"third_party": ["Middlewares/LVGL"], "ignore": []},
    )
    assert out["result"]["status"] == "updated"
    text = (root / SCOPE_CONFIG_FILENAME).read_text(encoding="utf-8")
    assert "Middlewares/LVGL" in text


# ---------- 3. scope 修改后 reindex 用新配置 ----------


@pytest.mark.asyncio
async def test_scope_update_then_reindex_uses_new_scope(tmp_path, monkeypatch) -> None:
    from agentx.understanding.graph import ProjectGraph

    root = _make_project(tmp_path)

    def _graph(r: Path) -> ProjectGraph:
        return ProjectGraph(
            source="codegraph",
            files=[{"path": "User/main.c", "language": "c"},
                   {"path": "User/tool.py", "language": "python"},
                   {"path": "User/app.c", "language": "c"}],
            symbols=[{"name": "app_fn", "type": "function", "file": "User/app.c", "start_line": 1}],
            call_graph=[], include_map={}, build_info={}, errors=[],
        )

    monkeypatch.setattr("agentx.plan.service.analyze_project", _graph)
    monkeypatch.setattr("agentx.understanding.graph.analyze_project", _graph)
    app = _app_for(root)
    monkeypatch.setattr(mcp_server, "_app", lambda path: app)

    # 首次建 Index（tool.py 在）
    r1 = await mcp_server.agentx(
        str(root), "init", action="reindex",
        scope_selections={"ignore": [], "third_party": []},
    )
    assert r1["result"]["status"] == "completed"
    from agentx.index.index import load_index

    assert any(f.path == "User/tool.py" for f in load_index(root).files)

    # 修改 scope：忽略 *.py
    su = await mcp_server.agentx(
        str(root), "忽略 python", action="scope_update",
        scope_selections={"ignore": ["*.py"], "third_party": []},
    )
    assert su["result"]["scope_changed"] is True

    # reindex：必须使用新 scope，tool.py 不再进主 Index
    r2 = await mcp_server.agentx(str(root), "重建", action="reindex")
    assert r2["result"]["status"] == "completed"
    assert r2["result"]["index_status"] == "VALID"
    idx2 = load_index(root)
    assert not any(f.path == "User/tool.py" for f in idx2.files)
    assert r2["result"]["scope_summary"]["ignored"] >= 0


# ---------- 4. reindex 不触发 CODE_WRITE approval ----------


@pytest.mark.asyncio
async def test_reindex_no_code_approval(tmp_path, monkeypatch) -> None:
    root = _make_project(tmp_path)
    app = _app_for(root)
    monkeypatch.setattr(mcp_server, "_app", lambda path: app)
    from agentx.understanding.graph import ProjectGraph

    monkeypatch.setattr(
        "agentx.plan.service.analyze_project",
        lambda r: ProjectGraph(source="filescan", files=[{"path": "User/main.c", "language": "c"}],
                               include_map={}, errors=[]),
    )
    monkeypatch.setattr(
        "agentx.understanding.graph.analyze_project",
        lambda r: ProjectGraph(source="filescan", files=[{"path": "User/main.c", "language": "c"}],
                               include_map={}, errors=[]),
    )
    out = await mcp_server.agentx(str(root), "重建", action="reindex",
                                  scope_selections={"ignore": [], "third_party": []})
    assert out["operation_class"] == "INDEX_WRITE"
    assert out["changes_code"] is False
    assert out["requires_decision_gate"] is False


# ---------- 5. understand 不触发 CODE_WRITE approval ----------


@pytest.mark.asyncio
async def test_understand_no_code_approval(tmp_path, monkeypatch) -> None:
    root = _make_project(tmp_path)
    app = _app_for(root)
    monkeypatch.setattr(mcp_server, "_app", lambda path: app)
    from agentx.understanding.graph import ProjectGraph

    monkeypatch.setattr(
        "agentx.plan.service.analyze_project",
        lambda r: ProjectGraph(source="filescan", files=[{"path": "User/main.c", "language": "c"}],
                               include_map={}, errors=[]),
    )
    monkeypatch.setattr(
        "agentx.understanding.graph.analyze_project",
        lambda r: ProjectGraph(source="filescan", files=[{"path": "User/main.c", "language": "c"}],
                               include_map={}, errors=[]),
    )
    out = await mcp_server.agentx(str(root), "理解", action="understand",
                                  scope_selections={"ignore": [], "third_party": []})
    assert out["operation_class"] == "INDEX_WRITE"
    assert out["changes_code"] is False


# ---------- 6. plan 仍走 Decision Gate（changes_code=True） ----------


@pytest.mark.asyncio
async def test_plan_still_code_write_preview(tmp_path, monkeypatch) -> None:
    root = _make_project(tmp_path)
    app = _app_for(root)
    monkeypatch.setattr(mcp_server, "_app", lambda path: app)
    from agentx.understanding.graph import ProjectGraph

    monkeypatch.setattr(
        "agentx.plan.service.analyze_project",
        lambda r: ProjectGraph(source="filescan", files=[{"path": "User/main.c", "language": "c"}],
                               include_map={}, errors=[]),
    )
    monkeypatch.setattr(
        "agentx.understanding.graph.analyze_project",
        lambda r: ProjectGraph(source="filescan", files=[{"path": "User/main.c", "language": "c"}],
                               include_map={}, errors=[]),
    )
    out = await mcp_server.agentx(str(root), "改代码", action="plan",
                                  scope_selections={"ignore": [], "third_party": []})
    assert out["operation_class"] == "CODE_WRITE_PREVIEW"
    assert out["changes_code"] is True
    assert out["requires_decision_gate"] is True


# ---------- 7. capabilities 返回分类表（供宿主拆分混合请求） ----------


@pytest.mark.asyncio
async def test_capabilities_lists_operation_classes(tmp_path) -> None:
    from agentx.mcp.server import agentx_capabilities

    cap = await agentx_capabilities()
    ops = cap["operations"]
    assert ops["query"]["class"] == "READ"
    assert ops["scope_update"]["class"] == "INDEX_WRITE"
    assert ops["reindex"]["class"] == "INDEX_WRITE"
    assert ops["sync"]["class"] == "INDEX_WRITE"
    assert ops["understand"]["class"] == "INDEX_WRITE"
    assert ops["plan"]["class"] == "CODE_WRITE_PREVIEW"
    assert ops["auto"]["class"] == "CODE_WRITE"
    # INDEX_WRITE 全部 changes_code=False（宿主可自动执行）
    for a in ("scope_update", "reindex", "sync", "understand"):
        assert ops[a]["changes_code"] is False
