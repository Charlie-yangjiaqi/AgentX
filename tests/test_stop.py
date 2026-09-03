"""stop 修复测试：取消令牌让运行中的任务立即停止。"""

from __future__ import annotations

from pathlib import Path

from agentx.agents.definitions import AgentDefinition
from agentx.agents.prompts import ROLE_PROMPTS
from agentx.agents.runtime import AgentRuntime
from agentx.app.application import Application
from agentx.core.control import TaskControl
from agentx.providers.messages import ChatMessage, ModelResponse, ToolCall
from agentx.providers.mock import MockProvider
from agentx.state.models import AgentRole, Project, Task, TaskState
from agentx.tools import build_default_registry
from agentx.tools.base import ToolContext


async def test_runtime_stops_when_control_flagged() -> None:
    """stop_check 触发后，Agent 循环立即中断，不再调用 provider。"""
    calls = {"n": 0}

    def forever(messages: list[ChatMessage]) -> ModelResponse:
        calls["n"] += 1
        return ModelResponse(
            tool_calls=[ToolCall(id="c1", name="fs.read_file", arguments={"path": "x"})]
        )

    control = TaskControl()
    defn = AgentDefinition.from_role("ex-1", AgentRole.EXECUTOR, ROLE_PROMPTS["executor"])
    runtime = AgentRuntime(
        defn,
        MockProvider().with_handler(forever),
        build_default_registry(),
        stop_check=lambda: control.is_cancelled("t1"),
    )
    control.cancel("t1")
    ctx = ToolContext(project_root=Path("."))
    result = await runtime.run([ChatMessage(role="user", content="go")], ctx)
    assert result.interrupted
    assert calls["n"] == 0  # 第一步就因取消而终止，没有额外调用


async def test_runtime_stops_mid_loop() -> None:
    """运行若干步后触发取消，后续步骤不再执行。"""
    calls = {"n": 0}
    control = TaskControl()

    def forever(messages: list[ChatMessage]) -> ModelResponse:
        calls["n"] += 1
        if calls["n"] == 2:
            control.cancel("t1")
        return ModelResponse(
            tool_calls=[ToolCall(id="c1", name="fs.read_file", arguments={"path": "x"})]
        )

    defn = AgentDefinition.from_role("ex-1", AgentRole.EXECUTOR, ROLE_PROMPTS["executor"])
    runtime = AgentRuntime(
        defn,
        MockProvider().with_handler(forever),
        build_default_registry(),
        stop_check=lambda: control.is_cancelled("t1"),
    )
    ctx = ToolContext(project_root=Path("."))
    result = await runtime.run([ChatMessage(role="user", content="go")], ctx)
    assert result.interrupted
    assert calls["n"] == 2


async def test_application_cancel_stops_running_task(tmp_path: Path) -> None:
    """应用层：run 期间调用 cancel，任务最终状态为 CANCELLED 且不会被阶段覆盖。"""
    import asyncio

    app = Application(tmp_path)

    # 慢 provider：保证 cancel 发生在任务运行中
    async def slow(messages: list[ChatMessage]) -> ModelResponse:
        await asyncio.sleep(0.3)
        return ModelResponse(content="完成")

    app.provider = MockProvider().with_handler(slow)
    for runtime in app.orchestrator.agents.values():
        runtime.provider = app.provider

    task = app.create_task("测试取消")
    task_id = task.id

    async def _run_and_cancel() -> None:
        await asyncio.sleep(0.5)
        app.cancel(task_id)

    await asyncio.gather(app.run(task_id), _run_and_cancel())
    final = app.store.get_task(task_id)
    assert final is not None
    assert final.state == TaskState.CANCELLED


async def test_orchestrator_transition_never_overwrites_cancelled(
    tmp_path: Path,
) -> None:
    """终态保护：任务已取消后，阶段结果不允许把状态改回进行中。"""
    app = Application(tmp_path)
    app.store.insert_project(Project(id="p1", root_path=str(tmp_path)))
    app.store.insert_task(Task(id="t1", project_id="p1", goal="x"))
    app.store.update_task_state("t1", TaskState.CANCELLED)
    app.control.cancel("t1")

    cancelled_task = app.store.get_task("t1")
    assert cancelled_task is not None
    final = await app.orchestrator._transition(cancelled_task, TaskState.REVIEWING)
    assert final.state == TaskState.CANCELLED
