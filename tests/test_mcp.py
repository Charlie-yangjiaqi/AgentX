"""MCP Server 测试：统一入口 agentx + action 分发。"""

from __future__ import annotations

from pathlib import Path

import pytest

import agentx.mcp.server as mcp_server
from agentx.app.application import Application
from agentx.index.index import IndexStatus, index_status
from agentx.providers.messages import ModelResponse
from agentx.providers.mock import MockProvider, text_response
from tests.helpers import EXPLORE_RESPONSE


def _make_c_project(tmp_path: Path) -> None:
    (tmp_path / "main.c").write_text("int main(void) { return 0; }\n", encoding="utf-8")
    (tmp_path / "param.c").write_text("// TODO\n", encoding="utf-8")
    (tmp_path / "param.h").write_text("#ifndef P\n#define P\n#endif\n", encoding="utf-8")


def _app_with_mock(tmp_path: Path, providers: dict[str, MockProvider]) -> Application:
    app = Application(tmp_path)
    for role, provider in providers.items():
        app.orchestrator.agents[role].provider = provider
    return app


def _plan_json(verification: str = "echo ok") -> str:
    return (
        '{"summary": "实现参数事务", "steps": [{"action": "实现", "files": ["param.c"]}], '
        '"files_involved": ["param.c"], "risks": [], '
        f'"verification": "{verification}"}}'
    )


@pytest.mark.asyncio
async def test_agentx_unknown_action(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _make_c_project(tmp_path)
    app = _app_with_mock(tmp_path, {})
    monkeypatch.setattr(mcp_server, "_app", lambda path: app)
    result = await mcp_server.agentx(str(tmp_path), "任务", action="debug")
    assert "未知 action" in result["error"]


@pytest.mark.asyncio
async def test_agentx_plan_action(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, gate_bypass: None
) -> None:
    _make_c_project(tmp_path)
    app = _app_with_mock(
        tmp_path,
        {
            "plan": MockProvider().respond(
                text_response(EXPLORE_RESPONSE), text_response(_plan_json())
            ),
        },
    )
    monkeypatch.setattr(mcp_server, "_app", lambda path: app)

    result = await mcp_server.agentx(str(tmp_path), "实现参数事务", action="plan")
    # Phase 6.5：返回 {result, runtime, events}，result 保留旧结构
    assert result["result"]["index_before"]["status"] == "MISSING"
    assert result["result"]["index_after"]["status"] == "VALID"
    assert result["result"]["index_status"] == "VALID"
    assert result["result"]["plan"]["summary"] == "实现参数事务"
    # 索引进入 <项目名>_codebase_index/
    dir_name = f"{tmp_path.name}_codebase_index"
    assert result["result"]["index_dir"].endswith(dir_name)
    assert (tmp_path / dir_name / "index.json").exists()
    status, _ = index_status(tmp_path)
    assert status == IndexStatus.VALID

    # runtime：Index 决策解释（进入时 MISSING → rebuild_index）
    runtime = result["runtime"]
    assert runtime["index_state"] == "VALID"
    assert runtime["decision"]["action"] == "rebuild_index"
    assert "missing" in runtime["decision"]["reason"]
    assert runtime["workflow"]["action"] == "plan"
    # events：结构化 workflow 阶段
    stages = [e["stage"] for e in result["events"]]
    assert "index_check" in stages
    assert "index_decision" in stages
    assert "codegraph_analysis" in stages
    assert "query_context" in stages
    assert "planning" in stages
    assert "completed" in stages


@pytest.mark.asyncio
async def test_agentx_review_action(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _make_c_project(tmp_path)
    app = _app_with_mock(
        tmp_path,
        {
            "plan": MockProvider().respond(
                text_response(EXPLORE_RESPONSE), text_response(_plan_json())
            ),
            "reviewer": MockProvider().respond(
                text_response(
                    '{"verdict": "FAIL", "findings": [{"severity": "HIGH", "category": "回归", '
                    '"location": "param.c", "description": "缺少回滚"}]}'
                )
            ),
        },
    )
    monkeypatch.setattr(mcp_server, "_app", lambda path: app)

    await mcp_server.agentx(str(tmp_path), "任务", action="plan")
    result = await mcp_server.agentx(str(tmp_path), "任务", action="review")
    assert result["result"]["verdict"] == "FAIL"
    assert result["result"]["findings"][0]["severity"] == "HIGH"
    # 决策解释：VALID → reuse_index
    assert result["runtime"]["decision"]["action"] == "reuse_index"


@pytest.mark.asyncio
async def test_agentx_review_requires_plan(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _make_c_project(tmp_path)
    app = _app_with_mock(tmp_path, {})
    monkeypatch.setattr(mcp_server, "_app", lambda path: app)
    result = await mcp_server.agentx(str(tmp_path), "任务", action="review")
    assert "先调用 agentx.plan" in result["error"]


@pytest.mark.asyncio
async def test_agentx_verify_action(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _make_c_project(tmp_path)
    app = _app_with_mock(
        tmp_path,
        {
            "plan": MockProvider().respond(
                text_response(EXPLORE_RESPONSE), text_response(_plan_json())
            ),
        },
    )
    monkeypatch.setattr(mcp_server, "_app", lambda path: app)

    await mcp_server.agentx(str(tmp_path), "任务", action="plan")
    result = await mcp_server.agentx(str(tmp_path), "任务", action="verify")
    assert result["result"]["verdict"] == "PASS"
    assert result["result"]["evidence"][0]["exit_code"] == 0
    assert result["runtime"]["decision"]["action"] == "reuse_index"
    stages = [e["stage"] for e in result["events"]]
    assert "verify" in stages and "completed" in stages


@pytest.mark.asyncio
async def test_agentx_status_action(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _make_c_project(tmp_path)
    app = _app_with_mock(
        tmp_path,
        {
            "plan": MockProvider().respond(
                text_response(EXPLORE_RESPONSE), text_response(_plan_json())
            ),
        },
    )
    monkeypatch.setattr(mcp_server, "_app", lambda path: app)

    result = await mcp_server.agentx(str(tmp_path), "任务", action="status")
    assert result["result"]["index_status"] == IndexStatus.MISSING

    await mcp_server.agentx(str(tmp_path), "任务", action="plan")
    result2 = await mcp_server.agentx(str(tmp_path), "任务", action="status")
    assert result2["result"]["index_status"] == IndexStatus.VALID
    assert result2["result"]["plan"]["summary"] == "实现参数事务"
    assert result2["runtime"]["index_state"] == "VALID"


@pytest.mark.asyncio
async def test_agentx_auto_action(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, gate_bypass: None
) -> None:
    """auto：Plan → Review → Verify 一次完成。"""
    _make_c_project(tmp_path)
    app = _app_with_mock(
        tmp_path,
        {
            "plan": MockProvider().respond(
                text_response(EXPLORE_RESPONSE), text_response(_plan_json())
            ),
            "reviewer": MockProvider().respond(
                text_response('{"verdict": "PASS", "findings": []}')
            ),
        },
    )
    monkeypatch.setattr(mcp_server, "_app", lambda path: app)

    result = await mcp_server.agentx(str(tmp_path), "实现参数事务", action="auto")
    assert result["result"]["phase"] == "complete"
    assert result["result"]["plan"]["index_status"] == "VALID"
    assert result["result"]["plan"]["index_before"]["status"] == "MISSING"
    assert result["result"]["review"]["verdict"] == "PASS"
    assert result["result"]["verify"]["verdict"] == "PASS"
    assert result["runtime"]["workflow"]["action"] == "auto"
    assert "planning" in [e["stage"] for e in result["events"]]


@pytest.mark.asyncio
async def test_agentx_never_crashes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """异常被转换为结构化错误，而不是抛给 MCP 客户端。"""
    _make_c_project(tmp_path)

    def broken(messages: list) -> ModelResponse:
        raise RuntimeError("模型 API 挂了")

    app = _app_with_mock(tmp_path, {"plan": MockProvider().with_handler(broken)})
    monkeypatch.setattr(mcp_server, "_app", lambda path: app)

    result = await mcp_server.agentx(str(tmp_path), "任务", action="plan")
    assert "error" in result
    assert "plan 失败" in result["error"]
