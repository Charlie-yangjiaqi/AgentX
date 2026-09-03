"""进度回调：把 Plan/Review/Verify 的内部过程转成用户可见的进度事件。

订阅 Application 的 EventBus，把 Agent 思考/工具调用转成简短进度文本；
阶段级进度由各 Service 主动回调。
"""

from __future__ import annotations

from collections.abc import Callable

from agentx.core.event_bus import EventBus
from agentx.state.models import StoredEvent

ProgressFn = Callable[[str], None]


class ProgressReporter:
    """把 Agent 工具活动实时转成进度文本。"""

    def __init__(self, bus: EventBus, callback: ProgressFn) -> None:
        self._bus = bus
        self._callback = callback
        self._subscribed = False

    def start(self) -> None:
        if not self._subscribed:
            self._bus.subscribe(self._on_event)
            self._subscribed = True

    def close(self) -> None:
        if self._subscribed:
            self._bus.unsubscribe(self._on_event)
            self._subscribed = False

    def _on_event(self, event: StoredEvent) -> None:
        if event.type == "AgentThinking":
            self._callback("思考中...")
        elif event.type == "ToolStarted":
            tool = event.payload.get("tool", "")
            args = event.payload.get("args") or {}
            detail = ""
            if isinstance(args, dict) and args.get("path"):
                detail = f" {args['path']}"
            elif isinstance(args, dict) and args.get("command"):
                detail = f" {str(args['command'])[:40]}"
            self._callback(f"工具: {tool}{detail}")
        elif event.type == "ToolFinished":
            tool = event.payload.get("tool", "")
            ok = event.payload.get("ok")
            self._callback(f"完成: {tool} {'[OK]' if ok else '[FAIL]'}")
        elif event.type == "AgentStarted":
            self._callback(f"{event.agent_id or ''} 开始工作")
