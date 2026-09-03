"""Phase 6.7：ProgressAdapter（notifications/progress 主展示通道）。

- stage 编号映射 1-10
- 事件 → progress（message 带 [stage] 前缀）
- 心跳 → progress：stage 不变，只更新 message
- 双通道：message 保留 + progress 新增
- 无 progressToken 降级：任务正常，不发 progress
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from agentx.runtime.progress import (
    STAGE_PROGRESS,
    TOTAL_PROGRESS,
    ProgressAdapter,
)

# ---------- stage 映射 ----------


def test_stage_progress_mapping_1_to_10() -> None:
    assert TOTAL_PROGRESS == 10
    assert STAGE_PROGRESS == {
        "index_check": 1,
        "index_decision": 2,
        "index_sync": 3,
        "codegraph_analysis": 4,
        "query_context": 5,
        "understanding": 6,
        "planning": 7,
        "review": 8,
        "verify": 9,
        "completed": 10,
    }


class _FakeSession:
    def __init__(self) -> None:
        self.progress: list[tuple[Any, ...]] = []

    async def send_progress_notification(
        self, token: Any, progress: float, total: float | None = None, message: str | None = None
    ) -> None:
        self.progress.append((token, progress, total, message))


@pytest.mark.asyncio
async def test_progress_adapter_event_mapping() -> None:
    session = _FakeSession()
    adapter = ProgressAdapter(session, token="tok-1")
    adapter.on_event(
        {"stage": "index_sync", "status": "running", "message": "syncing project index L3"}
    )
    adapter.on_event({"stage": "completed", "status": "completed", "message": "plan done"})
    # fire-and-forget：等循环让任务跑
    await asyncio.sleep(0.05)
    assert len(session.progress) == 2
    assert session.progress[0] == ("tok-1", 3, 10, "[index_sync] syncing project index L3")
    assert session.progress[1] == ("tok-1", 10, 10, "[completed] plan done")


@pytest.mark.asyncio
async def test_progress_adapter_unknown_stage_ignored() -> None:
    session = _FakeSession()
    adapter = ProgressAdapter(session, token="t")
    adapter.on_event({"stage": "nope", "status": "running"})
    await asyncio.sleep(0.05)
    assert session.progress == []


@pytest.mark.asyncio
async def test_progress_adapter_beat_keeps_stage_changes_message() -> None:
    session = _FakeSession()
    adapter = ProgressAdapter(session, token="t")
    adapter.on_event({"stage": "understanding", "status": "running", "message": "ensuring"})
    adapter.on_beat(
        {"stage": "understanding", "status": "running", "message": "still running", "elapsed": 45}
    )
    adapter.on_beat(
        {"stage": "understanding", "status": "running", "message": "still running", "elapsed": 60}
    )
    await asyncio.sleep(0.05)
    msgs = [p[3] for p in session.progress]
    # progress 编号不变（都是 understanding=6），只更新 message
    nums = [p[1] for p in session.progress]
    assert nums == [6, 6, 6]
    assert msgs[1] == "[understanding] still running (45s)"
    assert msgs[2] == "[understanding] still running (60s)"


@pytest.mark.asyncio
async def test_progress_adapter_pending_gather_flush() -> None:
    """pending 列表兜底：无真实 await 点也能在 gather 后送达。"""
    session = _FakeSession()
    pending: list[asyncio.Task[None]] = []
    adapter = ProgressAdapter(session, token="t", pending=pending)
    adapter.on_event({"stage": "planning", "status": "running", "message": "planner"})
    assert len(pending) == 1
    await asyncio.gather(*pending, return_exceptions=True)
    assert len(session.progress) == 1


# ---------- MCP 双通道 ----------


class _DualSession:
    """同时记录 notifications/message 与 progress 的假 session。"""

    def __init__(self) -> None:
        self.notifications: list[Any] = []
        self.progress: list[tuple[Any, ...]] = []

    async def send_notification(self, notification: Any, related_request_id: Any = None) -> None:
        self.notifications.append(notification)

    async def send_progress_notification(
        self, token: Any, progress: float, total: float | None = None, message: str | None = None
    ) -> None:
        self.progress.append((token, progress, total, message))


class _FakeContextWithToken:
    def __init__(self, session: Any, token: str | int) -> None:
        class _Meta(dict):
            pass

        meta = _Meta()
        meta["progress_token"] = token
        self.request_context = type("RC", (), {"session": session, "meta": meta})()


@pytest.mark.asyncio
async def test_mcp_dual_channel_message_and_progress(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """有 progressToken：message 通道仍收 + progress 通道收到（Reasonix 主展示）。"""
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

    session = _DualSession()
    result = await mcp_server.agentx(
        str(tmp_path), "任务", action="plan", context=_FakeContextWithToken(session, "rt-1")
    )
    # 最终 result 不变
    assert result["result"]["index_after"]["status"] == "VALID"
    assert result["runtime"]["decision"]["action"] == "rebuild_index"
    # message 通道仍收到
    assert session.notifications, "message 通道应收到"
    msg_payloads = [n.params.data for n in session.notifications]
    assert any(p["type"] == "workflow_event" for p in msg_payloads)
    # progress 通道收到，且顺序与事件一致（stage 编号递增）
    assert session.progress, "progress 通道应收到"
    tokens = {p[0] for p in session.progress}
    assert tokens == {"rt-1"}
    nums = [p[1] for p in session.progress]
    totals = {p[2] for p in session.progress}
    assert totals == {10}
    # 第一个事件是 index_check(1)，最后是 completed(10)
    # （执行顺序可能非单调：query_context=5 在 understanding=6 之后执行）
    assert nums[0] == 1
    assert nums[-1] == 10
    assert all(1 <= n <= 10 for n in nums)
    # message 带 [stage] 前缀
    first_msg = session.progress[0][3]
    assert first_msg.startswith("[index_check]")


@pytest.mark.asyncio
async def test_mcp_without_token_degrades_to_message_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """无 progressToken：任务正常，不发 progress，message 通道照旧。"""
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

    session = _DualSession()

    class _FakeContextNoMeta:
        request_context = type("RC", (), {"session": session, "meta": None})()

    result = await mcp_server.agentx(
        str(tmp_path), "任务", action="plan", context=_FakeContextNoMeta()
    )
    assert result["result"]["index_after"]["status"] == "VALID"
    assert session.notifications, "无 token 时 message 通道仍工作"
    assert session.progress == [], "无 token 时不应发 progress"


def test_progress_token_extraction() -> None:
    import agentx.mcp.server as mcp_server

    class _Meta(dict):
        pass

    meta = _Meta()
    meta["progress_token"] = "abc"
    ctx = type("C", (), {"request_context": type("RC", (), {"meta": meta})()})()
    assert mcp_server._progress_token_from_context(ctx) == "abc"

    ctx_none = type("C", (), {"request_context": type("RC", (), {"meta": None})()})()
    assert mcp_server._progress_token_from_context(ctx_none) is None
    assert mcp_server._progress_token_from_context(None) is None


@pytest.mark.asyncio
async def test_mcp_heartbeat_dual_channel(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """心跳：message + progress 双通道；progress 不改变 stage 编号。"""
    import agentx.mcp.server as mcp_server
    from agentx.runtime.events import EventCollector, Heartbeat

    # 直接测 Heartbeat + 双通道 sender（短 interval）
    col = EventCollector()
    session = _DualSession()
    from agentx.runtime.progress import ProgressAdapter

    adapter = ProgressAdapter(session, "hb-1")
    hb = Heartbeat(
        col, on_beat=mcp_server._make_heartbeat_sender(session, [], adapter), interval=0.05
    )
    col.emit("index_sync", "running", "syncing")
    hb.start()
    await asyncio.sleep(0.13)
    hb.stop()
    await asyncio.sleep(0.05)
    # message 通道有 heartbeat
    msg_types = [n.params.data.get("type") for n in session.notifications]
    assert "workflow_heartbeat" in msg_types
    # progress 通道有 heartbeat，且编号保持 index_sync=3
    assert session.progress
    for p in session.progress:
        assert p[1] == 3
    # 主事件列表无心跳污染
    assert [e["stage"] for e in col.events()] == ["index_sync"]
