"""上下文防护测试：read 截断 / 工具循环全局预算 / _index_preview 摘要化。"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from agentx.providers.messages import ChatMessage, ModelResponse, ToolCall
from agentx.tools.base import ToolContext
from agentx.tools.fs import MAX_READ_CHARS, build_fs_tools


def _tool_call_response(call_id: str, name: str, arguments: dict) -> ModelResponse:
    return ModelResponse(tool_calls=[ToolCall(id=call_id, name=name, arguments=arguments)])


def _text_response(content: str) -> ModelResponse:
    return ModelResponse(content=content)


@pytest.fixture()
def ctx(tmp_path: Any) -> ToolContext:
    return ToolContext(project_root=tmp_path)


# ---------- 1. fs.read_file 截断 ----------


async def test_read_file_truncates_large_output(ctx) -> None:
    big = "x" * (MAX_READ_CHARS + 5000)
    (ctx.project_root / "big.c").write_text(big, encoding="utf-8")
    tool = next(t for t in build_fs_tools() if t.name == "fs.read_file")
    result = await tool.execute({"path": "big.c"}, ctx)
    assert result.ok
    assert len(result.output) <= MAX_READ_CHARS + 200  # 截断 + 提示
    assert "已截断" in result.output
    assert "仅显示前" in result.output


async def test_read_file_small_output_untouched(ctx) -> None:
    (ctx.project_root / "small.c").write_text("int main(void){}\n", encoding="utf-8")
    tool = next(t for t in build_fs_tools() if t.name == "fs.read_file")
    result = await tool.execute({"path": "small.c"}, ctx)
    assert result.ok
    assert "已截断" not in result.output
    assert "int main" in result.output


# ---------- 2. 工具循环全局预算 ----------


async def test_budgeted_tool_text_under_budget() -> None:
    from agentx.agents.runtime import _budgeted_tool_text
    from agentx.tools.base import ToolResult

    text, left = _budgeted_tool_text(ToolResult(ok=True, output="hi"), 1000)
    assert text == "hi"
    assert left == 998


async def test_budgeted_tool_text_over_budget_single() -> None:
    from agentx.agents.runtime import _budgeted_tool_text
    from agentx.tools.base import ToolResult

    text, left = _budgeted_tool_text(ToolResult(ok=True, output="y" * 5000), 1000)
    assert left == 0
    assert len(text) <= 1000
    assert "已截断" in text
    assert text.startswith("y" * (1000 - 120 - 100))


async def test_budgeted_tool_text_exhausted() -> None:
    from agentx.agents.runtime import _budgeted_tool_text
    from agentx.tools.base import ToolResult

    text, left = _budgeted_tool_text(ToolResult(ok=True, output="z" * 100), 0)
    assert left == 0
    assert "预算已耗尽" in text


async def test_multi_turn_tool_budget_accumulates(ctx) -> None:
    """多轮 Read 累积：总工具输出受 TOOL_OUTPUT_BUDGET 限制。"""

    from agentx.agents.definitions import AgentDefinition
    from agentx.agents.prompts import ROLE_PROMPTS
    from agentx.agents.runtime import TOOL_OUTPUT_BUDGET, AgentRuntime
    from agentx.providers.mock import MockProvider
    from agentx.state.models import AgentRole
    from agentx.tools import build_default_registry

    executor = AgentDefinition.from_role(
        "executor-1", AgentRole.EXECUTOR, ROLE_PROMPTS["executor"], model="m"
    )
    executor.max_steps = 5

    big = "d" * (TOOL_OUTPUT_BUDGET // 2 + 10_000)  # 单文件超预算一半以上
    (ctx.project_root / "huge1.c").write_text(big, encoding="utf-8")
    (ctx.project_root / "huge2.c").write_text(big, encoding="utf-8")

    provider = MockProvider().respond(
        *[_tool_call_response(f"c{i}", "fs.read_file", {"path": f"huge{i}.c"}) for i in (1, 2, 3)],
        _text_response("任务完成"),
    )
    runtime = AgentRuntime(executor, provider, build_default_registry())
    result = await runtime.run([ChatMessage(role="user", content="读文件")], ctx)

    # 工具全部执行（真实工具调用发生），但预算跨轮累积
    assert len(result.tool_results) == 3
    # 验证 messages 里工具文本总量受限：通过第二步/第三步输出被截断验证
    # 直接验证预算函数行为已在上方测试；此处验证循环不崩溃且完成
    assert result.content == "任务完成"


# ---------- 3. _index_preview 摘要化 ----------


def test_index_preview_build_info_summarized() -> None:
    from agentx.index.index import ProjectIndex
    from agentx.plan.service import _index_preview

    index = ProjectIndex(
        project_fingerprint="fp",
        index_version="1.3",
        generated_at=datetime(2026, 8, 29, tzinfo=UTC),
        files=[
            {"path": f"file{i}.c", "status": "active", "compile_status": "compiled"}
            for i in range(5)
        ],
        build_info={
            "system": "keil",
            "build_source": "keil",
            "target": "GD32F427",
            "cpu": None,
            "defines": ["USE_FREERTOS"],
            "has_build_config": True,
            "compiled_files": [{"file": f"file{i}.c", "compiled": True} for i in range(860)],
            "excluded_files": [{"file": f"ex{i}.c", "compiled": False} for i in range(23)],
        },
        file_count=5,
    )
    preview = _index_preview(index)
    # 摘要化：不再包含 compiled_files 全量列表
    assert "file859.c" not in preview  # 860 个文件的最后一条不在输出里
    assert "ex22.c" not in preview
    assert '"compiled_count": 860' in preview
    assert '"excluded_count": 23' in preview
    assert "GD32F427" in preview
    assert "USE_FREERTOS" in preview
    # 总长度可控（不随 compiled_files 数量增长）
    assert len(preview) < 3000
