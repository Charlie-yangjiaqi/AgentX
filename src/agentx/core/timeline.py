"""统一时间线：对话流的数据源。

把 Task 生命周期内的所有可观察事实按时间交错合并：
Agent 消息、决策节点、证据、改动、工具调用、状态变化。
TUI 左栏按这个时间线渲染对话流。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from agentx.state.models import AgentXModel, StoredEvent
from agentx.state.store import SQLiteStore

KIND_MESSAGE = "message"
KIND_DECISION = "decision"
KIND_EVIDENCE = "evidence"
KIND_CHANGE = "change"
KIND_TOOL = "tool"
KIND_STATE = "state"

ROLE_EXECUTOR = "executor"
ROLE_REVIEWER = "reviewer"
ROLE_VERIFIER = "verifier"
ROLE_AGENTX = "agentx"
ROLE_USER = "user"

_AGENT_ROLE_BY_ID: dict[str, str] = {}


def _role_for(sender: str) -> str:
    """从 agent id（executor-1 等）推断角色。"""
    if sender in _AGENT_ROLE_BY_ID:
        return _AGENT_ROLE_BY_ID[sender]
    if sender.startswith("executor"):
        role = ROLE_EXECUTOR
    elif sender.startswith("reviewer"):
        role = ROLE_REVIEWER
    elif sender.startswith("verifier"):
        role = ROLE_VERIFIER
    else:
        role = sender
    _AGENT_ROLE_BY_ID[sender] = role
    return role


class TimelineEntry(AgentXModel):
    seq: int
    key: str = ""
    ts: datetime
    kind: str
    sender: str | None = None
    sender_role: str | None = None
    content: str = ""
    payload: dict[str, Any] = {}

    @property
    def is_structured(self) -> bool:
        return bool(self.payload.get("structured"))


def build_timeline(store: SQLiteStore, task_id: str) -> list[TimelineEntry]:
    """按时间交错合并所有事实，返回稳定排序的时间线。"""
    raw: list[tuple[datetime, str, TimelineEntry]] = []

    def add(entry: TimelineEntry, ts: datetime, sort_key: str) -> None:
        entry.key = sort_key
        raw.append((ts, sort_key, entry))

    # Agent 消息（含结构化 Finding / Verdict 识别）
    for msg in store.list_messages(task_id):
        payload: dict[str, Any] = {}
        content = msg.content or ""
        structured = _detect_structured(content)
        if structured is not None:
            payload["structured"] = structured["kind"]
            payload[structured["kind"]] = structured["data"]
        add(
            TimelineEntry(
                seq=0,
                ts=msg.created_at,
                kind=KIND_MESSAGE,
                sender=msg.sender,
                sender_role=_role_for(msg.sender),
                content=content,
                payload=payload,
            ),
            msg.created_at,
            f"m:{msg.id}",
        )

    # 决策节点（AgentX）
    for decision in store.list_decisions(task_id):
        add(
            TimelineEntry(
                seq=0,
                ts=decision.created_at,
                kind=KIND_DECISION,
                sender=ROLE_AGENTX,
                sender_role=ROLE_AGENTX,
                content=decision.reason or "",
                payload={
                    "result": decision.result,
                    "rule": decision.rule,
                    "structured": "decision",
                },
            ),
            decision.created_at,
            f"d:{decision.id}",
        )

    # 证据
    for ev in store.list_evidence(task_id):
        add(
            TimelineEntry(
                seq=0,
                ts=ev.timestamp,
                kind=KIND_EVIDENCE,
                sender=ROLE_AGENTX,
                sender_role=ROLE_AGENTX,
                content=(ev.command or "") + f" exit={ev.exit_code}",
                payload={"type": ev.type, "exit_code": ev.exit_code, "result": ev.result},
            ),
            ev.timestamp,
            f"e:{ev.id}",
        )

    # 改动
    for change in store.list_changes(task_id):
        add(
            TimelineEntry(
                seq=0,
                ts=change.timestamp,
                kind=KIND_CHANGE,
                sender=ROLE_EXECUTOR,
                sender_role=ROLE_EXECUTOR,
                content=f"{change.operation} {change.file}",
                payload={"operation": change.operation, "file": change.file},
            ),
            change.timestamp,
            f"c:{change.id}",
        )

    # 工具调用与状态变化（事件）：ToolStarted/ToolFinished 合并为一条
    pending_tools: list[tuple[StoredEvent, str]] = []
    for event in store.list_events(task_id):
        if event.type == "ToolStarted":
            pending_tools.append((event, f"ev:{event.seq or 0}"))
        elif event.type == "ToolFinished" and pending_tools:
            start_event, sort_key = pending_tools.pop(0)
            payload = dict(start_event.payload)
            payload.update(event.payload)
            add(
                TimelineEntry(
                    seq=0,
                    ts=start_event.ts,
                    kind=KIND_TOOL,
                    sender=event.agent_id,
                    sender_role=_role_for(event.agent_id) if event.agent_id else None,
                    content=str(event.payload.get("tool", "")),
                    payload=payload,
                ),
                start_event.ts,
                sort_key,
            )
        elif event.type == "TaskStateChanged":
            add(
                TimelineEntry(
                    seq=0,
                    ts=event.ts,
                    kind=KIND_STATE,
                    sender=ROLE_AGENTX,
                    sender_role=ROLE_AGENTX,
                    content=f"{event.payload.get('from')} → {event.payload.get('to')}",
                    payload=dict(event.payload),
                ),
                event.ts,
                f"ev:{event.seq or 0}",
            )

    # 统一排序：时间升序；同一时间用各自唯一键稳定排序
    raw.sort(key=lambda item: (item[0], item[1]))
    for i, (_, _, entry) in enumerate(raw):
        entry.seq = i
    return [entry for (_, _, entry) in raw]


def _detect_structured(content: str) -> dict[str, Any] | None:
    """识别消息里的结构化输出：finding（Reviewer）/ verdict（Verifier）。"""
    import json

    start = content.find("{")
    end = content.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        data = json.loads(content[start : end + 1])
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    if isinstance(data.get("findings"), list):
        return {"kind": "finding", "data": data.get("findings")}
    if "conclusion" in data or "build" in data or "tests" in data:
        return {"kind": "verdict", "data": data}
    return None
