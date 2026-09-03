"""P3 测试：Event Bus + Tool 系统 + 权限门禁 + 沙箱。"""

from __future__ import annotations

import asyncio

import pytest

from agentx.core.event_bus import EventBus, make_event
from agentx.state.models import StoredEvent
from agentx.tools import build_default_registry
from agentx.tools.base import ROLE_PERMISSIONS, ToolContext, resolve_safe_path
from agentx.tools.registry import ToolRegistry

EXECUTOR = ROLE_PERMISSIONS["executor"]
REVIEWER = ROLE_PERMISSIONS["reviewer"]
VERIFIER = ROLE_PERMISSIONS["verifier"]


@pytest.fixture()
def ctx(tmp_path) -> ToolContext:
    return ToolContext(project_root=tmp_path)


@pytest.fixture()
def registry(ctx: ToolContext) -> ToolRegistry:
    return build_default_registry()


# ---------- Event Bus ----------


async def test_event_bus_dispatch_by_type() -> None:
    bus = EventBus()
    seen: list[str] = []

    async def on_tool(h: StoredEvent) -> None:
        seen.append(h.type)

    bus.subscribe(on_tool, event_type="ToolFinished")
    await bus.publish(make_event("ToolStarted", task_id="t1"))
    await bus.publish(make_event("ToolFinished", task_id="t1"))
    assert seen == ["ToolFinished"]


async def test_event_bus_all_subscriber() -> None:
    bus = EventBus()
    seen: list[StoredEvent] = []

    async def on_all(h: StoredEvent) -> None:
        seen.append(h)

    bus.subscribe(on_all)
    await bus.publish(make_event("A", task_id="t1", payload={"k": 1}))
    assert len(seen) == 1
    assert seen[0].task_id == "t1"
    assert seen[0].payload == {"k": 1}


# ---------- 权限 ----------


async def test_permission_gate_blocks_reviewer_write(
    registry: ToolRegistry, ctx: ToolContext
) -> None:
    result = await registry.execute(
        "fs.write_file",
        {"path": "a.txt", "content": "x"},
        ctx,
        REVIEWER,
        agent_id="reviewer",
    )
    assert not result.ok
    assert "权限不足" in (result.error or "")


async def test_executor_can_write(registry: ToolRegistry, ctx: ToolContext) -> None:
    result = await registry.execute(
        "fs.write_file",
        {"path": "a.txt", "content": "hello"},
        ctx,
        EXECUTOR,
    )
    assert result.ok
    assert (ctx.project_root / "a.txt").read_text(encoding="utf-8") == "hello"


async def test_verifier_can_run_test_but_not_write(
    registry: ToolRegistry, ctx: ToolContext
) -> None:
    write = await registry.execute("fs.write_file", {"path": "x", "content": "1"}, ctx, VERIFIER)
    assert not write.ok
    run = await registry.execute("test.run", {"command": "echo ok"}, ctx, VERIFIER)
    assert run.ok


# ---------- Filesystem ----------


async def test_read_write_roundtrip(registry: ToolRegistry, ctx: ToolContext) -> None:
    await registry.execute("fs.write_file", {"path": "sub/a.txt", "content": "你好"}, ctx, EXECUTOR)
    r = await registry.execute("fs.read_file", {"path": "sub/a.txt"}, ctx, EXECUTOR)
    assert r.ok
    assert r.output == "你好"


async def test_list_files(registry: ToolRegistry, ctx: ToolContext) -> None:
    (ctx.project_root / "a.c").write_text("x", encoding="utf-8")
    (ctx.project_root / "b.c").write_text("x", encoding="utf-8")
    (ctx.project_root / "docs").mkdir()
    (ctx.project_root / "docs" / "readme.md").write_text("x", encoding="utf-8")
    r = await registry.execute("fs.list", {"depth": 2}, ctx, EXECUTOR)
    assert r.ok
    assert "a.c" in r.output
    assert "docs/readme.md" in r.output


async def test_sandbox_blocks_escape(registry: ToolRegistry, ctx: ToolContext) -> None:
    r = await registry.execute("fs.read_file", {"path": "../outside.txt"}, ctx, EXECUTOR)
    assert not r.ok
    assert "越出" in (r.error or "")


def test_resolve_safe_path_boundary(tmp_path) -> None:
    inside = resolve_safe_path(tmp_path, "sub/a.txt")
    assert inside == (tmp_path / "sub" / "a.txt").resolve()
    with pytest.raises(ValueError):
        resolve_safe_path(tmp_path, "../outside")


# ---------- Shell ----------


async def test_shell_run(registry: ToolRegistry, ctx: ToolContext) -> None:
    r = await registry.execute("shell.run", {"command": "echo hello"}, ctx, EXECUTOR)
    assert r.ok
    assert r.exit_code == 0
    assert "hello" in (r.output or "")


async def test_shell_failing_command(registry: ToolRegistry, ctx: ToolContext) -> None:
    r = await registry.execute("shell.run", {"command": "exit 3"}, ctx, EXECUTOR)
    assert not r.ok
    assert r.exit_code == 3


async def test_shell_high_risk_requires_approval(registry: ToolRegistry, ctx: ToolContext) -> None:
    r = await registry.execute("shell.run", {"command": "rm -rf /"}, ctx, EXECUTOR)
    assert not r.ok
    assert r.requires_approval


async def test_shell_reviewer_blocked(registry: ToolRegistry, ctx: ToolContext) -> None:
    r = await registry.execute("shell.run", {"command": "echo hi"}, ctx, REVIEWER)
    assert not r.ok
    assert "权限不足" in (r.error or "")


# ---------- Git ----------


async def test_git_status_and_diff(registry: ToolRegistry, ctx: ToolContext) -> None:
    proc = await asyncio.create_subprocess_exec(
        "git",
        "init",
        "-q",
        cwd=ctx.project_root,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    await proc.wait()

    (ctx.project_root / "f.c").write_text("int x;\n", encoding="utf-8")
    add = await asyncio.create_subprocess_exec(
        "git",
        "add",
        "f.c",
        cwd=ctx.project_root,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    await add.wait()
    (ctx.project_root / "f.c").write_text("int x = 1;\n", encoding="utf-8")

    status = await registry.execute("git.status", {}, ctx, EXECUTOR)
    assert status.ok
    assert "f.c" in (status.output or "")

    diff = await registry.execute("git.diff", {"stat": True}, ctx, EXECUTOR)
    assert diff.ok
    assert "f.c" in (diff.output or "")


# ---------- Test ----------


async def test_test_run_records_exit_code(registry: ToolRegistry, ctx: ToolContext) -> None:
    r = await registry.execute("test.run", {"command": "echo ok"}, ctx, VERIFIER)
    assert r.ok
    assert r.exit_code == 0


# ---------- 事件联动 ----------


async def test_registry_publishes_tool_events(registry: ToolRegistry, ctx: ToolContext) -> None:
    bus = EventBus()
    seen: list[StoredEvent] = []

    async def on_event(h: StoredEvent) -> None:
        seen.append(h)

    bus.subscribe(on_event)
    registry2 = build_default_registry(bus)
    await registry2.execute(
        "fs.write_file", {"path": "a.txt", "content": "1"}, ctx, EXECUTOR, task_id="t1"
    )

    types = [e.type for e in seen]
    assert "ToolStarted" in types
    assert "ToolFinished" in types
    assert seen[0].task_id == "t1"


async def test_provider_spec_mapping(registry: ToolRegistry) -> None:
    specs = registry.specs()
    names = {s.name for s in specs}
    assert {
        "fs.read_file",
        "fs.write_file",
        "fs.list",
        "shell.run",
        "git.status",
        "git.diff",
        "test.run",
        "project.inspect",
    } <= names
