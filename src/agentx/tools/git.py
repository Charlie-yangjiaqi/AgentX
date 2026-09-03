"""Git Tools：读取真实 Git 状态（status / diff）。

V1 只做只读操作（GIT_READ）；commit / checkout 等写操作后置。
"""

from __future__ import annotations

import asyncio
from typing import Any

from agentx.tools.base import FunctionTool, Permission, ToolContext, ToolResult

_PARAMS_DIFF = {
    "type": "object",
    "properties": {
        "path": {"type": "string", "description": "限定到单个文件（可选）"},
        "stat": {"type": "boolean", "description": "只输出统计摘要", "default": False},
    },
}


async def _git(args: list[str], ctx: ToolContext) -> ToolResult:
    try:
        proc = await asyncio.create_subprocess_exec(
            "git",
            *args,
            cwd=ctx.project_root,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        output_bytes, _ = await asyncio.wait_for(proc.communicate(), timeout=ctx.timeout)
        output = output_bytes.decode("utf-8", errors="replace").rstrip()
    except TimeoutError:
        return ToolResult(ok=False, error=f"git 超时: {' '.join(args)}")
    except OSError as e:
        return ToolResult(ok=False, error=f"git 执行失败: {e}")
    return ToolResult(ok=proc.returncode == 0, output=output, exit_code=proc.returncode)


async def _git_status(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    return await _git(["status", "--porcelain=v1", "--branch"], ctx)


async def _git_diff(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    cmd = ["diff"]
    if args.get("stat"):
        cmd.append("--stat")
    if args.get("path"):
        cmd.append("--")
        cmd.append(str(args["path"]))
    return await _git(cmd, ctx)


def build_git_tools() -> list[FunctionTool]:
    return [
        FunctionTool(
            name="git.status",
            description="查看工作区状态（porcelain 格式，含分支信息）",
            permission=Permission.GIT_READ,
            parameters={"type": "object", "properties": {}},
            func=_git_status,
        ),
        FunctionTool(
            name="git.diff",
            description="查看未提交的修改（默认全文 diff，可 --stat 或限定文件）",
            permission=Permission.GIT_READ,
            parameters=_PARAMS_DIFF,
            func=_git_diff,
        ),
    ]
