"""Orchestrator：状态机驱动的多 Agent 调度核心。

闭环：EXECUTING → REVIEWING → VERIFYING → DECIDING
      PASS → COMPLETED；FAIL → REPAIRING → EXECUTING（迭代+1）
任何阶段可因高风险操作进入 WAITING_USER，等待人工审批。

状态机必须由 Orchestrator 控制：Agent 只提交结果，不允许跳转状态。
"""

from __future__ import annotations

import platform
import uuid
from pathlib import Path
from typing import Any

from agentx.agents.definitions import AgentDefinition
from agentx.agents.runtime import AgentResult, AgentRuntime
from agentx.core.control import TaskControl
from agentx.core.decision import DecisionEngine, Verdict, parse_verdict
from agentx.core.event_bus import EventBus, make_event
from agentx.providers.messages import ChatMessage
from agentx.state.models import (
    EVENT_FINDING_CREATED,
    EVENT_TASK_STATE_CHANGED,
    Change,
    Decision,
    Finding,
    Message,
    MessageScope,
    StoredEvent,
    Task,
    TaskState,
)
from agentx.state.store import SQLiteStore
from agentx.tools.base import ToolContext
from agentx.tools.registry import ToolRegistry


class Orchestrator:
    def __init__(
        self,
        *,
        store: SQLiteStore,
        registry: ToolRegistry,
        decision_engine: DecisionEngine,
        agents: dict[str, AgentRuntime],
        event_bus: EventBus,
        control: TaskControl | None = None,
    ) -> None:
        self.store = store
        self.registry = registry
        self.decision_engine = decision_engine
        self.agents = agents
        self.event_bus = event_bus
        self.control = control or TaskControl()
        self._verdicts: dict[str, Verdict | None] = {}
        # 所有事件落库：SQLite 订阅 Event Bus，而不是被显式调用
        event_bus.subscribe(self._persist_event)

    def _persist_event(self, event: StoredEvent) -> None:
        self.store.append_event(event)

    # ---------- 任务入口 ----------

    async def run_task(self, task_id: str) -> Task:
        """执行任务直到终止状态或需要用户介入。"""
        task = self.store.get_task(task_id)
        if task is None:
            raise KeyError(f"任务不存在: {task_id}")
        if task.state == TaskState.CREATED:
            task = await self._transition(task, TaskState.EXECUTING)
        return await self._run_loop(task)

    async def resume(self, task_id: str) -> Task:
        """从 WAITING_USER 恢复：回到 EXECUTING 并继续闭环。"""
        task = self.store.get_task(task_id)
        if task is None:
            raise KeyError(f"任务不存在: {task_id}")
        if task.state == TaskState.WAITING_USER:
            task = await self._transition(task, TaskState.EXECUTING)
        return await self._run_loop(task)

    async def _run_loop(self, task: Task) -> Task:
        while not task.state.is_terminal and task.state != TaskState.WAITING_USER:
            if self.control.is_cancelled(task.id):
                return await self._transition(task, TaskState.CANCELLED)
            phase = task.state
            if phase == TaskState.EXECUTING:
                task = await self._phase_execute(task)
            elif phase == TaskState.REVIEWING:
                task = await self._phase_review(task)
            elif phase == TaskState.VERIFYING:
                task = await self._phase_verify(task)
            elif phase == TaskState.DECIDING:
                task = await self._phase_decide(task)
            elif phase == TaskState.REPAIRING:
                task = await self._transition(
                    task, TaskState.EXECUTING, iteration=task.iteration + 1
                )
            else:
                raise RuntimeError(f"无法处理的阶段: {phase}")
        return task

    # ---------- 阶段 ----------

    async def _phase_execute(self, task: Task) -> Task:
        runtime = self.agents["executor"]
        messages = [
            ChatMessage(role="user", content=f"任务目标：{task.goal}"),
            ChatMessage(role="user", content=_env_hint()),
        ]

        open_findings = [f for f in self.store.list_findings(task.id) if f.status.value == "OPEN"]
        if open_findings:
            desc = "\n".join(
                f"- [{f.severity.value}] {f.location or ''}: {f.description}" for f in open_findings
            )
            messages.append(ChatMessage(role="user", content=f"需要修复上一轮发现的问题：\n{desc}"))

        result = await runtime.run(messages, self._ctx(task), task_id=task.id)
        await self._handle_result(task, result, runtime.definition)

        if result.requires_approval:
            return await self._transition(task, TaskState.WAITING_USER)

        await self._store_changes(task, result)
        return await self._transition(task, TaskState.REVIEWING)

    async def _phase_review(self, task: Task) -> Task:
        runtime = self.agents["reviewer"]
        # 新一轮审查开始：上一轮的 OPEN Finding 视为已被本轮实现处理
        for old in self.store.list_findings(task.id):
            if old.status.value == "OPEN":
                self.store.update_finding_status(old.id, "RESOLVED")
        last_executor = self._last_agent_message(task.id, "executor")
        messages = [
            ChatMessage(role="user", content=f"任务目标：{task.goal}"),
            ChatMessage(role="user", content=f"Executor 的完成说明：\n{last_executor or '(无)'}"),
        ]
        result = await runtime.run(messages, self._ctx(task), task_id=task.id)
        await self._handle_result(task, result, runtime.definition)

        findings = _parse_findings(result.content or "")
        for f in findings:
            finding = Finding(
                id=uuid.uuid4().hex,
                task_id=task.id,
                severity=f["severity"],
                category=f["category"],
                location=f.get("location"),
                description=f["description"],
            )
            self.store.insert_finding(finding)
            await self.event_bus.publish(
                make_event(
                    EVENT_FINDING_CREATED,
                    task_id=task.id,
                    payload={"finding_id": finding.id},
                )
            )
        return await self._transition(task, TaskState.VERIFYING)

    async def _phase_verify(self, task: Task) -> Task:
        runtime = self.agents["verifier"]
        findings = self.store.list_findings(task.id)
        findings_text = (
            "\n".join(f"- [{f.severity.value}] {f.description}" for f in findings) or "(无)"
        )
        messages = [
            ChatMessage(role="user", content=f"任务目标：{task.goal}"),
            ChatMessage(role="user", content=_env_hint()),
            ChatMessage(role="user", content=f"Reviewer 的 Finding：\n{findings_text}"),
        ]
        result = await runtime.run(messages, self._ctx(task), task_id=task.id)
        await self._handle_result(task, result, runtime.definition)

        for ev in result.evidence:
            self.store.insert_evidence(ev)

        verdict = parse_verdict(result.content or "")
        if verdict is not None:
            self._verdicts[task.id] = verdict
        return await self._transition(task, TaskState.DECIDING)

    async def _phase_decide(self, task: Task) -> Task:
        findings = self.store.list_findings(task.id)
        evidence = self.store.list_evidence(task.id)
        verdict = self._verdicts.get(task.id)
        decision = self.decision_engine.decide(
            findings=findings,
            evidence=evidence,
            verdict=verdict,
            iteration=task.iteration,
        )
        self.store.insert_decision(
            Decision(
                id=uuid.uuid4().hex,
                task_id=task.id,
                rule="no_blocker_findings + build_passed + tests_passed",
                result=decision.outcome,
                reason="; ".join(decision.reasons),
            )
        )
        if decision.is_pass:
            return await self._transition(task, TaskState.COMPLETED)
        if task.iteration >= self.decision_engine.rules.max_iterations:
            return await self._transition(task, TaskState.FAILED)
        return await self._transition(task, TaskState.REPAIRING)

    # ---------- 辅助 ----------

    def _ctx(self, task: Task) -> ToolContext:
        project = self.store.get_project(task.project_id)
        if project is None:
            raise RuntimeError(f"项目不存在: {task.project_id}")
        return ToolContext(project_root=Path(project.root_path))

    async def _handle_result(
        self, task: Task, result: AgentResult, definition: AgentDefinition
    ) -> None:
        """记录 Agent 输出为 Message，并更新 Agent 状态。"""
        self.store.insert_message(
            Message(
                id=uuid.uuid4().hex,
                task_id=task.id,
                sender=definition.id,
                scope=MessageScope.TASK,
                content=result.content or "",
            )
        )
        self.store.update_agent_status(
            definition.id, "working" if not result.interrupted else "idle"
        )

    def _last_agent_message(self, task_id: str, agent_id: str) -> str | None:
        for m in reversed(self.store.list_messages(task_id)):
            if m.sender == agent_id:
                return m.content
        return None

    async def _store_changes(self, task: Task, result: AgentResult) -> None:
        for r in result.tool_results:
            path = r.args.get("path")
            if not r.ok or not path:
                continue
            operation = "APPEND" if r.args.get("append") else "WRITE"
            self.store.insert_change(
                Change(id=uuid.uuid4().hex, task_id=task.id, file=str(path), operation=operation)
            )

    async def _transition(
        self, task: Task, state: TaskState, *, iteration: int | None = None
    ) -> Task:
        # 终态保护：任务已终止/取消后，不允许被阶段结果覆盖
        current = self.store.get_task(task.id)
        if current is None:
            raise RuntimeError(f"任务不存在: {task.id}")
        if current.state.is_terminal or current.state == TaskState.CANCELLED:
            return current
        updated = self.store.update_task_state(task.id, state, iteration=iteration)
        if updated is None:
            raise RuntimeError(f"任务状态更新失败: {task.id}")
        await self.event_bus.publish(
            make_event(
                EVENT_TASK_STATE_CHANGED,
                task_id=task.id,
                payload={
                    "from": task.state.value,
                    "to": state.value,
                    "iteration": updated.iteration,
                },
            )
        )
        return updated


def _env_hint() -> str:
    """把运行时环境告诉 Agent，避免它执着于不存在的东西（如 Windows 的 make）。"""
    if platform.system() == "Windows":
        return (
            "环境说明：Windows + MinGW 环境。"
            "没有 make 命令（make 不在 PATH），但有 gcc / cc 和 mingw32-make；"
            "Makefile 里的 rm、./main 等 POSIX 命令不可用，"
            "请用 cmd 语法（del、dir）或直接调用 gcc 编译、运行 .exe。"
            "验证时请以实际可运行的命令为准。"
        )
    return "环境说明：类 Unix 环境，make / gcc 通常可用。"


def _parse_findings(content: str) -> list[dict[str, Any]]:
    """解析 Reviewer 的 JSON findings 输出（容错：取第一个 JSON 对象）。"""
    import json

    start = content.find("{")
    end = content.rfind("}")
    if start == -1 or end <= start:
        return []
    try:
        data = json.loads(content[start : end + 1])
    except json.JSONDecodeError:
        return []
    findings = data.get("findings") if isinstance(data, dict) else None
    if not isinstance(findings, list):
        return []
    return [f for f in findings if isinstance(f, dict) and f.get("description")]
