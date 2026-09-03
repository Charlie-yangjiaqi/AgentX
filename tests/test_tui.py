"""P0 测试：TUI 两栏对话流工作台。"""

from __future__ import annotations

from pathlib import Path

import pytest

from agentx.state.models import Finding, FindingSeverity
from agentx.tui.app import AgentXApp


def _timeline_text(app: AgentXApp) -> str:
    """收集对话流所有卡片的完整文本。"""
    parts = []
    for widget in app.query("#timeline > Static"):
        parts.append(str(widget.render()))
    return "\n".join(parts)


def _display_text(app: AgentXApp) -> str:
    body = app.query_one("#display-body")
    return str(body.render())


@pytest.mark.asyncio
async def test_tui_two_panels_exist(tmp_path: Path) -> None:
    app = AgentXApp(tmp_path)
    async with app.run_test() as pilot:
        assert app.query("#conversation")
        assert app.query("#display")
        await pilot.pause()


@pytest.mark.asyncio
async def test_tui_start_task_via_input(tmp_path: Path) -> None:
    app = AgentXApp(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        input_widget = app.query_one("#input")
        input_widget.value = "实现一个测试功能"
        await pilot.press("enter")
        await pilot.pause(0.3)
        assert app.current_task_id is not None
        await pilot.pause()


@pytest.mark.asyncio
async def test_tui_display_shows_todo_and_findings(tmp_path: Path) -> None:
    app = AgentXApp(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        task = app.application.create_task("陈列区测试")
        app.current_task_id = task.id
        app.application.store.insert_finding(
            Finding(
                id="f1",
                task_id=task.id,
                severity=FindingSeverity.HIGH,
                category="回归",
                description="缺少回滚",
                location="param.c",
            )
        )
        app._refresh_display()
        text = _display_text(app)
        assert "TODO" in text
        assert "HIGH" in text
        assert "缺少回滚" in text
        await pilot.pause()


@pytest.mark.asyncio
async def test_tui_timeline_renders_message_and_finding(tmp_path: Path) -> None:
    app = AgentXApp(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        task = app.application.create_task("对话流测试")
        app.current_task_id = task.id
        app.application.store.insert_message(
            __import__("agentx.state.models", fromlist=["Message"]).Message(
                id="m1", task_id=task.id, sender="executor-1", content="实现完成"
            )
        )
        app.application.store.insert_message(
            __import__("agentx.state.models", fromlist=["Message"]).Message(
                id="m2",
                task_id=task.id,
                sender="reviewer-1",
                content=(
                    '{"findings": [{"severity": "HIGH", "category": "回归", '
                    '"location": "param.c", "description": "缺少回滚"}]}'
                ),
            )
        )
        app._refresh_timeline()
        text = _timeline_text(app)
        assert "Executor" in text
        assert "Reviewer" in text
        assert "HIGH" in text
        await pilot.pause()


@pytest.mark.asyncio
async def test_tui_tool_row_collapsed_by_default(tmp_path: Path) -> None:
    app = AgentXApp(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        task = app.application.create_task("工具行测试")
        app.current_task_id = task.id
        app.application.store.insert_message(
            __import__("agentx.state.models", fromlist=["Message"]).Message(
                id="m1", task_id=task.id, sender="executor-1", content="x"
            )
        )
        app.application.store.append_event(
            __import__("agentx.state.models", fromlist=["StoredEvent"]).StoredEvent(
                task_id=task.id,
                agent_id="executor-1",
                type="ToolStarted",
                payload={"tool": "shell.run", "args": {"command": "make clean"}},
            )
        )
        app.application.store.append_event(
            __import__("agentx.state.models", fromlist=["StoredEvent"]).StoredEvent(
                task_id=task.id,
                agent_id="executor-1",
                type="ToolFinished",
                payload={"tool": "shell.run", "ok": False, "exit_code": 2, "output": "错误输出"},
            )
        )
        app._refresh_timeline()
        text = _timeline_text(app)
        assert "▸" in text  # 默认折叠
        assert "make clean" in text
        assert "错误输出" not in text  # 输出不显示（折叠）
        # 展开后显示输出
        app._expanded_tools.add("ev:1")
        app._refresh_timeline()
        text2 = _timeline_text(app)
        assert "▾" in text2
        assert "错误输出" in text2
        await pilot.pause()


@pytest.mark.asyncio
async def test_tui_decision_card(tmp_path: Path) -> None:
    app = AgentXApp(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        task = app.application.create_task("决策测试")
        app.current_task_id = task.id
        app.application.store.insert_decision(
            __import__("agentx.state.models", fromlist=["Decision"]).Decision(
                id="d1", task_id=task.id, result="PASS", reason="build 通过 (exit=0)"
            )
        )
        app._refresh_timeline()
        text = _timeline_text(app)
        assert "AgentX" in text
        assert "PASS" in text
        await pilot.pause()
