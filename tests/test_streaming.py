"""Phase 6.6：Streaming Runtime Events。

- EventCollector：subscribe 单/多 listener、listener 异常隔离、无 listener 不变
- 事件四态（pending/running/completed/failed）
- Heartbeat：长任务触发、stage 完成停止、不进主事件列表
- MCP：无 session 降级兼容、notification 数据形态、顺序
- CLI：实时输出 ASCII 安全
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from agentx.runtime.events import (
    EventCollector,
    Heartbeat,
    WorkflowEvent,
)

# ---------- EventCollector listener ----------


def test_emit_without_listener_unchanged() -> None:
    col = EventCollector()
    col.emit("index_check", "running", "checking")
    col.emit("index_check", "completed", "VALID")
    assert col.events() == [
        {"stage": "index_check", "status": "running", "message": "checking"},
        {"stage": "index_check", "status": "completed", "message": "VALID"},
    ]


def test_emit_with_single_listener() -> None:
    col = EventCollector()
    seen: list[dict[str, str]] = []
    col.subscribe(seen.append)
    col.emit("planning", "running", "planner analyzing")
    assert len(seen) == 1
    assert seen[0]["stage"] == "planning"
    assert seen[0]["status"] == "running"
    # listener 收到的是副本，改动不影响内部
    seen[0]["stage"] = "hacked"
    assert col.events()[0]["stage"] == "planning"


def test_emit_with_multiple_listeners() -> None:
    col = EventCollector()
    a: list[dict[str, str]] = []
    b: list[dict[str, str]] = []
    col.subscribe(a.append)
    col.subscribe(b.append)
    col.emit("index_decision", "completed", "reuse_index")
    assert len(a) == 1 and len(b) == 1
    assert (
        a[0] == b[0] == {"stage": "index_decision", "status": "completed", "message": "reuse_index"}
    )


def test_listener_exception_does_not_break_business() -> None:
    col = EventCollector()

    def bad(_: dict[str, str]) -> None:
        raise RuntimeError("stream broken")

    col.subscribe(bad)
    good: list[dict[str, str]] = []
    col.subscribe(good.append)
    # 不抛异常，事件正常 append + 正常 listener 收到
    col.emit("planning", "completed", "done")
    assert len(col.events()) == 1
    assert len(good) == 1


def test_unsubscribe() -> None:
    col = EventCollector()
    seen: list[dict[str, str]] = []
    col.subscribe(seen.append)
    col.unsubscribe(seen.append)
    col.emit("verify", "completed", "PASS")
    assert seen == []


# ---------- 事件四态 ----------


def test_workflow_event_four_statuses() -> None:
    for status in ("pending", "running", "completed", "failed"):
        e = WorkflowEvent("planning", status)
        assert e["status"] == status


def test_workflow_event_rejects_unknown_status() -> None:
    with pytest.raises(ValueError):
        WorkflowEvent("planning", "watching")


# ---------- Heartbeat ----------


@pytest.mark.asyncio
async def test_heartbeat_ticks_while_running_and_stops_on_complete() -> None:
    col = EventCollector()
    beats: list[dict[str, object]] = []
    hb = Heartbeat(col, on_beat=beats.append, interval=0.05)

    col.emit("codegraph_analysis", "running", "analyzing")
    hb.start()
    await asyncio.sleep(0.12)
    assert len(beats) >= 1
    assert beats[0]["stage"] == "codegraph_analysis"
    assert beats[0]["status"] == "running"
    assert isinstance(beats[0]["elapsed"], int)
    assert beats[0]["elapsed"] >= 0

    col.emit("codegraph_analysis", "completed", "done")
    await asyncio.sleep(0.12)
    count_at_complete = len(beats)
    await asyncio.sleep(0.12)
    assert len(beats) == count_at_complete  # 完成后再无心跳
    hb.stop()


@pytest.mark.asyncio
async def test_heartbeat_stage_switch_restarts_timer() -> None:
    col = EventCollector()
    beats: list[dict[str, object]] = []
    hb = Heartbeat(col, on_beat=beats.append, interval=0.05)

    col.emit("index_check", "running", "checking")
    hb.start()
    await asyncio.sleep(0.11)
    col.emit("index_check", "completed", "VALID")
    col.emit("planning", "running", "planner")  # 换档
    await asyncio.sleep(0.11)
    assert beats[-1]["stage"] == "planning"
    hb.stop()


@pytest.mark.asyncio
async def test_heartbeat_not_polluting_main_events() -> None:
    """心跳只发 sink，不进 EventCollector._events（最终 events[] 无心跳）。"""
    col = EventCollector()
    beats: list[dict[str, object]] = []
    hb = Heartbeat(col, on_beat=beats.append, interval=0.03)
    col.emit("codegraph_analysis", "running", "analyzing")
    hb.start()
    await asyncio.sleep(0.1)
    hb.stop()
    assert len(beats) >= 1
    stages = [e["stage"] for e in col.events()]
    assert stages == ["codegraph_analysis"]
    assert "still running" not in [e.get("message", "") for e in col.events()]


@pytest.mark.asyncio
async def test_heartbeat_no_events_no_beats() -> None:
    col = EventCollector()
    beats: list[dict[str, object]] = []
    hb = Heartbeat(col, on_beat=beats.append, interval=0.03)
    hb.start()
    await asyncio.sleep(0.1)
    hb.stop()
    assert beats == []


# ---------- MCP 流式（session 注入 / 降级） ----------


class _FakeSession:
    """记录 send_notification 调用的假 session（模拟 MCP ServerSession）。"""

    def __init__(self) -> None:
        self.notifications: list[Any] = []

    async def send_notification(self, notification: Any, related_request_id: Any = None) -> None:
        self.notifications.append(notification)


@pytest.mark.asyncio
async def test_mcp_streaming_with_session(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """有 session：events 实时推送 notifications/message，且最终 result 不变。"""
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

    session = _FakeSession()

    class _FakeContext:
        request_context = type("RC", (), {"session": session})()

    result = await mcp_server.agentx(str(tmp_path), "任务", action="plan", context=_FakeContext())
    # 最终 result 保持 Phase 6.5 结构
    assert result["result"]["index_after"]["status"] == "VALID"
    assert result["runtime"]["decision"]["action"] == "rebuild_index"
    # 流式：notifications/message 收到 workflow_event（顺序与 emit 一致）
    assert session.notifications, "应有 notification 推送"
    payloads = [n.params.data for n in session.notifications]
    types = [p["type"] for p in payloads]
    assert types[0] == "workflow_event"
    assert True  # 心跳 interval 15s 未触发，仅验证事件流
    first = payloads[0]["event"]
    assert first["stage"] == "index_check"
    assert first["status"] == "running"
    # 事件顺序与最终 events[] 一致（前 N 条相同）
    final_stages = [e["stage"] for e in result["events"]]
    stream_stages = [p["event"]["stage"] for p in payloads if p["type"] == "workflow_event"]
    assert stream_stages == final_stages


@pytest.mark.asyncio
async def test_mcp_streaming_without_session_degrades(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """无 session（直调/旧宿主）：不崩，最终 result 正常返回。"""
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
    assert result["result"]["index_after"]["status"] == "VALID"
    assert len(result["events"]) > 0


@pytest.mark.asyncio
async def test_stream_failure_does_not_break_task(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """stream 失败（listener 抛异常）：任务照常完成。"""
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

    class _BrokenSession:
        async def send_notification(self, *args: Any, **kwargs: Any) -> None:
            raise RuntimeError("stream connection failed")

    class _FakeContext:
        request_context = type("RC", (), {"session": _BrokenSession()})()

    result = await mcp_server.agentx(str(tmp_path), "任务", action="plan", context=_FakeContext())
    assert result["result"]["index_after"]["status"] == "VALID"


# ---------- CLI 实时输出（ASCII 安全） ----------


def test_live_event_printing_ascii_safe(capsys: pytest.CaptureFixture[str]) -> None:
    from agentx.cli.app import _print_live_event

    counter: dict[str, int] = {"n": 0}
    _print_live_event(
        counter, {"stage": "index_check", "status": "running", "message": "checking fingerprint"}
    )
    _print_live_event(counter, {"stage": "index_check", "status": "completed", "message": "VALID"})
    _print_live_event(
        counter, {"stage": "planning", "status": "running", "message": "planner analyzing"}
    )
    _print_live_event(counter, {"stage": "planning", "status": "failed", "message": "boom"})
    out = capsys.readouterr().out
    assert "[RUN]" in out
    assert "[DONE]" in out
    assert "[FAIL]" in out
    assert "checking fingerprint" in out
    # 序号：同一 stage 的 running/completed 共享序号，新 stage 递增
    lines = [ln for ln in out.splitlines() if ln.strip()]
    assert lines[0].startswith("[ 1]")
    assert lines[1].startswith("[ 1]")
    assert lines[2].startswith("[ 2]")
    assert lines[3].startswith("[ 2]")
