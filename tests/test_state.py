"""P1 测试：领域模型 + SQLite 持久化。"""

from __future__ import annotations

import pytest

from agentx.state.models import (
    EVENT_TASK_STATE_CHANGED,
    AgentRole,
    AgentSession,
    Change,
    Decision,
    Evidence,
    Finding,
    FindingSeverity,
    Message,
    MessageScope,
    Project,
    StoredEvent,
    Task,
    TaskState,
)
from agentx.state.store import SQLiteStore


@pytest.fixture()
def store(tmp_path) -> SQLiteStore:
    s = SQLiteStore(tmp_path / "agentx.db")
    s.open()
    yield s
    s.close()


def test_project_roundtrip(store: SQLiteStore) -> None:
    p = Project(id="p1", root_path="/repo", branch="main")
    store.insert_project(p)
    got = store.get_project("p1")
    assert got is not None
    assert got.root_path == "/repo"
    assert got.branch == "main"


def test_task_roundtrip_and_state_machine(store: SQLiteStore) -> None:
    task = Task(id="t1", project_id="p1", goal="实现参数事务功能")
    store.insert_task(task)
    got = store.get_task("t1")
    assert got is not None
    assert got.state == TaskState.CREATED
    assert got.iteration == 0

    updated = store.update_task_state("t1", TaskState.EXECUTING, iteration=1)
    assert updated is not None
    assert updated.state == TaskState.EXECUTING
    assert updated.iteration == 1
    assert store.get_task("t1").state == TaskState.EXECUTING


def test_terminal_states() -> None:
    assert TaskState.COMPLETED.is_terminal
    assert TaskState.FAILED.is_terminal
    assert TaskState.CANCELLED.is_terminal
    assert not TaskState.EXECUTING.is_terminal


def test_agent_roundtrip(store: SQLiteStore) -> None:
    agent = AgentSession(id="a1", role=AgentRole.EXECUTOR, model="gpt-x")
    store.insert_agent(agent)
    got = store.get_agent("a1")
    assert got is not None
    assert got.role == AgentRole.EXECUTOR

    store.update_agent_status("a1", "working")
    assert store.get_agent("a1").status.value == "working"


def test_message_with_scope(store: SQLiteStore) -> None:
    msg = Message(
        id="m1",
        task_id="t1",
        sender="user",
        target="executor",
        scope=MessageScope.AGENT_PRIVATE,
        content="不要改旧 API",
    )
    store.insert_message(msg)
    got = store.list_messages("t1")
    assert len(got) == 1
    assert got[0].scope == MessageScope.AGENT_PRIVATE
    assert got[0].content == "不要改旧 API"


def test_finding_and_evidence(store: SQLiteStore) -> None:
    finding = Finding(
        id="f1",
        task_id="t1",
        severity=FindingSeverity.HIGH,
        category="compat",
        location="src/api.c:42",
        description="rollback state not restored",
    )
    store.insert_finding(finding)
    ev = Evidence(
        id="e1",
        task_id="t1",
        type="build",
        command="make",
        result="exit 0",
        exit_code=0,
    )
    store.insert_evidence(ev)

    findings = store.list_findings("t1")
    assert len(findings) == 1
    assert findings[0].severity == FindingSeverity.HIGH

    evidence = store.list_evidence("t1")
    assert len(evidence) == 1
    assert evidence[0].exit_code == 0


def test_decision_record(store: SQLiteStore) -> None:
    d = Decision(id="d1", task_id="t1", rule="no_blocker_findings", result="PASS", reason="ok")
    store.insert_decision(d)
    got = store.list_decisions("t1")
    assert len(got) == 1
    assert got[0].result == "PASS"


def test_change_record(store: SQLiteStore) -> None:
    c = Change(id="c1", task_id="t1", file="src/param.c", operation="MODIFY", diff_hash="abc")
    store.insert_change(c)
    got = store.list_changes("t1")
    assert len(got) == 1
    assert got[0].file == "src/param.c"


def test_events_append_and_replay(store: SQLiteStore) -> None:
    e1 = StoredEvent(task_id="t1", type=EVENT_TASK_STATE_CHANGED, payload={"state": "EXECUTING"})
    e2 = StoredEvent(task_id="t1", type="ToolFinished", payload={"tool": "make"})
    store.append_event(e1)
    store.append_event(e2)

    events = store.list_events(task_id="t1")
    assert len(events) == 2
    assert events[0].seq == 1
    assert events[0].payload["state"] == "EXECUTING"
    assert events[1].seq == 2

    after = store.list_events(task_id="t1", after_seq=1)
    assert len(after) == 1
    assert after[0].type == "ToolFinished"


def test_unicode_content(store: SQLiteStore) -> None:
    task = Task(id="t1", project_id="p1", goal="实现参数事务功能，保持现有 API 兼容")
    store.insert_task(task)
    assert store.get_task("t1").goal == "实现参数事务功能，保持现有 API 兼容"
