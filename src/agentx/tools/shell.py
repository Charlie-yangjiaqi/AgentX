"""Shell Tool：受限的 shell 执行。

安全边界：cwd 固定在项目根目录；高风险命令模式命中时返回
requires_approval=True，等待用户 Allow Once / Allow Task / Deny。
"""

from __future__ import annotations

import asyncio
import re
from typing import Any

from agentx.tools.base import FunctionTool, Permission, ToolContext, ToolResult

_PARAMS_RUN = {
    "type": "object",
    "properties": {"command": {"type": "string", "description": "要执行的 shell 命令"}},
    "required": ["command"],
}

# 高风险命令模式（命中 → 需要用户审批）
_HIGH_RISK_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"\bsudo\b", re.IGNORECASE),
    re.compile(r"\brm\s+-[a-z]*[rf][a-z]*\s+[/~]", re.IGNORECASE),
    re.compile(r"\bformat\b", re.IGNORECASE),
    re.compile(r"\bshutdown\b", re.IGNORECASE),
    re.compile(r"\brestart\b", re.IGNORECASE),
    re.compile(r"\breg\s+(delete|add)\b", re.IGNORECASE),
    re.compile(r"\bdiskpart\b", re.IGNORECASE),
    re.compile(r"\bRemove-Item\b", re.IGNORECASE),
    re.compile(r"\bdel\s+/[a-z]*[sq][a-z]*\b", re.IGNORECASE),
    re.compile(r"curl\s+.*\|\s*(sh|bash)", re.IGNORECASE),
    re.compile(r"git\s+push\s+--force", re.IGNORECASE),
    re.compile(r"chmod\s+777", re.IGNORECASE),
    re.compile(r"\bnet\s+user\b", re.IGNORECASE),
]


def _is_high_risk(command: str) -> bool:
    return any(p.search(command) for p in _HIGH_RISK_PATTERNS)


async def _run_shell(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    command = str(args["command"]).strip()
    if not command:
        return ToolResult(ok=False, error="命令为空")

    if _is_high_risk(command):
        return ToolResult(
            ok=False,
            error=f"高风险命令需要用户审批: {command}",
            requires_approval=True,
        )

    try:
        proc = await asyncio.create_subprocess_shell(
            command,
            cwd=ctx.project_root,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        output_bytes, _ = await asyncio.wait_for(proc.communicate(), timeout=ctx.timeout)
        output = output_bytes.decode("utf-8", errors="replace").rstrip()
    except TimeoutError:
        return ToolResult(ok=False, error=f"命令超时 ({ctx.timeout}s): {command}")
    except OSError as e:
        return ToolResult(ok=False, error=f"执行失败: {e}")

    return ToolResult(ok=proc.returncode == 0, output=output, exit_code=proc.returncode)


def build_shell_tools() -> list[FunctionTool]:
    return [
        FunctionTool(
            name="shell.run",
            description="在项目根目录内执行 shell 命令（只读/构建/测试类命令）",
            permission=Permission.SHELL,
            parameters=_PARAMS_RUN,
            func=_run_shell,
        )
    ]
