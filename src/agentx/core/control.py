"""任务取消令牌：让运行中的任务能响应 stop/取消。

Orchestrator 与 AgentRuntime 在每步循环中检查令牌，
而不是等一个阶段自然结束。
"""

from __future__ import annotations


class TaskControl:
    def __init__(self) -> None:
        self._cancelled: set[str] = set()

    def cancel(self, task_id: str) -> None:
        self._cancelled.add(task_id)

    def clear(self, task_id: str) -> None:
        self._cancelled.discard(task_id)

    def is_cancelled(self, task_id: str) -> bool:
        return task_id in self._cancelled
