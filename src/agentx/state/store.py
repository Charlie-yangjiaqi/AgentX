"""SQLite 持久化层：Project State 的可恢复事实层。

V1 单机 SQLite，零外部服务。所有时间戳以 ISO 8601 (UTC) 文本存储。
"""

from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

from agentx.state.models import (
    AgentSession,
    Change,
    Decision,
    Evidence,
    Finding,
    Message,
    Project,
    StoredEvent,
    Task,
    utcnow,
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
    id          TEXT PRIMARY KEY,
    root_path   TEXT NOT NULL,
    branch      TEXT,
    created_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS tasks (
    id             TEXT PRIMARY KEY,
    project_id     TEXT NOT NULL,
    goal           TEXT NOT NULL,
    state          TEXT NOT NULL,
    iteration      INTEGER NOT NULL DEFAULT 0,
    max_iterations INTEGER NOT NULL DEFAULT 5,
    created_at     TEXT NOT NULL,
    updated_at     TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS agents (
    id           TEXT PRIMARY KEY,
    role         TEXT NOT NULL,
    provider_ref TEXT NOT NULL,
    model        TEXT,
    status       TEXT NOT NULL,
    created_at   TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS messages (
    id         TEXT PRIMARY KEY,
    task_id    TEXT,
    sender     TEXT NOT NULL,
    target     TEXT,
    scope      TEXT NOT NULL,
    content    TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS changes (
    id         TEXT PRIMARY KEY,
    task_id    TEXT NOT NULL,
    file       TEXT NOT NULL,
    operation  TEXT NOT NULL,
    diff_hash  TEXT,
    timestamp  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS findings (
    id          TEXT PRIMARY KEY,
    task_id     TEXT NOT NULL,
    severity    TEXT NOT NULL,
    category    TEXT NOT NULL,
    location    TEXT,
    description TEXT NOT NULL,
    status      TEXT NOT NULL,
    created_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS evidence (
    id         TEXT PRIMARY KEY,
    task_id    TEXT NOT NULL,
    type       TEXT NOT NULL,
    command    TEXT,
    result     TEXT,
    exit_code  INTEGER,
    timestamp  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS decisions (
    id         TEXT PRIMARY KEY,
    task_id    TEXT NOT NULL,
    rule       TEXT,
    result     TEXT NOT NULL,
    reason     TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    seq      INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id  TEXT,
    agent_id TEXT,
    type     TEXT NOT NULL,
    payload  TEXT NOT NULL,
    ts       TEXT NOT NULL
);
"""


def _iso(dt: datetime) -> str:
    return dt.isoformat()


class SQLiteStore:
    """Project State 的持久化仓库。

    同步实现（sqlite3），异步调用方用 asyncio.to_thread 包一层。
    """

    def __init__(self, db_path: str | Path) -> None:
        self._path = Path(db_path)
        self._conn: sqlite3.Connection | None = None

    @property
    def path(self) -> Path:
        return self._path

    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = self._connect()
        return self._conn

    def _connect(self) -> sqlite3.Connection:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self._path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def open(self) -> None:
        """打开连接并初始化 schema。"""
        self.conn.executescript(_SCHEMA)
        self.conn.commit()

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def __enter__(self) -> SQLiteStore:
        self.open()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # ---------- projects ----------

    def insert_project(self, project: Project) -> Project:
        self.conn.execute(
            "INSERT INTO projects (id, root_path, branch, created_at) VALUES (?, ?, ?, ?)",
            (project.id, project.root_path, project.branch, _iso(project.created_at)),
        )
        self.conn.commit()
        return project

    def get_project(self, project_id: str) -> Project | None:
        row = self.conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
        if row is None:
            return None
        return Project(**dict(row))

    def list_projects(self) -> list[Project]:
        rows = self.conn.execute("SELECT * FROM projects ORDER BY created_at").fetchall()
        return [Project(**dict(r)) for r in rows]

    # ---------- tasks ----------

    def insert_task(self, task: Task) -> Task:
        self.conn.execute(
            "INSERT INTO tasks (id, project_id, goal, state, iteration, max_iterations,"
            " created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                task.id,
                task.project_id,
                task.goal,
                task.state.value,
                task.iteration,
                task.max_iterations,
                _iso(task.created_at),
                _iso(task.updated_at),
            ),
        )
        self.conn.commit()
        return task

    def get_task(self, task_id: str) -> Task | None:
        row = self.conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if row is None:
            return None
        return Task(**dict(row))

    def list_tasks(self, project_id: str | None = None) -> list[Task]:
        if project_id is None:
            rows = self.conn.execute("SELECT * FROM tasks ORDER BY created_at").fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM tasks WHERE project_id = ? ORDER BY created_at", (project_id,)
            ).fetchall()
        return [Task(**dict(r)) for r in rows]

    def update_task_state(
        self, task_id: str, state: Any, iteration: int | None = None
    ) -> Task | None:
        task = self.get_task(task_id)
        if task is None:
            return None
        task.state = state
        task.updated_at = utcnow()
        if iteration is not None:
            task.iteration = iteration
        self.conn.execute(
            "UPDATE tasks SET state = ?, iteration = ?, updated_at = ? WHERE id = ?",
            (task.state.value, task.iteration, _iso(task.updated_at), task_id),
        )
        self.conn.commit()
        return task

    # ---------- agents ----------

    def insert_agent(self, agent: AgentSession) -> AgentSession:
        self.conn.execute(
            "INSERT OR REPLACE INTO agents (id, role, provider_ref, model, status, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (
                agent.id,
                agent.role.value,
                agent.provider_ref,
                agent.model,
                agent.status.value,
                _iso(agent.created_at),
            ),
        )
        self.conn.commit()
        return agent

    def get_agent(self, agent_id: str) -> AgentSession | None:
        row = self.conn.execute("SELECT * FROM agents WHERE id = ?", (agent_id,)).fetchone()
        if row is None:
            return None
        return AgentSession(**dict(row))

    def list_agents(self) -> list[AgentSession]:
        rows = self.conn.execute("SELECT * FROM agents ORDER BY created_at").fetchall()
        return [AgentSession(**dict(r)) for r in rows]

    def update_agent_status(self, agent_id: str, status: Any) -> AgentSession | None:
        agent = self.get_agent(agent_id)
        if agent is None:
            return None
        agent.status = status
        self.conn.execute(
            "UPDATE agents SET status = ? WHERE id = ?", (agent.status.value, agent_id)
        )
        self.conn.commit()
        return agent

    # ---------- messages ----------

    def insert_message(self, message: Message) -> Message:
        self.conn.execute(
            "INSERT INTO messages (id, task_id, sender, target, scope, content, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                message.id,
                message.task_id,
                message.sender,
                message.target,
                message.scope.value,
                message.content,
                _iso(message.created_at),
            ),
        )
        self.conn.commit()
        return message

    def list_messages(self, task_id: str | None = None) -> list[Message]:
        if task_id is None:
            rows = self.conn.execute("SELECT * FROM messages ORDER BY created_at").fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM messages WHERE task_id = ? ORDER BY created_at", (task_id,)
            ).fetchall()
        return [Message(**dict(r)) for r in rows]

    # ---------- changes ----------

    def insert_change(self, change: Change) -> Change:
        self.conn.execute(
            "INSERT INTO changes (id, task_id, file, operation, diff_hash, timestamp)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (
                change.id,
                change.task_id,
                change.file,
                change.operation,
                change.diff_hash,
                _iso(change.timestamp),
            ),
        )
        self.conn.commit()
        return change

    def list_changes(self, task_id: str) -> list[Change]:
        rows = self.conn.execute(
            "SELECT * FROM changes WHERE task_id = ? ORDER BY timestamp", (task_id,)
        ).fetchall()
        return [Change(**dict(r)) for r in rows]

    # ---------- findings ----------

    def insert_finding(self, finding: Finding) -> Finding:
        self.conn.execute(
            "INSERT INTO findings (id, task_id, severity, category, location, description,"
            " status, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                finding.id,
                finding.task_id,
                finding.severity.value,
                finding.category,
                finding.location,
                finding.description,
                finding.status.value,
                _iso(finding.created_at),
            ),
        )
        self.conn.commit()
        return finding

    def list_findings(self, task_id: str | None = None) -> list[Finding]:
        if task_id is None:
            rows = self.conn.execute("SELECT * FROM findings ORDER BY created_at").fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM findings WHERE task_id = ? ORDER BY created_at", (task_id,)
            ).fetchall()
        return [Finding(**dict(r)) for r in rows]

    def update_finding_status(self, finding_id: str, status: Any) -> Finding | None:
        finding = self.get_finding(finding_id)
        if finding is None:
            return None
        finding.status = status
        self.conn.execute(
            "UPDATE findings SET status = ? WHERE id = ?", (finding.status.value, finding_id)
        )
        self.conn.commit()
        return finding

    def get_finding(self, finding_id: str) -> Finding | None:
        row = self.conn.execute("SELECT * FROM findings WHERE id = ?", (finding_id,)).fetchone()
        if row is None:
            return None
        return Finding(**dict(row))

    # ---------- evidence ----------

    def insert_evidence(self, evidence: Evidence) -> Evidence:
        self.conn.execute(
            "INSERT INTO evidence (id, task_id, type, command, result, exit_code, timestamp)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                evidence.id,
                evidence.task_id,
                evidence.type,
                evidence.command,
                evidence.result,
                evidence.exit_code,
                _iso(evidence.timestamp),
            ),
        )
        self.conn.commit()
        return evidence

    def list_evidence(self, task_id: str | None = None) -> list[Evidence]:
        if task_id is None:
            rows = self.conn.execute("SELECT * FROM evidence ORDER BY timestamp").fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM evidence WHERE task_id = ? ORDER BY timestamp", (task_id,)
            ).fetchall()
        return [Evidence(**dict(r)) for r in rows]

    # ---------- decisions ----------

    def insert_decision(self, decision: Decision) -> Decision:
        self.conn.execute(
            "INSERT INTO decisions (id, task_id, rule, result, reason, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (
                decision.id,
                decision.task_id,
                decision.rule,
                decision.result,
                decision.reason,
                _iso(decision.created_at),
            ),
        )
        self.conn.commit()
        return decision

    def list_decisions(self, task_id: str | None = None) -> list[Decision]:
        if task_id is None:
            rows = self.conn.execute("SELECT * FROM decisions ORDER BY created_at").fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM decisions WHERE task_id = ? ORDER BY created_at", (task_id,)
            ).fetchall()
        return [Decision(**dict(r)) for r in rows]

    # ---------- events ----------

    def append_event(self, event: StoredEvent) -> StoredEvent:
        cur = self.conn.execute(
            "INSERT INTO events (task_id, agent_id, type, payload, ts) VALUES (?, ?, ?, ?, ?)",
            (event.task_id, event.agent_id, event.type, _json(event.payload), _iso(event.ts)),
        )
        self.conn.commit()
        lastrowid = cur.lastrowid
        if lastrowid is not None:
            event.seq = lastrowid
        return event

    def list_events(self, task_id: str | None = None, after_seq: int = 0) -> list[StoredEvent]:
        if task_id is None:
            rows = self.conn.execute(
                "SELECT * FROM events WHERE seq > ? ORDER BY seq", (after_seq,)
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM events WHERE seq > ? AND task_id = ? ORDER BY seq",
                (after_seq, task_id),
            ).fetchall()
        return [StoredEvent(**dict(r)) for r in rows]


def _json(value: dict[str, Any]) -> str:
    import json

    return json.dumps(value, ensure_ascii=False, default=str)
