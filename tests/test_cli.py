"""P6 测试：CLI 命令（init / run / status / logs / resume / stop）。"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from agentx.cli.app import app
from agentx.state.models import Project, TaskState
from agentx.state.store import SQLiteStore

runner = CliRunner()


@pytest.fixture(autouse=True)
def _isolated_config(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """隔离真实 ~/.agentx 配置：无 api_key → mock provider（测试不依赖外部 LLM API）。"""
    monkeypatch.setattr(
        "agentx.config.config.default_config_path",
        lambda: tmp_path / "agentx_test_config.json",
    )


def _run(tmp_path: Path, *args: str) -> str:
    result = runner.invoke(app, [*args, "--path", str(tmp_path)])
    assert result.exit_code == 0, result.output
    return result.output


def test_cli_init_creates_workspace(tmp_path: Path) -> None:
    output = _run(tmp_path, "init")
    assert "已初始化" in output
    assert (tmp_path / ".agentx" / "agentx.db").exists()


def test_cli_run_with_mock_provider(tmp_path: Path) -> None:
    """mock provider 会警告并直接完成（无真实模型时）。"""
    output = _run(tmp_path, "run", "实现一个功能")
    assert "Task #" in output
    db = SQLiteStore(tmp_path / ".agentx" / "agentx.db")
    db.open()
    tasks = db.list_tasks()
    assert len(tasks) == 1
    db.close()


def test_cli_status_and_logs(tmp_path: Path) -> None:
    _run(tmp_path, "run", "测试任务")
    status_output = _run(tmp_path, "status")
    assert "Task #" in status_output
    logs_output = _run(tmp_path, "logs")
    assert "TaskStateChanged" in logs_output


def test_cli_stop_cancels_task(tmp_path: Path) -> None:
    db = SQLiteStore(tmp_path / ".agentx" / "agentx.db")
    db.open()
    from agentx.state.models import Task

    db.insert_project(Project(id="p1", root_path=str(tmp_path)))
    db.insert_task(Task(id="t1", project_id="p1", goal="待运行任务"))
    task_id = "t1"
    db.close()

    output = _run(tmp_path, "stop", "--task", task_id)
    assert "CANCELLED" in output

    db = SQLiteStore(tmp_path / ".agentx" / "agentx.db")
    db.open()
    assert db.get_task(task_id).state == TaskState.CANCELLED
    db.close()


def test_cli_agents_lists_roles(tmp_path: Path) -> None:
    _run(tmp_path, "init")
    output = _run(tmp_path, "agents")
    assert "executor-1" in output
    assert "reviewer-1" in output
    assert "verifier-1" in output


def test_cli_resume_without_task_fails(tmp_path: Path) -> None:
    result = runner.invoke(app, ["resume", "--path", str(tmp_path)])
    assert result.exit_code == 1
