"""Project Tool：项目概览（文件树统计 + 构建/测试配置线索）。"""

from __future__ import annotations

from typing import Any

from agentx.tools.base import FunctionTool, Permission, ToolContext, ToolResult


async def _inspect(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    root = ctx.project_root
    if not root.is_dir():
        return ToolResult(ok=False, error=f"项目根目录不存在: {root}")

    files = 0
    dirs = 0
    extensions: dict[str, int] = {}
    for p in root.rglob("*"):
        if p.is_dir():
            if any(part.startswith(".") for part in p.parts):
                continue
            dirs += 1
        elif p.is_file():
            if any(part.startswith(".") for part in p.parts):
                continue
            files += 1
            ext = p.suffix.lower() or "(无扩展名)"
            extensions[ext] = extensions.get(ext, 0) + 1

    top_exts = sorted(extensions.items(), key=lambda kv: kv[1], reverse=True)[:10]
    markers = []
    for name in (
        "Makefile",
        "CMakeLists.txt",
        "pyproject.toml",
        "Cargo.toml",
        "package.json",
        "go.mod",
    ):
        if (root / name).exists():
            markers.append(name)

    lines = [
        f"root: {root}",
        f"files: {files}, dirs: {dirs}",
        "top extensions: " + ", ".join(f"{e}×{n}" for e, n in top_exts),
        "build markers: " + (", ".join(markers) if markers else "(无)"),
    ]
    return ToolResult(ok=True, output="\n".join(lines))


def build_project_tools() -> list[FunctionTool]:
    return [
        FunctionTool(
            name="project.inspect",
            description="项目概览：文件数、目录数、常见扩展名分布、构建系统线索",
            permission=Permission.READ,
            parameters={"type": "object", "properties": {}},
            func=_inspect,
        )
    ]
