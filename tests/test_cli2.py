"""CLI 体验层测试：config / setup / doctor / sessions / 管道输入 / 权限模式。"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from agentx.cli.app import app
from agentx.config.config import AgentXConfig, load_config, save_config
from agentx.state.models import Task
from agentx.state.store import SQLiteStore

runner = CliRunner()


@pytest.fixture(autouse=True)
def _isolated_config(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """隔离真实 ~/.agentx 配置：无 api_key → mock provider（测试不依赖外部 LLM API）。"""
    monkeypatch.setattr(
        "agentx.config.config.default_config_path",
        lambda: tmp_path / "agentx_test_config.json",
    )


# ---------- config ----------


def test_config_roundtrip(tmp_path: Path) -> None:
    cfg = AgentXConfig(
        api_key="sk-test", base_url="https://api.deepseek.com/v1", model="deepseek-v4-flash"
    )
    p = tmp_path / "config.json"
    save_config(cfg, p)
    loaded = load_config(p)
    assert loaded.api_key == "sk-test"
    assert loaded.model == "deepseek-v4-flash"
    assert loaded.no_proxy is True


def test_config_defaults() -> None:
    cfg = AgentXConfig()
    assert cfg.permission_mode == "review"
    assert cfg.base_url == "https://api.deepseek.com/v1"


def test_load_config_missing_returns_default(tmp_path: Path) -> None:
    cfg = load_config(tmp_path / "nope.json")
    assert cfg.api_key is None


# ---------- CLI ----------


def test_cli_sessions_lists_tasks(tmp_path: Path) -> None:
    from agentx.state.models import Project

    db = SQLiteStore(tmp_path / ".agentx" / "agentx.db")
    db.open()
    db.insert_project(Project(id="p1", root_path=str(tmp_path)))
    db.insert_task(Task(id="t1", project_id="p1", goal="第一个任务"))
    db.insert_task(Task(id="t2", project_id="p1", goal="第二个任务"))
    db.close()

    result = runner.invoke(app, ["sessions", "--path", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert "t2" in result.output
    assert "第二个任务" in result.output


def test_cli_run_accepts_stdin(tmp_path: Path) -> None:
    result = runner.invoke(app, ["run", "-", "--path", str(tmp_path)], input="从管道来的任务\n")
    assert result.exit_code == 0, result.output
    assert "Task #" in result.output
    db = SQLiteStore(tmp_path / ".agentx" / "agentx.db")
    db.open()
    assert db.list_tasks()[-1].goal == "从管道来的任务"
    db.close()


def test_cli_run_print_mode(tmp_path: Path) -> None:
    result = runner.invoke(app, ["run", "测试任务", "--path", str(tmp_path), "--print"])
    assert result.exit_code == 0, result.output
    # print 模式不输出过程时间线，但输出最终状态
    assert "#" in result.output


def test_cli_doctor_without_key(tmp_path: Path) -> None:
    result = runner.invoke(app, ["doctor", "--path", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert "配置" in result.output


def test_cli_setup_writes_config(tmp_path: Path) -> None:
    result = runner.invoke(
        app,
        ["setup", "--config", str(tmp_path / "config.json")],
        input=(
            "y\n"
            "\n"
            "https://api.deepseek.com/v1\n"
            "deepseek-v4-flash\n"
            "y\n"
            "review\n"
            "n\n"  # plan 不用独立配置
            "n\n"  # review 不用独立配置
            "n\n"  # verify 不用独立配置
        ),
    )
    assert result.exit_code == 0, result.output
    assert "已保存" in result.output
    assert (tmp_path / "config.json").exists()


def test_cli_permission_mode_flag_accepted(tmp_path: Path) -> None:
    result = runner.invoke(
        app, ["run", "测试", "--path", str(tmp_path), "--permission-mode", "auto"]
    )
    assert result.exit_code == 0, result.output
