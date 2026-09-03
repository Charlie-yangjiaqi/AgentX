"""Filesystem Tools：read / write / list，全部限制在项目根目录内。"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from agentx.tools.base import FunctionTool, Permission, ToolContext, ToolResult, resolve_safe_path

_PARAMS_READ = {
    "type": "object",
    "properties": {"path": {"type": "string", "description": "相对项目根的文件路径"}},
    "required": ["path"],
}
_PARAMS_WRITE = {
    "type": "object",
    "properties": {
        "path": {"type": "string", "description": "相对项目根的文件路径"},
        "content": {"type": "string", "description": "要写入的完整内容"},
        "append": {"type": "boolean", "description": "True 则追加而非覆盖", "default": False},
    },
    "required": ["path", "content"],
}
_PARAMS_LIST = {
    "type": "object",
    "properties": {
        "path": {"type": "string", "description": "相对项目根的目录路径", "default": "."},
        "depth": {"type": "integer", "description": "递归深度，1 只列一层", "default": 1},
    },
}


MAX_READ_CHARS = 16_000


async def _read_file(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    try:
        target = resolve_safe_path(ctx.project_root, str(args["path"]))
    except ValueError as e:
        return ToolResult(ok=False, error=str(e))
    if not target.is_file():
        return ToolResult(ok=False, error=f"文件不存在: {target}")
    try:
        data = target.read_bytes()
    except OSError as e:
        return ToolResult(ok=False, error=f"读取失败: {e}")
    if b"\x00" in data[:8192]:
        return ToolResult(ok=False, error=f"二进制文件不支持读取: {target.name}")
    try:
        content = data.decode("utf-8", errors="replace")
    except Exception:
        content = ""
    if len(content) > MAX_READ_CHARS:
        content = (
            content[:MAX_READ_CHARS]
            + f"\n...(已截断：文件共 {len(content)} 字符，仅显示前 {MAX_READ_CHARS} 字符，"
            + "如需更多内容请用行号范围分段读取)"
        )
    return ToolResult(ok=True, output=content)


async def _write_file(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    try:
        target = resolve_safe_path(ctx.project_root, str(args["path"]))
    except ValueError as e:
        return ToolResult(ok=False, error=str(e))
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        mode = "a" if args.get("append") else "w"
        with target.open(mode, encoding="utf-8", newline="\n") as f:
            f.write(str(args["content"]))
    except OSError as e:
        return ToolResult(ok=False, error=f"写入失败: {e}")
    return ToolResult(
        ok=True,
        output=f"已写入 {target.relative_to(ctx.project_root)} ({target.stat().st_size} bytes)",
    )


async def _list_files(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    try:
        target = resolve_safe_path(ctx.project_root, str(args.get("path", ".")))
    except ValueError as e:
        return ToolResult(ok=False, error=str(e))
    if not target.is_dir():
        return ToolResult(ok=False, error=f"目录不存在: {target}")

    depth = max(1, int(args.get("depth", 1)))
    lines: list[str] = []
    root_str = str(target.resolve())

    def walk(dir_path: Path, level: int) -> None:
        for entry in sorted(os.scandir(dir_path), key=lambda e: e.name.lower()):
            rel = os.path.relpath(entry.path, root_str).replace("\\", "/")
            if entry.is_dir():
                if rel != ".":
                    lines.append(f"{rel}/")
                if level < depth:
                    walk(Path(entry.path), level + 1)
            else:
                lines.append(rel)

    walk(target, 1)
    return ToolResult(ok=True, output="\n".join(lines) if lines else "(空)")


def build_fs_tools() -> list[FunctionTool]:
    return [
        FunctionTool(
            name="fs.read_file",
            description="读取项目内的文本文件内容（UTF-8）",
            permission=Permission.READ,
            parameters=_PARAMS_READ,
            func=_read_file,
        ),
        FunctionTool(
            name="fs.write_file",
            description="写入/覆盖/追加项目内文件（UTF-8，自动建目录）",
            permission=Permission.WRITE,
            parameters=_PARAMS_WRITE,
            func=_write_file,
        ),
        FunctionTool(
            name="fs.list",
            description="列出项目内目录结构",
            permission=Permission.READ,
            parameters=_PARAMS_LIST,
            func=_list_files,
        ),
    ]
