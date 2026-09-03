"""Test Tool：运行测试命令并记录 exit code 作为证据。

Verifier 通过它建立 build/test 证据；Executor 也可以运行（TEST 权限）。
"""

from __future__ import annotations

import asyncio
from typing import Any

from agentx.tools.base import FunctionTool, Permission, ToolContext, ToolResult

_PARAMS_RUN = {
    "type": "object",
    "properties": {
        "command": {
            "type": "string",
            "description": "测试/构建命令，如 pytest、make test、cargo test",
        },
    },
    "required": ["command"],
}


async def _run_test(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    command = str(args["command"]).strip()
    if not command:
        return ToolResult(ok=False, error="命令为空")
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
        return ToolResult(ok=False, error=f"测试超时 ({ctx.timeout}s): {command}")
    except OSError as e:
        return ToolResult(ok=False, error=f"执行失败: {e}")

    return ToolResult(ok=proc.returncode == 0, output=output, exit_code=proc.returncode)


def build_test_tools() -> list[FunctionTool]:
    return [
        FunctionTool(
            name="test.run",
            description="运行测试/构建命令（pytest / make test 等），记录 exit code 作为证据",
            permission=Permission.TEST,
            parameters=_PARAMS_RUN,
            func=_run_test,
        )
    ]
