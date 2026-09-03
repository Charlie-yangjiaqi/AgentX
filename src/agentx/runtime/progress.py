"""ProgressAdapter：workflow 事件 → MCP notifications/progress（Reasonix 主展示通道）。

Phase 6.7：Reasonix 的 MCP client 只消费 `notifications/progress`，
`notifications/message`（logging）会被静默丢弃。Adapter 把同一事件流
转成 progress notification，stage 编号映射：

    index_check 1/10 ... completed 10/10

设计：
- 不改变 EventCollector 模型（事件仍只进 events[] + subscribers）
- Adapter 是观察层组件：业务代码不感知
- 心跳：progress 保持 stage 编号不变，只更新 message
- 无 progressToken：MCP 层不创建 Adapter（降级，message 通道照旧）
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any

# stage → progress 编号（1-10，与 STAGES 顺序一致）
STAGE_ORDER = (
    "index_check",
    "index_decision",
    "index_sync",
    "codegraph_analysis",
    "query_context",
    "understanding",
    "planning",
    "review",
    "verify",
    "completed",
)
TOTAL_PROGRESS = len(STAGE_ORDER)
STAGE_PROGRESS: dict[str, int] = {s: i + 1 for i, s in enumerate(STAGE_ORDER)}


class ProgressAdapter:
    """把 workflow 事件/心跳转成 MCP progress notification。

    session：鸭子类型，需提供 async send_progress_notification(token, progress, total, message)
    token：请求 _meta.progressToken（Reasonix 自动携带）
    pending：可选任务列表，结束时 gather 兜底送达
    """

    def __init__(
        self,
        session: Any,
        token: str | int,
        pending: list[asyncio.Task[None]] | None = None,
        total: int = TOTAL_PROGRESS,
    ) -> None:
        self._session = session
        self._token = token
        self._pending = pending
        self._total = total

    def _send(self, progress: int, message: str) -> None:
        # 输出边界清洗（Phase 7.9.1）：progress message 同受非法编码保护
        from agentx.mcp.sanitize import sanitize_str

        safe_message = sanitize_str(message)

        async def _notify() -> None:
            with contextlib.suppress(Exception):
                await self._session.send_progress_notification(
                    self._token, progress, self._total, safe_message
                )

        try:
            task = asyncio.create_task(_notify())
            if self._pending is not None:
                self._pending.append(task)
        except Exception:
            pass

    def on_event(self, event: dict[str, str]) -> None:
        """workflow 事件 → progress（progress = stage 编号）。"""
        stage = event.get("stage", "")
        num = STAGE_PROGRESS.get(stage)
        if num is None:
            return
        msg = event.get("message") or event.get("status", "")
        self._send(num, f"[{stage}] {msg}")

    def on_beat(self, beat: dict[str, object]) -> None:
        """心跳 → progress：stage 编号不变，只更新 message。"""
        stage = str(beat.get("stage", "") or "")
        num = STAGE_PROGRESS.get(stage)
        if num is None:
            return
        raw = beat.get("elapsed", 0)
        elapsed = int(raw) if isinstance(raw, (int, float)) else 0
        self._send(num, f"[{stage}] still running ({elapsed}s)")
