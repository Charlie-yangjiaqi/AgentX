"""P0 冒烟测试：项目可安装、可运行、可测试。"""

from typer.testing import CliRunner

from agentx.cli.app import app

runner = CliRunner()


def test_cli_help_shows_commands() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    for command in ("init", "run", "status", "logs", "resume", "stop", "agents"):
        assert command in result.stdout
