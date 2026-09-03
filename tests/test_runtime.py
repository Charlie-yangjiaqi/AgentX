"""Phase 6.5：Runtime Transparency & Workflow Observability。

决策表（force 优先级最高）、RuntimeContext、EventCollector、
on_event 兼容、force_rebuild 行为、MCP 返回结构。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agentx.runtime.context import (
    build_runtime_context,
    decide_index_action,
)
from agentx.runtime.events import EventCollector, WorkflowEvent

# ---------- 决策表（顺序固定，force 优先级最高） ----------


def test_decision_force_wins_over_valid() -> None:
    """VALID + force → rebuild_index（不允许 VALID 默认重建，但 force 显式优先）。"""
    decision = decide_index_action("VALID", force_rebuild=True)
    assert decision == {
        "action": "rebuild_index",
        "reason": "user requested rebuild",
    }


def test_decision_valid_reuses() -> None:
    assert decide_index_action("VALID") == {
        "action": "reuse_index",
        "reason": "fingerprint matched existing index",
    }


def test_decision_stale_syncs() -> None:
    assert decide_index_action("STALE") == {
        "action": "sync_index",
        "reason": "project changed since index",
    }


def test_decision_missing_corrupted_rebuild() -> None:
    assert decide_index_action("MISSING")["action"] == "rebuild_index"
    assert decide_index_action("CORRUPTED")["action"] == "rebuild_index"


def test_decision_force_still_wins_on_stale() -> None:
    assert decide_index_action("STALE", force_rebuild=True)["action"] == "rebuild_index"


# ---------- RuntimeContext ----------


def test_runtime_context_shape() -> None:
    ctx = build_runtime_context(
        index_state="VALID",
        fingerprint="abc123",
        workflow_action="plan",
        workflow_stage="completed",
    )
    d = ctx.to_dict()
    assert d["index_state"] == "VALID"
    assert d["fingerprint"] == "abc123"
    assert d["decision"]["action"] == "reuse_index"
    assert d["workflow"] == {"action": "plan", "stage": "completed"}


def test_runtime_context_force() -> None:
    ctx = build_runtime_context("VALID", force_rebuild=True)
    assert ctx.to_dict()["decision"]["action"] == "rebuild_index"


# ---------- EventCollector ----------


def test_event_collector_emit_and_validate() -> None:
    col = EventCollector()
    col.emit("index_check", "running", "checking fingerprint")
    col.emit("index_check", "completed", "VALID")
    events = col.events()
    assert len(events) == 2
    assert events[0] == {
        "stage": "index_check",
        "status": "running",
        "message": "checking fingerprint",
    }
    assert events[1]["status"] == "completed"
    assert col.events()[0] == events[0]  # 返回副本


def test_event_collector_rejects_unknown_stage() -> None:
    col = EventCollector()
    with pytest.raises(ValueError, match="未知 stage"):
        col.emit("nope", "running")


def test_event_collector_rejects_bad_status() -> None:
    col = EventCollector()
    with pytest.raises(ValueError, match="未知 status"):
        col.emit("planning", "weird")


def test_event_collector_truthiness() -> None:
    """空 collector 在 bool() 下必须是 True（否则 on_event 参数会被误判为 None）。"""
    col = EventCollector()
    assert bool(col) is True


def test_workflow_event_message_optional() -> None:
    e = WorkflowEvent("planning", "running")
    assert e == {"stage": "planning", "status": "running"}


# ---------- on_event 兼容性（不传时行为不变） ----------


@pytest.mark.asyncio
async def test_plan_on_event_streams_stages(tmp_path: Path) -> None:
    from agentx.app.application import Application
    from agentx.plan.service import PlanService
    from agentx.providers.mock import MockProvider, text_response
    from tests.helpers import EXPLORE_RESPONSE

    (tmp_path / "main.c").write_text("int main(void) { return 0; }\n", encoding="utf-8")
    app = Application(tmp_path)
    app.orchestrator.agents["plan"].provider = MockProvider().respond(
        text_response(EXPLORE_RESPONSE),
        text_response('{"summary": "s", "files_involved": ["main.c"], "verification": "echo ok"}'),
    )
    col = EventCollector()
    await PlanService(app).plan("任务", on_event=col.emit)

    stages = [e["stage"] for e in col.events()]
    assert stages[0] == "index_check"
    assert "index_decision" in stages
    assert "codegraph_analysis" in stages
    assert "query_context" in stages
    assert "understanding" in stages
    assert "planning" in stages
    assert stages[-1] == "completed"
    # 每个 stage 有 running/completed 对（planning 有两条）
    assert [e["status"] for e in col.events() if e["stage"] == "planning"] == [
        "running",
        "completed",
    ]


@pytest.mark.asyncio
async def test_plan_without_on_event_still_works(tmp_path: Path) -> None:
    """不传 on_event：行为与 Phase 6 前完全一致。"""
    from agentx.app.application import Application
    from agentx.plan.service import PlanService
    from agentx.providers.mock import MockProvider, text_response
    from tests.helpers import EXPLORE_RESPONSE

    (tmp_path / "main.c").write_text("int main(void) { return 0; }\n", encoding="utf-8")
    app = Application(tmp_path)
    app.orchestrator.agents["plan"].provider = MockProvider().respond(
        text_response(EXPLORE_RESPONSE),
        text_response('{"summary": "s", "files_involved": ["main.c"], "verification": "echo ok"}'),
    )
    result = await PlanService(app).plan("任务")
    assert result["index_after"]["status"] == "VALID"
    assert result["plan"]["summary"] == "s"


# ---------- force_rebuild ----------


@pytest.mark.asyncio
async def test_plan_force_rebuild_rebuilds_valid(tmp_path: Path) -> None:
    """VALID + force_rebuild → 显式重建（用户意图优先）。"""
    from agentx.app.application import Application
    from agentx.plan.service import PlanService
    from agentx.providers.mock import MockProvider, text_response
    from tests.helpers import EXPLORE_RESPONSE

    (tmp_path / "main.c").write_text("int main(void) { return 0; }\n", encoding="utf-8")
    app = Application(tmp_path)
    app.orchestrator.agents["plan"].provider = MockProvider().respond(
        text_response(EXPLORE_RESPONSE),
        text_response('{"summary": "s", "files_involved": ["main.c"], "verification": "echo ok"}'),
    )
    await PlanService(app).plan("任务")  # 首次：MISSING → 建 Index
    fp_before = (tmp_path / f"{tmp_path.name}_codebase_index" / "index.json").read_text(
        encoding="utf-8"
    )
    result = await PlanService(app).plan("任务", force_rebuild=True)
    fp_after = (tmp_path / f"{tmp_path.name}_codebase_index" / "index.json").read_text(
        encoding="utf-8"
    )
    assert result["index_before"]["status"] == "VALID"
    # force 重建会重新生成（generated_at 变化）
    import json

    assert json.loads(fp_before)["generated_at"] != json.loads(fp_after)["generated_at"]


# ---------- MCP 返回结构（force_rebuild 透传 + runtime/events） ----------


@pytest.mark.asyncio
async def test_mcp_force_rebuild_runtime_decision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """MCP 层 force_rebuild=true：runtime.decision=rebuild_index（即使 VALID）。"""
    import agentx.mcp.server as mcp_server
    from agentx.providers.mock import MockProvider, text_response
    from tests.helpers import EXPLORE_RESPONSE

    (tmp_path / "main.c").write_text("int main(void) { return 0; }\n", encoding="utf-8")
    app = mcp_server._app(str(tmp_path))
    app.orchestrator.agents["plan"].provider = MockProvider().respond(
        text_response(EXPLORE_RESPONSE),
        text_response('{"summary": "s", "files_involved": ["main.c"], "verification": "echo ok"}'),
    )
    monkeypatch.setattr(mcp_server, "_app", lambda path: app)

    # 先建 Index（MISSING → rebuild）
    r1 = await mcp_server.agentx(str(tmp_path), "任务", action="plan")
    assert r1["runtime"]["decision"]["action"] == "rebuild_index"
    assert "missing" in r1["runtime"]["decision"]["reason"]

    # VALID 默认复用
    r2 = await mcp_server.agentx(str(tmp_path), "任务", action="plan")
    assert r2["runtime"]["index_state"] == "VALID"
    assert r2["runtime"]["decision"]["action"] == "reuse_index"

    # force_rebuild → 显式重建
    r3 = await mcp_server.agentx(str(tmp_path), "任务", action="plan", force_rebuild=True)
    assert r3["runtime"]["decision"]["action"] == "rebuild_index"
    assert r3["runtime"]["decision"]["reason"] == "user requested rebuild"


@pytest.mark.asyncio
async def test_mcp_runtime_workflow_and_events(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """MCP 返回：result 兼容 + runtime.workflow + events 非空。"""
    import agentx.mcp.server as mcp_server
    from agentx.providers.mock import MockProvider, text_response
    from tests.helpers import EXPLORE_RESPONSE

    (tmp_path / "main.c").write_text("int main(void) { return 0; }\n", encoding="utf-8")
    app = mcp_server._app(str(tmp_path))
    app.orchestrator.agents["plan"].provider = MockProvider().respond(
        text_response(EXPLORE_RESPONSE),
        text_response('{"summary": "s", "files_involved": ["main.c"], "verification": "echo ok"}'),
    )
    monkeypatch.setattr(mcp_server, "_app", lambda path: app)

    result = await mcp_server.agentx(str(tmp_path), "任务", action="plan")
    assert result["runtime"]["workflow"] == {"action": "plan", "stage": "completed"}
    assert result["runtime"]["fingerprint"] != ""
    stages = [e["stage"] for e in result["events"]]
    assert "index_check" in stages
    assert "index_decision" in stages
    assert "codegraph_analysis" in stages
    assert "query_context" in stages
    assert "understanding" in stages
    assert "planning" in stages
    assert "completed" in stages


# ---------- CLI 辅助 ----------


def test_print_events_ascii_safe(capsys: pytest.CaptureFixture[str]) -> None:
    """CLI 事件打印 ASCII 安全（GBK 控制台不崩溃）。"""
    from agentx.cli.app import _print_workflow_events

    col = EventCollector()
    col.emit("index_decision", "completed", "reuse_index: fingerprint matched")
    _print_workflow_events(col)
    out = capsys.readouterr().out
    assert "reuse_index" in out
    assert "[completed]" in out
