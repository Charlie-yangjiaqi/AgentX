"""结构化 Workflow 事件 + 实时流（Phase 6.6）。

设计（两条通道，互不污染）：
- EventCollector.emit(stage, status, message)
      |---> _events[]（持久，进最终 events[]，Phase 6.5 兼容）
      |---> _subscribers[]（实时回调，供 MCP notification / CLI 展示）
- Heartbeat（观察层）：只发 running+elapsed 到独立 sink，不进主事件列表。

原则：
- listener 异常不影响业务（try/except 隔离）
- 业务代码（Service）不感知 stream，仍只 emit
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import Callable

# 统一 Stage 定义（Phase 6.5）
STAGES = (
    "index_check",
    "index_decision",
    "index_sync",
    "codegraph_analysis",
    "index_quality",
    "query_context",
    "understanding",
    "decision_gate",
    "planning",
    "evidence_validation",
    "review",
    "verify",
    "completed",
)

# 事件生命周期（Phase 6.6：四态）
STATUS_PENDING = "pending"
STATUS_RUNNING = "running"
STATUS_COMPLETED = "completed"
STATUS_FAILED = "failed"
STATUSES = (STATUS_PENDING, STATUS_RUNNING, STATUS_COMPLETED, STATUS_FAILED)

# Service 层可选回调：on_event(stage, status, message)
EventFn = Callable[[str, str, str], None]
# listener 回调：收到完整事件 dict
ListenerFn = Callable[[dict[str, str]], None]

DEFAULT_HEARTBEAT_INTERVAL = 15.0


class WorkflowEvent(dict[str, str]):
    """单条 workflow 事件（dict 子类，便于 JSON 序列化）。"""

    def __init__(self, stage: str, status: str, message: str = "") -> None:
        if stage not in STAGES:
            raise ValueError(f"未知 stage: {stage}（支持: {', '.join(STAGES)}）")
        if status not in STATUSES:
            raise ValueError(f"未知 status: {status}（支持: {', '.join(STATUSES)}）")
        super().__init__(stage=stage, status=status)
        if message:
            self["message"] = message


class EventCollector:
    """收集 Service 发出的 (stage, status, message) 事件。

    - 无 listener 时行为与 Phase 6.5 完全一致
    - listener 异常不影响业务（观察层隔离）
    - emit 保持 append（持久）+ 实时回调（流）
    """

    def __init__(self) -> None:
        self._events: list[WorkflowEvent] = []
        self._subscribers: list[ListenerFn] = []

    def subscribe(self, callback: ListenerFn) -> None:
        """注册实时 listener：每次 emit 都会收到事件 dict 副本。"""
        if callback not in self._subscribers:
            self._subscribers.append(callback)

    def unsubscribe(self, callback: ListenerFn) -> None:
        if callback in self._subscribers:
            self._subscribers.remove(callback)

    def emit(self, stage: str, status: str, message: str = "") -> None:
        event = WorkflowEvent(stage, status, message)
        self._events.append(event)
        snapshot = dict(event)
        for callback in list(self._subscribers):
            with contextlib.suppress(Exception):
                # 观察层异常（stream 断/回调 bug）绝不影响业务
                callback(snapshot)

    def events(self) -> list[dict[str, str]]:
        return [dict(e) for e in self._events]

    def clear(self) -> None:
        self._events.clear()

    # 注意：不定义 __len__/__bool__，保证空 collector 在 bool() 下为 True
    # （否则 `events.emit if events else None` 会把空收集器误判为 None）


class Heartbeat:
    """观察层心跳：长阶段无事件时，周期发送 running+elapsed 到独立 sink。

    - 只跟踪"最近一个 running 事件"的 stage
    - stage 换档（新 running）自动重置计时；completed/failed 自动停止
    - 心跳只发 sink（MCP notification / CLI），不进 EventCollector._events
      （避免 15s/30s/45s... 心跳污染最终 events[]）
    - sink 异常不影响业务
    """

    def __init__(
        self,
        collector: EventCollector,
        on_beat: Callable[[dict[str, object]], None] | None = None,
        interval: float = DEFAULT_HEARTBEAT_INTERVAL,
    ) -> None:
        self._collector = collector
        self._on_beat = on_beat
        self._interval = interval
        self._stage: str | None = None
        self._started = 0.0
        self._task: asyncio.Task[None] | None = None
        collector.subscribe(self._on_event)

    def _on_event(self, event: dict[str, str]) -> None:
        status = event.get("status", "")
        if status == STATUS_RUNNING:
            self._stage = event.get("stage")
            self._started = time.monotonic()
        elif status in (STATUS_COMPLETED, STATUS_FAILED):
            if event.get("stage") == self._stage:
                self._stage = None

    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(self._interval)
            if self._stage is None or self._on_beat is None:
                continue
            beat: dict[str, object] = {
                "stage": self._stage,
                "status": STATUS_RUNNING,
                "message": "still running",
                "elapsed": int(time.monotonic() - self._started),
            }
            with contextlib.suppress(Exception):
                self._on_beat(beat)

    def start(self) -> asyncio.Task[None]:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._loop())
        return self._task

    def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            self._task = None

    @property
    def active_stage(self) -> str | None:
        return self._stage
