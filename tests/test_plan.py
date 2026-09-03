"""P2 测试：Plan 服务（Index 管理 + Plan 生成）。"""

from __future__ import annotations

from pathlib import Path

import pytest

from agentx.app.application import Application
from agentx.index.index import IndexStatus, index_status
from agentx.plan.service import PlanService, load_plan, parse_plan
from agentx.providers.messages import ModelResponse, ToolCall
from agentx.providers.mock import MockProvider, text_response
from tests.helpers import EXPLORE_RESPONSE


def _make_c_project(tmp_path: Path) -> None:
    (tmp_path / "main.c").write_text("int main(void) { return 0; }\n", encoding="utf-8")
    (tmp_path / "param.c").write_text("// TODO: 未实现\n", encoding="utf-8")
    (tmp_path / "param.h").write_text("#ifndef P\n#define P\n#endif\n", encoding="utf-8")
    (tmp_path / "Makefile").write_text("all:\n\tcc main.c\n", encoding="utf-8")


def test_parse_plan_handles_fences() -> None:
    content = (
        "好的，计划如下：\n"
        "```json\n"
        '{"summary": "实现参数事务", "steps": [{"action": "写 param.c", "files": ["param.c"]}], '
        '"files_involved": ["param.c"], "risks": ["API 兼容"], '
        '"verification": "gcc -o main main.c param.c"}\n'
        "```"
    )
    plan = parse_plan(content)
    assert plan is not None
    assert plan.summary == "实现参数事务"
    assert plan.verification == "gcc -o main main.c param.c"


def test_parse_plan_invalid() -> None:
    assert parse_plan("完全无法解析") is None


@pytest.mark.asyncio
async def test_plan_creates_index_and_saves_plan(tmp_path: Path, gate_bypass: None) -> None:
    _make_c_project(tmp_path)
    app = Application(tmp_path)
    planner = MockProvider().respond(
        text_response(EXPLORE_RESPONSE),
        text_response(
            '{"summary": "实现参数事务", "steps": [{"action": "实现", "files": ["param.c"]}], '
            '"files_involved": ["param.c"], "risks": [], '
            '"verification": "gcc -Wall -o main main.c param.c && ./main"}'
        ),
    )
    app.orchestrator.agents["plan"].provider = planner

    service = PlanService(app)
    result = await service.plan("实现参数事务功能")

    # Index 从 MISSING 建立 → VALID（响应反映处理前/后状态）
    assert result["index_before"]["status"] == "MISSING"
    assert result["index_after"]["status"] == "VALID"
    assert result["index_created"] is True
    assert result["index_status"] == "VALID"
    status, _ = index_status(tmp_path)
    assert status == IndexStatus.VALID

    # Plan 落盘
    plan = load_plan(tmp_path)
    assert plan is not None
    assert plan.summary == "实现参数事务"
    assert (tmp_path / ".agentx" / "plan.json").exists()


@pytest.mark.asyncio
async def test_plan_reuses_valid_index(tmp_path: Path) -> None:
    _make_c_project(tmp_path)
    app = Application(tmp_path)
    planner = MockProvider().respond(
        text_response(EXPLORE_RESPONSE),
        text_response('{"summary": "s", "steps": [], "files_involved": [], "risks": []}'),
        text_response('{"summary": "s", "steps": [], "files_involved": [], "risks": []}'),
    )
    app.orchestrator.agents["plan"].provider = planner
    service = PlanService(app)

    # 第一次建 Index，第二次 VALID 复用
    await service.plan("任务一")
    status_before, _ = index_status(tmp_path)
    assert status_before == IndexStatus.VALID

    result = await service.plan("任务二")
    assert result["index_status"] == "VALID"
    assert result["index_before"]["status"] == "VALID"
    assert result["index_created"] is False


@pytest.mark.asyncio
async def test_plan_refreshes_stale_index(tmp_path: Path) -> None:
    _make_c_project(tmp_path)
    app = Application(tmp_path)
    planner = MockProvider().respond(
        text_response(EXPLORE_RESPONSE),
        text_response('{"summary": "s", "steps": [], "files_involved": [], "risks": []}'),
        text_response('{"summary": "s", "steps": [], "files_involved": [], "risks": []}'),
    )
    app.orchestrator.agents["plan"].provider = planner
    service = PlanService(app)

    await service.plan("任务一")
    # 项目变化 → STALE
    (tmp_path / "param.c").write_text("int changed;\n", encoding="utf-8")
    status, _ = index_status(tmp_path)
    assert status == IndexStatus.STALE

    result = await service.plan("任务二")
    assert result["index_status"] == "VALID"
    assert result["index_before"]["status"] == "STALE"
    assert result["index_after"]["status"] == "VALID"
    # 刷新后 VALID
    status, _ = index_status(tmp_path)
    assert status == IndexStatus.VALID


@pytest.mark.asyncio
async def test_plan_agent_can_read_files(tmp_path: Path, gate_bypass: None) -> None:
    """Plan agent 有工具权限：深读文件是允许的。"""
    _make_c_project(tmp_path)
    app = Application(tmp_path)
    calls: list[list] = []

    def handler(messages: list) -> ModelResponse:
        if len(calls) == 0:
            calls.append(messages)
            return ModelResponse(
                tool_calls=[ToolCall(id="c1", name="fs.read_file", arguments={"path": "param.c"})]
            )
        return text_response(
            '{"summary": "已读代码", "steps": [], "files_involved": ["param.c"], "risks": []}'
        )

    app.orchestrator.agents["plan"].provider = MockProvider().with_handler(handler)
    service = PlanService(app)
    result = await service.plan("实现参数事务")
    assert result["plan"]["summary"] == "已读代码"
