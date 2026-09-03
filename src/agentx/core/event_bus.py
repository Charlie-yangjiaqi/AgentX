"""进程内 Event Bus：CLI / TUI / Logger / SQLite 订阅同一套事件。

V1 不上消息队列。事件是结构化对象（带 task_id / agent_id / ts），
未来映射到 WebSocket / NATS 只需换传输层。
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from agentx.state.models import StoredEvent

EventHandler = Callable[[StoredEvent], Awaitable[None] | None]


class EventBus:
    def __init__(self) -> None:
        self._subscribers: dict[str, list[EventHandler]] = {}
        self._all_subscribers: list[EventHandler] = []

    def subscribe(self, handler: EventHandler, event_type: str | None = None) -> None:
        """订阅：event_type=None 表示订阅所有事件。"""
        if event_type is None:
            self._all_subscribers.append(handler)
        else:
            self._subscribers.setdefault(event_type, []).append(handler)

    def unsubscribe(self, handler: EventHandler, event_type: str | None = None) -> None:
        """取消订阅。"""
        if event_type is None:
            if handler in self._all_subscribers:
                self._all_subscribers.remove(handler)
        else:
            handlers = self._subscribers.get(event_type)
            if handlers and handler in handlers:
                handlers.remove(handler)

    async def publish(self, event: StoredEvent) -> None:
        """发布事件：同步派发给所有订阅者。"""
        handlers = list(self._all_subscribers)
        handlers.extend(self._subscribers.get(event.type, []))
        if not handlers:
            return
        awaitables = [h(event) for h in handlers]
        await asyncio.gather(
            *(r for r in awaitables if isinstance(r, Awaitable)),
            return_exceptions=True,
        )

    async def publish_sync(self, event: StoredEvent) -> None:
        """同步发布：串行等待每个订阅者处理完（用于必须按序的场景）。"""
        for h in list(self._all_subscribers) + self._subscribers.get(event.type, []):
            result = h(event)
            if isinstance(result, Awaitable):
                await result

    def subscriber_count(self, event_type: str) -> int:
        return len(self._all_subscribers) + len(self._subscribers.get(event_type, []))


def make_event(
    event_type: str,
    *,
    task_id: str | None = None,
    agent_id: str | None = None,
    payload: dict[str, Any] | None = None,
) -> StoredEvent:
    return StoredEvent(
        type=event_type,
        task_id=task_id,
        agent_id=agent_id,
        payload=payload or {},
    )
