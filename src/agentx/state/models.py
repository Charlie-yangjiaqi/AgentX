"""领域模型：Task / Agent / Message / Change / Finding / Evidence / Decision / Event。

原则：Claim ≠ Evidence。Agent 的自然语言声明不进入 State 事实层，
只有结构化记录（Finding、Evidence、Decision）才是事实。
"""

from __future__ import annotations

import enum
import json
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator


def utcnow() -> datetime:
    """统一时间源，保证所有时间戳可比较、可回放。"""
    return datetime.now(UTC)


class AgentXModel(BaseModel):
    """基类：赋值时校验（保证 store 里直接改字段也是类型安全的）。"""

    model_config = ConfigDict(validate_assignment=True)


class TaskState(enum.StrEnum):
    """Task 状态机，由 Orchestrator 控制，Agent 不允许自行跳转。"""

    CREATED = "CREATED"
    PLANNING = "PLANNING"
    EXECUTING = "EXECUTING"
    REVIEWING = "REVIEWING"
    VERIFYING = "VERIFYING"
    DECIDING = "DECIDING"
    REPAIRING = "REPAIRING"
    WAITING_USER = "WAITING_USER"
    PAUSED = "PAUSED"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"

    @property
    def is_terminal(self) -> bool:
        return self in {TaskState.COMPLETED, TaskState.FAILED, TaskState.CANCELLED}


class AgentRole(enum.StrEnum):
    PLAN = "plan"
    EXECUTOR = "executor"
    REVIEWER = "reviewer"
    VERIFIER = "verifier"


class AgentStatus(enum.StrEnum):
    IDLE = "idle"
    WORKING = "working"
    WAITING = "waiting"


class FindingSeverity(enum.StrEnum):
    INFO = "INFO"
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    BLOCKER = "BLOCKER"


class FindingStatus(enum.StrEnum):
    OPEN = "OPEN"
    RESOLVED = "RESOLVED"
    WONT_FIX = "WONT_FIX"


class MessageScope(enum.StrEnum):
    """消息作用域：决定消息进入 Global Context 还是 Agent 私有上下文。"""

    GLOBAL = "GLOBAL"
    TASK = "TASK"
    AGENT_PRIVATE = "AGENT_PRIVATE"
    MEETING = "MEETING"


class Project(AgentXModel):
    """项目身份与工作目录。"""

    id: str
    root_path: str
    branch: str | None = None
    created_at: datetime = Field(default_factory=utcnow)


class Task(AgentXModel):
    """任务生命周期记录。"""

    id: str
    project_id: str
    goal: str
    state: TaskState = TaskState.CREATED
    iteration: int = 0
    max_iterations: int = 5
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)


class AgentSession(AgentXModel):
    """Agent 运行实例（角色 ≠ 模型，角色与模型解耦）。"""

    id: str
    role: AgentRole
    provider_ref: str = "default"
    model: str | None = None
    status: AgentStatus = AgentStatus.IDLE
    created_at: datetime = Field(default_factory=utcnow)


class Message(AgentXModel):
    """人与 Agent 的协作消息，带 scope。"""

    id: str
    task_id: str | None = None
    sender: str
    target: str | None = None
    scope: MessageScope = MessageScope.TASK
    content: str
    created_at: datetime = Field(default_factory=utcnow)


class Change(AgentXModel):
    """修改记录。"""

    id: str
    task_id: str
    file: str
    operation: str
    diff_hash: str | None = None
    timestamp: datetime = Field(default_factory=utcnow)


class Finding(AgentXModel):
    """审查/核验发现的问题。"""

    id: str
    task_id: str
    severity: FindingSeverity
    category: str
    location: str | None = None
    description: str
    status: FindingStatus = FindingStatus.OPEN
    created_at: datetime = Field(default_factory=utcnow)


class Evidence(AgentXModel):
    """可验证证据：来自真实 Tool 执行结果，而非 Agent 声明。"""

    id: str
    task_id: str | None = None
    type: str
    command: str | None = None
    result: str | None = None
    exit_code: int | None = None
    timestamp: datetime = Field(default_factory=utcnow)


class Decision(AgentXModel):
    """最终决策记录：由 Decision Engine 产生。"""

    id: str
    task_id: str
    rule: str | None = None
    result: str
    reason: str | None = None
    created_at: datetime = Field(default_factory=utcnow)


class StoredEvent(AgentXModel):
    """事件总线上的结构化事件，持久化用于回放与恢复。"""

    seq: int | None = None
    task_id: str | None = None
    agent_id: str | None = None
    type: str
    payload: dict[str, Any] = Field(default_factory=dict)
    ts: datetime = Field(default_factory=utcnow)

    @field_validator("payload", mode="before")
    @classmethod
    def _parse_payload(cls, value: Any) -> Any:
        if isinstance(value, str):
            return json.loads(value)
        return value


# 事件类型常量（与 Event Bus 共享，防止魔法字符串）
EVENT_AGENT_STARTED = "AgentStarted"
EVENT_AGENT_THINKING = "AgentThinking"
EVENT_AGENT_MESSAGE = "AgentMessage"
EVENT_TOOL_STARTED = "ToolStarted"
EVENT_TOOL_FINISHED = "ToolFinished"
EVENT_FILE_CHANGED = "FileChanged"
EVENT_FINDING_CREATED = "FindingCreated"
EVENT_TEST_STARTED = "TestStarted"
EVENT_TEST_FINISHED = "TestFinished"
EVENT_USER_INPUT_REQUIRED = "UserInputRequired"
EVENT_TASK_STATE_CHANGED = "TaskStateChanged"
