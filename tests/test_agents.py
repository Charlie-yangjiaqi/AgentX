"""P4 测试：AgentRuntime 工具调用循环。"""

from __future__ import annotations

import pytest

from agentx.agents.definitions import AgentDefinition
from agentx.agents.prompts import ROLE_PROMPTS
from agentx.agents.runtime import AgentRuntime
from agentx.providers.messages import ChatMessage, ModelResponse, ToolCall
from agentx.providers.mock import MockProvider
from agentx.state.models import AgentRole
from agentx.tools import build_default_registry
from agentx.tools.base import ToolContext


@pytest.fixture()
def ctx(tmp_path) -> ToolContext:
    return ToolContext(project_root=tmp_path)


@pytest.fixture()
def executor() -> AgentDefinition:
    return AgentDefinition.from_role(
        "executor-1", AgentRole.EXECUTOR, ROLE_PROMPTS["executor"], model="mock-model"
    )


async def test_plain_text_reply_no_tools(executor: AgentDefinition, ctx: ToolContext) -> None:
    provider = MockProvider().respond(ModelResponse(content="不需要工具，任务完成"))
    runtime = AgentRuntime(executor, provider, build_default_registry())
    result = await runtime.run([ChatMessage(role="user", content="做某事")], ctx)
    assert result.content == "不需要工具，任务完成"
    assert result.steps == 1
    assert not result.interrupted


async def test_single_tool_call_loop(executor: AgentDefinition, ctx: ToolContext) -> None:
    provider = MockProvider().respond(
        ModelResponse(
            tool_calls=[
                ToolCall(
                    id="c1",
                    name="fs.write_file",
                    arguments={"path": "a.txt", "content": "hi"},
                )
            ]
        ),
        ModelResponse(content="已写入 a.txt"),
    )
    runtime = AgentRuntime(executor, provider, build_default_registry())
    result = await runtime.run([ChatMessage(role="user", content="写一个文件")], ctx)
    assert result.content == "已写入 a.txt"
    assert result.steps == 2
    assert (ctx.project_root / "a.txt").read_text(encoding="utf-8") == "hi"
    assert len(result.tool_results) == 1


async def test_multi_tool_calls_in_one_turn(executor: AgentDefinition, ctx: ToolContext) -> None:
    provider = MockProvider().respond(
        ModelResponse(
            tool_calls=[
                ToolCall(
                    id="c1",
                    name="fs.write_file",
                    arguments={"path": "a.txt", "content": "1"},
                ),
                ToolCall(
                    id="c2",
                    name="fs.write_file",
                    arguments={"path": "b.txt", "content": "2"},
                ),
            ]
        ),
        ModelResponse(content="done"),
    )
    runtime = AgentRuntime(executor, provider, build_default_registry())
    result = await runtime.run([ChatMessage(role="user", content="写两个文件")], ctx)
    assert result.steps == 2
    assert len(result.tool_results) == 2
    assert (ctx.project_root / "a.txt").exists()
    assert (ctx.project_root / "b.txt").exists()


async def test_permission_enforced_at_runtime(ctx: ToolContext) -> None:
    reviewer = AgentDefinition.from_role(
        "reviewer-1", AgentRole.REVIEWER, ROLE_PROMPTS["reviewer"], model="m"
    )
    provider = MockProvider().respond(
        ModelResponse(
            tool_calls=[
                ToolCall(id="c1", name="fs.write_file", arguments={"path": "x", "content": "1"})
            ]
        ),
        ModelResponse(content="done"),
    )
    runtime = AgentRuntime(reviewer, provider, build_default_registry())
    result = await runtime.run([ChatMessage(role="user", content="改文件")], ctx)
    assert not result.tool_results[0].ok
    assert "权限不足" in (result.tool_results[0].error or "")


async def test_high_risk_tool_stops_loop(executor: AgentDefinition, ctx: ToolContext) -> None:
    provider = MockProvider().respond(
        ModelResponse(
            tool_calls=[ToolCall(id="c1", name="shell.run", arguments={"command": "rm -rf /"})]
        ),
        ModelResponse(content="should not happen"),
    )
    runtime = AgentRuntime(executor, provider, build_default_registry())
    result = await runtime.run([ChatMessage(role="user", content="清理")], ctx)
    assert result.requires_approval
    assert result.interrupted is False
    assert len(result.tool_results) == 1
    assert result.tool_results[0].requires_approval


async def test_max_steps_interrupt(executor: AgentDefinition, ctx: ToolContext) -> None:
    def forever(messages: list[ChatMessage]) -> ModelResponse:
        return ModelResponse(
            tool_calls=[ToolCall(id="c1", name="fs.read_file", arguments={"path": "missing.txt"})]
        )

    provider = MockProvider().with_handler(forever)
    runtime = AgentRuntime(executor, provider, build_default_registry())
    result = await runtime.run([ChatMessage(role="user", content="循环")], ctx, max_steps=3)
    assert result.interrupted
    assert result.steps == 3


async def test_interrupted_forces_final_summary(
    executor: AgentDefinition, ctx: ToolContext
) -> None:
    """步数耗尽时必须追问出最终结论，不能空手返回。"""
    calls: list[list[ChatMessage]] = []

    def forever(messages: list[ChatMessage]) -> ModelResponse:
        calls.append(messages)
        if len(calls) == 4:  # 第 4 次是追问，必须给结论
            return ModelResponse(content="最终结论：任务完成")
        return ModelResponse(
            tool_calls=[ToolCall(id="c1", name="fs.read_file", arguments={"path": "missing.txt"})]
        )

    provider = MockProvider().with_handler(forever)
    runtime = AgentRuntime(executor, provider, build_default_registry())
    result = await runtime.run([ChatMessage(role="user", content="循环")], ctx, max_steps=3)
    assert result.interrupted
    assert result.content == "最终结论：任务完成"
    assert calls[-1][-1].content == "步数已用尽，请立即给出最终结论总结（不要调用任何工具）。"


async def test_tool_evidence_collected(executor: AgentDefinition, ctx: ToolContext) -> None:
    provider = MockProvider().respond(
        ModelResponse(
            tool_calls=[ToolCall(id="c1", name="test.run", arguments={"command": "echo ok"})]
        ),
        ModelResponse(content="测试通过"),
    )
    runtime = AgentRuntime(executor, provider, build_default_registry())
    result = await runtime.run([ChatMessage(role="user", content="跑测试")], ctx)
    evidence = result.tool_evidence
    assert len(evidence) == 1
    assert evidence[0].exit_code == 0
