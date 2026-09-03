"""AgentRuntime：一个 Agent 的执行循环。

流程：system prompt + 对话 → Provider.chat → 有 tool_calls 则执行 Tool
并回填结果 → 直到模型给出纯文本回复或达到步数上限。

Tool 权限在注册表层强制；高风险 Tool 返回 requires_approval 时
Runtime 立即停止，把决策权交回上层（Orchestrator → WAITING_USER）。
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from typing import Any

from agentx.agents.definitions import AgentDefinition
from agentx.core.event_bus import EventBus, make_event
from agentx.providers.messages import ChatMessage
from agentx.state.models import (
    EVENT_AGENT_STARTED,
    EVENT_AGENT_THINKING,
    AgentXModel,
    Evidence,
)
from agentx.tools.base import ToolContext, ToolResult
from agentx.tools.registry import ToolRegistry


class AgentResult(AgentXModel):
    """Agent 单次运行结果。"""

    agent_id: str
    content: str | None = None
    steps: int = 0
    tool_results: list[ToolResult] = []
    evidence: list[Evidence] = []
    requires_approval: bool = False
    interrupted: bool = False

    @property
    def tool_evidence(self) -> list[ToolResult]:
        """可以当作证据的 Tool 结果（成功执行且有 exit_code）。"""
        return [r for r in self.tool_results if r.exit_code is not None]


class AgentRuntime:
    def __init__(
        self,
        definition: AgentDefinition,
        provider: Any,
        registry: ToolRegistry,
        event_bus: EventBus | None = None,
        auto_reject_high_risk: bool = False,
        stop_check: Callable[[], bool] | None = None,
    ) -> None:
        self.definition = definition
        self.provider = provider
        self.registry = registry
        self.event_bus = event_bus
        self.auto_reject_high_risk = auto_reject_high_risk
        self.stop_check = stop_check

    async def run(
        self,
        conversation: list[ChatMessage],
        ctx: ToolContext,
        *,
        task_id: str | None = None,
        max_steps: int | None = None,
    ) -> AgentResult:
        """运行 Agent：给定对话（不含 system），返回结果。"""
        limit = max_steps or self.definition.max_steps
        agent_id = self.definition.id
        if self.event_bus is not None:
            await self.event_bus.publish(
                make_event(EVENT_AGENT_STARTED, task_id=task_id, agent_id=agent_id)
            )

        messages: list[ChatMessage] = [
            ChatMessage(role="system", content=self.definition.system_prompt),
            *conversation,
        ]
        tool_results: list[ToolResult] = []
        evidence: list[Evidence] = []
        requires_approval = False
        empty_prompted = False
        auto_reject = self.auto_reject_high_risk
        tool_budget_left = TOOL_OUTPUT_BUDGET

        for step in range(1, limit + 1):
            if self.stop_check is not None and self.stop_check():
                return AgentResult(
                    agent_id=agent_id,
                    steps=step - 1,
                    tool_results=tool_results,
                    evidence=evidence,
                    interrupted=True,
                )
            if self.event_bus is not None:
                await self.event_bus.publish(
                    make_event(
                        EVENT_AGENT_THINKING,
                        task_id=task_id,
                        agent_id=agent_id,
                        payload={"step": step},
                    )
                )
            response = await self.provider.chat(
                messages,
                model=self.definition.model or "",
                tools=self.registry.specs(),
            )

            if response.tool_calls:
                messages.append(
                    ChatMessage(
                        role="assistant",
                        content=response.content,
                        tool_calls=response.tool_calls,
                    )
                )
                for call in response.tool_calls:
                    result = await self.registry.execute(
                        call.name,
                        call.arguments,
                        ctx,
                        self.definition.permissions,
                        task_id=task_id,
                        agent_id=agent_id,
                    )
                    tool_results.append(result)
                    if result.exit_code is not None:
                        evidence.append(
                            Evidence(
                                id=uuid.uuid4().hex,
                                task_id=task_id,
                                type=call.name,
                                command=str(call.arguments.get("command") or ""),
                                result=result.output,
                                exit_code=result.exit_code,
                            )
                        )
                    tool_text, tool_budget_left = _budgeted_tool_text(result, tool_budget_left)
                    messages.append(
                        ChatMessage(
                            role="tool",
                            tool_call_id=call.id,
                            content=tool_text,
                        )
                    )
                    if result.requires_approval:
                        if auto_reject:
                            # auto 模式：直接拒绝并继续，让 Agent 换安全方案
                            result.ok = False
                            result.requires_approval = False
                            result.error = f"高风险命令被自动拒绝（auto 模式）: {result.error}"
                        else:
                            requires_approval = True
                            break
                if requires_approval:
                    break
                continue

            # 模型在工具循环后可能返回空 content（deepseek 等常见），
            # 此时追问一次总结，避免空结论被当作"完成"
            if not (response.content or "").strip() and not empty_prompted:
                empty_prompted = True
                messages.append(
                    ChatMessage(
                        role="user",
                        content="请给出最终结论总结（不要调用工具）。",
                    )
                )
                continue

            return AgentResult(
                agent_id=agent_id,
                content=response.content,
                steps=step,
                tool_results=tool_results,
                evidence=evidence,
            )

        # 步数耗尽（interrupted）：追问一次最终结论，防止"工具循环结束但没输出总结"
        final_content: str | None = None
        if not requires_approval:
            final = await self.provider.chat(
                [
                    *messages,
                    ChatMessage(
                        role="user",
                        content="步数已用尽，请立即给出最终结论总结（不要调用任何工具）。",
                    ),
                ],
                model=self.definition.model or "",
                tools=None,
            )
            final_content = final.content

        return AgentResult(
            agent_id=agent_id,
            content=final_content,
            steps=limit,
            tool_results=tool_results,
            evidence=evidence,
            requires_approval=requires_approval,
            interrupted=not requires_approval,
        )


# 单次 Agent 运行的工具输出总预算（字符）：跨轮累积，防止多轮 Read 撑爆上下文。
# 60K 字符 ≈ 40-60K token（中文）；system+查询上下文通常 <10K token。
TOOL_OUTPUT_BUDGET = 60_000


def _budgeted_tool_text(result: ToolResult, budget_left: int) -> tuple[str, int]:
    """按剩余预算截断工具输出：返回 (消息文本, 剩余预算)。

    预算耗尽后不再追加内容（只给提示）；单次输出超预算时保留头部 + 截断说明。
    """
    text = _tool_result_text(result)
    if budget_left <= 0:
        return "(工具输出预算已耗尽，后续结果不再追加)", 0
    if len(text) <= budget_left:
        return text, budget_left - len(text)
    keep = max(budget_left - 120, 0)
    truncated = text[:keep]
    if keep > 0:
        truncated += f"\n...(工具输出超长，已截断至预算内：保留 {keep} 字符，原始 {len(text)} 字符)"
    return truncated, 0


def _tool_result_text(result: ToolResult) -> str:
    if result.ok:
        return result.output or f"exit={result.exit_code}"
    return f"ERROR: {result.error or '未知错误'} (exit={result.exit_code})"
