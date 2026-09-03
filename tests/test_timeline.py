"""P0 测试：统一时间线（对话流数据源）。"""

from __future__ import annotations

import uuid

import pytest

from agentx.core.timeline import (
    KIND_CHANGE,
    KIND_DECISION,
    KIND_EVIDENCE,
    KIND_MESSAGE,
    KIND_STATE,
    KIND_TOOL,
    ROLE_AGENTX,
    ROLE_EXECUTOR,
    ROLE_REVIEWER,
    ROLE_VERIFIER,
    build_timeline,
)
from agentx.state.models import (
    Change,
    Decision,
    Evidence,
    Message,
    Project,
    Task,
)
from agentx.state.store import SQLiteStore


def _id() -> str:
    return uuid.uuid4().hex[:12]


@pytest.fixture()
def store(tmp_path) -> SQLiteStore:
    s = SQLiteStore(tmp_path / "db.sqlite")
    s.open()
    yield s
    s.close()


@pytest.fixture()
def task(store: SQLiteStore) -> Task:
    store.insert_project(Project(id="p1", root_path="."))
    t = Task(id="t1", project_id="p1", goal="测试")
    store.insert_task(t)
    return t


def test_timeline_orders_messages_by_time(store: SQLiteStore, task: Task) -> None:
    m1 = Message(id=_id(), task_id=task.id, sender="executor-1", content="第一条")
    m2 = Message(id=_id(), task_id=task.id, sender="reviewer-1", content="第二条")
    store.insert_message(m2)
    store.insert_message(m1)

    timeline = build_timeline(store, task.id)
    contents = [e.content for e in timeline if e.kind == KIND_MESSAGE]
    assert contents == ["第一条", "第二条"]  # 按时间（非插入顺序）


def test_timeline_identifies_agent_roles(store: SQLiteStore, task: Task) -> None:
    store.insert_message(Message(id=_id(), task_id=task.id, sender="executor-1", content="x"))
    store.insert_message(Message(id=_id(), task_id=task.id, sender="reviewer-1", content="y"))
    store.insert_message(Message(id=_id(), task_id=task.id, sender="verifier-1", content="z"))

    timeline = build_timeline(store, task.id)
    roles = {e.sender_role for e in timeline}
    assert {ROLE_EXECUTOR, ROLE_REVIEWER, ROLE_VERIFIER} <= roles


def test_timeline_marks_finding_messages(store: SQLiteStore, task: Task) -> None:
    content = (
        '{"findings": [{"severity": "HIGH", "category": "回归", '
        '"location": "param.c", "description": "缺少回滚"}]}'
    )
    store.insert_message(Message(id=_id(), task_id=task.id, sender="reviewer-1", content=content))

    timeline = build_timeline(store, task.id)
    entry = timeline[0]
    assert entry.payload["structured"] == "finding"
    assert entry.payload["finding"][0]["severity"] == "HIGH"


def test_timeline_marks_verdict_messages(store: SQLiteStore, task: Task) -> None:
    content = '{"build": {"command": "make"}, "tests": [], "conclusion": "PASS"}'
    store.insert_message(Message(id=_id(), task_id=task.id, sender="verifier-1", content=content))

    timeline = build_timeline(store, task.id)
    entry = timeline[0]
    assert entry.payload["structured"] == "verdict"
    assert entry.payload["verdict"]["conclusion"] == "PASS"


def test_timeline_includes_decision_evidence_change(store: SQLiteStore, task: Task) -> None:
    store.insert_decision(Decision(id=_id(), task_id=task.id, result="PASS", reason="build 通过"))
    store.insert_evidence(
        Evidence(id=_id(), task_id=task.id, type="test.run", command="make", exit_code=0)
    )
    store.insert_change(Change(id=_id(), task_id=task.id, file="param.c", operation="WRITE"))

    timeline = build_timeline(store, task.id)
    kinds = {e.kind for e in timeline}
    assert KIND_DECISION in kinds
    assert KIND_EVIDENCE in kinds
    assert KIND_CHANGE in kinds
    decision = [e for e in timeline if e.kind == KIND_DECISION][0]
    assert decision.sender_role == ROLE_AGENTX
    assert decision.payload["result"] == "PASS"


def test_timeline_merges_tool_events(store: SQLiteStore, task: Task) -> None:
    from agentx.state.models import StoredEvent

    store.append_event(
        StoredEvent(
            task_id=task.id,
            agent_id="executor-1",
            type="ToolStarted",
            payload={"tool": "fs.read_file", "args": {"path": "param.c"}},
        )
    )
    store.append_event(
        StoredEvent(
            task_id=task.id,
            agent_id="executor-1",
            type="ToolFinished",
            payload={"tool": "fs.read_file", "ok": True, "exit_code": None, "output": "内容"},
        )
    )
    store.append_event(
        StoredEvent(
            task_id=task.id,
            type="TaskStateChanged",
            payload={"from": "EXECUTING", "to": "REVIEWING"},
        )
    )

    timeline = build_timeline(store, task.id)
    tools = [e for e in timeline if e.kind == KIND_TOOL]
    states = [e for e in timeline if e.kind == KIND_STATE]
    assert len(tools) == 1  # Started+Finished 合并
    assert tools[0].payload["args"] == {"path": "param.c"}
    assert tools[0].payload["ok"] is True
    assert tools[0].payload["output"] == "内容"
    assert len(states) == 1
    assert states[0].content == "EXECUTING → REVIEWING"


def test_timeline_plain_text_message_not_structured(store: SQLiteStore, task: Task) -> None:
    store.insert_message(
        Message(id=_id(), task_id=task.id, sender="executor-1", content="完成总结")
    )
    timeline = build_timeline(store, task.id)
    assert not timeline[0].is_structured
