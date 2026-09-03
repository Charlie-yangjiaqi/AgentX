"""MCP 后台任务（Job）管理：长任务不依赖单次同步 RPC（Phase 7.9.2 体验）。

问题：大型项目首次 Index 构建可能超过 MCP 客户端 300s RPC 超时。
方案：build 类 action（auto/sync/understand/plan）在 Index=MISSING 时
提交后台任务，立即返回 {status:"running", job_id, phase}；调用方通过
action=status + job_id 轮询最终状态。

Job 状态机：
- running：后台执行中
- scope_required：挂起等待 Scope 确认（无配置 + 有建议 + 未确认）
- completed：成功（result 附带最终响应）
- failed：失败（error 附带原因）

resume：调用方带 job_id + scope_selections 再次调用同 action →
用记录的原始参数 + scope_selections 重跑任务（scope 确认后幂等完成）。
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

JobFn = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]

_ACTIVE_TIMEOUT_SECONDS = 60 * 60  # 任务最长 1 小时


@dataclass
class Job:
    id: str
    action: str
    project_path: str
    status: str = "running"  # running | scope_required | completed | failed
    phase: str = "building_index"
    message: str = ""
    progress: int = 0
    result: dict[str, Any] | None = None
    error: str | None = None
    created_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    task: asyncio.Task[None] | None = None
    # resume 需要：原始调用参数
    params: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """对外状态视图（可序列化）。"""
        out: dict[str, Any] = {
            "job_id": self.id,
            "action": self.action,
            "status": self.status,
            "phase": self.phase,
            "message": self.message,
            "progress": self.progress,
            "created_at": self.created_at,
        }
        if self.status == "completed" and self.result is not None:
            out["result"] = self.result
        if self.status == "failed":
            out["error"] = self.error
        if self.status == "scope_required":
            out["suggestions"] = (self.result or {}).get("suggestions", {})
        return out


class JobManager:
    """进程内任务表：submit（后台执行）+ get（状态查询）+ resume（确认续跑）。"""

    def __init__(self) -> None:
        self._jobs: dict[str, Job] = {}

    def submit(
        self,
        action: str,
        project_path: str,
        params: dict[str, Any],
        runner: JobFn,
    ) -> Job:
        """提交后台任务；runner 是异步执行函数（内部实现 action 语义）。"""
        job = Job(
            id=uuid.uuid4().hex[:12],
            action=action,
            project_path=project_path,
            params=dict(params),
        )
        job.task = asyncio.create_task(self._run(job, runner))
        self._jobs[job.id] = job
        return job

    def get(self, job_id: str) -> Job | None:
        return self._jobs.get(job_id)

    def resume(
        self,
        job_id: str,
        runner: JobFn,
        scope_selections: dict[str, Any] | None = None,
    ) -> Job | None:
        """scope 确认后续跑：用原始参数 + scope_selections 重跑任务。

        仅允许 scope_required / failed 状态续跑（幂等：确认后重跑会完成）。
        """
        job = self.get(job_id)
        if job is None or job.status not in ("scope_required", "failed"):
            return None
        params = dict(job.params)
        if scope_selections is not None:
            params["scope_selections"] = scope_selections
        job.status = "running"
        job.phase = "building_index"
        job.message = "resumed after scope confirmation"
        job.result = None
        job.error = None
        job.task = asyncio.create_task(self._run(job, runner, params=params))
        return job

    async def _run(
        self,
        job: Job,
        runner: JobFn,
        params: dict[str, Any] | None = None,
    ) -> None:
        try:
            result = await asyncio.wait_for(
                runner(params if params is not None else job.params),
                timeout=_ACTIVE_TIMEOUT_SECONDS,
            )
        except TimeoutError:
            job.status = "failed"
            job.phase = "timeout"
            job.error = f"任务超时（>{_ACTIVE_TIMEOUT_SECONDS}s）"
            return
        except Exception as e:
            job.status = "failed"
            job.phase = "error"
            job.error = f"{type(e).__name__}: {e}"
            return
        # scope_required 识别：runner 返回 _wrap 结构（result 内层）或裸结构
        inner = result.get("result", result) if isinstance(result, dict) else result
        if isinstance(inner, dict) and (
            inner.get("status") == "scope_required" or inner.get("action") == "scope_required"
        ):
            # 挂起等确认（不失败、不假成功）；sync 返回 action=scope_required
            job.status = "scope_required"
            job.phase = "waiting_scope_confirmation"
            job.message = str(inner.get("message", "Need user confirmation before index build"))
            job.result = result
            return
        job.status = "completed"
        job.phase = "completed"
        job.progress = 100
        job.result = result

    def cleanup_stale(self) -> int:
        """清理超龄已完成任务（防内存增长）；返回清理数。"""
        cutoff = datetime.now(UTC).timestamp() - _ACTIVE_TIMEOUT_SECONDS
        stale: list[str] = []
        for jid, job in self._jobs.items():
            if job.status in ("completed", "failed") and job.task is not None and job.task.done():
                try:
                    created = datetime.fromisoformat(job.created_at).timestamp()
                except ValueError:
                    created = 0
                if created < cutoff:
                    stale.append(jid)
        for jid in stale:
            self._jobs.pop(jid, None)
        return len(stale)


_job_manager = JobManager()


def job_manager() -> JobManager:
    return _job_manager
