"""P3/P4 测试：Review（最小上下文）与 Verify（机器证据优先）。"""

from __future__ import annotations

from pathlib import Path

import pytest

from agentx.app.application import Application
from agentx.index.index import IndexStatus, index_status
from agentx.plan.service import PlanOutput, PlanService, save_plan
from agentx.providers.messages import ModelResponse
from agentx.providers.mock import MockProvider, text_response
from agentx.review.service import ReviewService
from agentx.verify.service import VerifyService
from tests.helpers import EXPLORE_RESPONSE


def _make_c_project(tmp_path: Path) -> None:
    (tmp_path / "main.c").write_text("int main(void) { return 0; }\n", encoding="utf-8")
    (tmp_path / "param.c").write_text("// TODO\n", encoding="utf-8")
    (tmp_path / "param.h").write_text("#ifndef P\n#define P\n#endif\n", encoding="utf-8")


async def _setup_with_plan(tmp_path: Path, app: Application | None = None) -> Application:
    _make_c_project(tmp_path)
    if app is None:
        app = Application(tmp_path)
    app.orchestrator.agents["plan"].provider = MockProvider().respond(
        text_response(EXPLORE_RESPONSE),
        text_response(
            '{"summary": "实现参数事务", "steps": [], "files_involved": ["param.c"], '
            '"risks": [], "verification": "echo build-ok"}'
        ),
    )
    await PlanService(app).plan("实现参数事务")
    return app


# ---------- Review ----------


@pytest.mark.asyncio
async def test_review_requires_index_first(tmp_path: Path) -> None:
    _make_c_project(tmp_path)
    app = Application(tmp_path)
    result = await ReviewService(app).review("任务")
    assert "先调用 agentx.plan" in result["error"]


@pytest.mark.asyncio
async def test_review_uses_index_plan_diff(tmp_path: Path, gate_bypass: None) -> None:
    app = await _setup_with_plan(tmp_path)
    seen: list[str] = []

    def handler(messages: list) -> ModelResponse:
        joined = "\n".join(m.content or "" for m in messages)
        seen.append(joined)
        return text_response(
            '{"verdict": "FAIL", "findings": [{"severity": "HIGH", "category": "回归", '
            '"location": "param.c", "description": "缺少回滚"}]}'
        )

    app.orchestrator.agents["reviewer"].provider = MockProvider().with_handler(handler)
    result = await ReviewService(app).review("实现参数事务")

    assert result["verdict"] == "FAIL"
    assert result["findings"][0]["severity"] == "HIGH"
    # 最小上下文：Plan 内容进入了消息
    assert "实施计划" in seen[-1]
    # 认知子图进入消息（中文"参数"→param 命中，比全量预览更精确）
    assert "param.c" in seen[-1]


@pytest.mark.asyncio
async def test_review_refreshes_stale_index(tmp_path: Path) -> None:
    app = await _setup_with_plan(tmp_path)
    (tmp_path / "param.c").write_text("int changed;\n", encoding="utf-8")
    status, _ = index_status(tmp_path)
    assert status == IndexStatus.STALE

    app.orchestrator.agents["reviewer"].provider = MockProvider().respond(
        text_response('{"verdict": "PASS", "findings": []}')
    )
    result = await ReviewService(app).review("实现参数事务")
    assert result["index_status"] == IndexStatus.STALE
    # 刷新后 VALID
    status, _ = index_status(tmp_path)
    assert status == IndexStatus.VALID


# ---------- Verify ----------


@pytest.mark.asyncio
async def test_verify_requires_plan(tmp_path: Path) -> None:
    _make_c_project(tmp_path)
    app = Application(tmp_path)
    result = await VerifyService(app).verify("任务")
    assert "先调用 agentx.plan" in result["error"]


@pytest.mark.asyncio
async def test_verify_uses_plan_verification_command(tmp_path: Path, gate_bypass: None) -> None:
    app = await _setup_with_plan(tmp_path)  # verification: echo build-ok
    result = await VerifyService(app).verify("实现参数事务")
    assert result["verdict"] == "PASS"
    assert result["evidence"][0]["exit_code"] == 0
    assert result["tests"] == [{"command": "echo build-ok", "passed": True}]
    assert "exit=0" in result["conclusion"]


@pytest.mark.asyncio
async def test_verify_fails_on_command_failure(tmp_path: Path) -> None:
    app = await _setup_with_plan(tmp_path)  # 先建 Index
    save_plan(
        tmp_path,
        PlanOutput(
            summary="s",
            steps=[],
            files_involved=[],
            risks=[],
            verification="exit 1",
        ),
    )
    result = await VerifyService(app).verify("任务")
    assert result["verdict"] == "FAIL"
    assert result["evidence"][0]["exit_code"] == 1


@pytest.mark.asyncio
async def test_verify_no_llm_calls(tmp_path: Path, gate_bypass: None) -> None:
    """Verify 全程确定性：模型 provider 不应被调用。"""
    app = await _setup_with_plan(tmp_path)
    calls: list = []
    app.orchestrator.agents["verifier"].provider = MockProvider().with_handler(
        lambda messages: calls.append(messages) or text_response("不应被调用")
    )
    result = await VerifyService(app).verify("实现参数事务")
    assert result["verdict"] == "PASS"
    assert calls == []  # 完全没有 LLM 调用
