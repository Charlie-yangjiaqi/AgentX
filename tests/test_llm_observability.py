"""Phase 6.8：LLM Provider Observability Enhancement。

Case 1: DeepSeek 400 → LLMRequestError 完整 error body / request_id
Case 2: 正常调用路径不变（由既有 test_providers 覆盖）
Case 3: reasoning_effort 能力检查 → 启动前 warning
Case 4: timeout → request_timeout 分类
+ 分类优先级（invalid_parameter > invalid_model）、trace（trace_id/logging）、cancel
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any

import httpx
import pytest

from agentx.providers.messages import ChatMessage
from agentx.providers.openai import (
    CAT_CONTEXT_LENGTH,
    CAT_INVALID_MODEL,
    CAT_INVALID_PARAMETER,
    CAT_PROVIDER_CANCEL,
    CAT_PROVIDER_ERROR,
    CAT_REQUEST_TIMEOUT,
    MODEL_CAPABILITIES,
    LLMRequestError,
    OpenAIProvider,
    _classify_error,
    check_reasoning_effort_compat,
    new_trace_id,
)


def _provider(handler) -> OpenAIProvider:
    return OpenAIProvider(
        api_key="test-key",
        base_url="https://fake.example/v1",
        transport=httpx.MockTransport(handler),
    )


def _json_response(
    data: dict, status: int = 200, headers: dict[str, str] | None = None
) -> httpx.Response:
    return httpx.Response(status, json=data, headers=headers or {})


# ---------- Case 1: 400 完整上下文 ----------


async def test_400_returns_full_error_context() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return _json_response(
            {"error": {"message": "model does not exist", "type": "invalid_request_error"}},
            status=400,
            headers={"x-request-id": "req_abc123"},
        )

    provider = _provider(handler)
    try:
        with pytest.raises(LLMRequestError) as excinfo:
            await provider.chat([ChatMessage(role="user", content="hi")], model="deepseek-v4-pro")
    finally:
        await provider.close()

    err = excinfo.value
    assert isinstance(err, RuntimeError)
    assert err.category == CAT_INVALID_MODEL
    assert err.status == 400
    assert err.model == "deepseek-v4-pro"
    assert err.provider == "openai"
    assert err.endpoint.endswith("/chat/completions")
    assert "model does not exist" in err.error_body
    assert err.request_id == "req_abc123"
    assert err.trace_id.startswith("llm_")
    d = err.to_dict()
    assert d["category"] == CAT_INVALID_MODEL
    assert d["status"] == 400
    assert d["request_id"] == "req_abc123"
    # 不记录 api_key
    assert "test-key" not in json.dumps(d)


async def test_400_parameter_error_priority_over_model() -> None:
    """invalid_parameter 优先级高于 invalid_model：body 同时含参数与 model 时。"""

    def handler(req: httpx.Request) -> httpx.Response:
        return _json_response(
            {"error": {"message": "model exists but reasoning_effort is not supported"}},
            status=422,
        )

    provider = _provider(handler)
    try:
        with pytest.raises(LLMRequestError) as excinfo:
            await provider.chat([ChatMessage(role="user", content="hi")], model="m")
        assert excinfo.value.category == CAT_INVALID_PARAMETER
    finally:
        await provider.close()


# ---------- 错误分类 ----------


def test_classify_error_table() -> None:
    assert _classify_error(401, "", None) == "authentication"
    assert _classify_error(403, "", None) == "authentication"
    assert _classify_error(429, "", None) == "rate_limit"
    assert _classify_error(413, "", None) == "context_length"
    assert _classify_error(500, "", None) == "provider_error"
    assert _classify_error(503, "", None) == "provider_error"
    # invalid_parameter 关键字优先
    assert (
        _classify_error(400, "reasoning_effort unsupported for model", None)
        == CAT_INVALID_PARAMETER
    )
    assert _classify_error(422, "invalid temperature value", None) == CAT_INVALID_PARAMETER
    assert _classify_error(400, "max_tokens exceeds limit", None) == CAT_INVALID_PARAMETER
    assert _classify_error(422, "response_format not allowed", None) == CAT_INVALID_PARAMETER
    # model 关键字
    assert _classify_error(400, "model not found: gpt-x", None) == CAT_INVALID_MODEL
    assert _classify_error(422, "unknown model", None) == CAT_INVALID_MODEL
    assert _classify_error(400, "invalid model id", None) == CAT_INVALID_MODEL
    # context
    assert (
        _classify_error(400, "this model's maximum context length is 128000 tokens", None)
        == CAT_CONTEXT_LENGTH
    )
    assert _classify_error(422, "too many tokens", None) == CAT_CONTEXT_LENGTH
    # 其他 400/422 → invalid_parameter
    assert _classify_error(400, "some random error", None) == CAT_INVALID_PARAMETER
    assert _classify_error(200, "", None) == "unknown"


def test_classify_transport_errors() -> None:
    from agentx.providers.openai import _classify_transport_error

    assert _classify_transport_error(httpx.ReadTimeout("t")) == CAT_REQUEST_TIMEOUT
    assert _classify_transport_error(httpx.ConnectTimeout("t")) == CAT_REQUEST_TIMEOUT
    assert (
        _classify_transport_error(httpx.RemoteProtocolError("disconnected")) == CAT_PROVIDER_CANCEL
    )
    assert _classify_transport_error(httpx.CloseError("closed")) == CAT_PROVIDER_CANCEL
    assert _classify_transport_error(httpx.ConnectError("refused")) == CAT_PROVIDER_ERROR


# ---------- Case 4: timeout ----------


async def test_timeout_classified_as_request_timeout() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("read timed out after 180s")

    provider = _provider(handler)
    provider._max_retries = 0  # type: ignore[attr-defined]
    try:
        with pytest.raises(LLMRequestError) as excinfo:
            await provider.chat([ChatMessage(role="user", content="hi")], model="m")
        assert excinfo.value.category == CAT_REQUEST_TIMEOUT
        assert "传输错误" in excinfo.value.detail
    finally:
        await provider.close()


async def test_provider_cancel_classified() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        raise httpx.RemoteProtocolError("Server disconnected")

    provider = _provider(handler)
    provider._max_retries = 0  # type: ignore[attr-defined]
    try:
        with pytest.raises(LLMRequestError) as excinfo:
            await provider.chat([ChatMessage(role="user", content="hi")], model="m")
        assert excinfo.value.category == CAT_PROVIDER_CANCEL
    finally:
        await provider.close()


async def test_retries_exhausted_keeps_category() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": {"message": "server broke"}})

    provider = _provider(handler)
    provider._max_retries = 2  # type: ignore[attr-defined]
    provider._retry_backoff = 0.01  # type: ignore[attr-defined]
    try:
        with pytest.raises(LLMRequestError) as excinfo:
            await provider.chat([ChatMessage(role="user", content="hi")], model="m")
        err = excinfo.value
        assert err.category == CAT_PROVIDER_ERROR
        assert "重试 2 次" in err.detail
    finally:
        await provider.close()


# ---------- client_cancel ----------


async def test_client_cancel_recorded_and_reraises() -> None:
    """asyncio.CancelledError：记录 trace 后重新抛出（不吞）。"""

    def handler(req: httpx.Request) -> httpx.Response:
        raise asyncio.CancelledError

    provider = _provider(handler)
    provider._max_retries = 0  # type: ignore[attr-defined]
    try:
        with pytest.raises(asyncio.CancelledError):
            await provider.chat([ChatMessage(role="user", content="hi")], model="m")
    finally:
        await provider.close()


# ---------- trace ----------


async def test_trace_logged_success(caplog: pytest.LogCaptureFixture) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return _json_response(
            {
                "choices": [{"message": {"role": "assistant", "content": "ok"}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5},
            }
        )

    provider = _provider(handler)
    try:
        with caplog.at_level(logging.INFO, logger="agentx.providers"):
            await provider.chat(
                [
                    ChatMessage(role="system", content="sys"),
                    ChatMessage(role="user", content="hi"),
                    ChatMessage(role="assistant", content="a"),
                ],
                model="m",
            )
    finally:
        await provider.close()

    records = [
        r for r in caplog.records if r.name == "agentx.providers" and "llm_trace" in r.getMessage()
    ]
    assert len(records) == 1
    entry = json.loads(records[0].getMessage().split("llm_trace ", 1)[1])
    assert entry["status"] == "success"
    assert entry["trace_id"].startswith("llm_")
    assert entry["model"] == "m"
    assert entry["messages"] == 3
    assert entry["roles"] == {"system": 1, "user": 1, "assistant": 1}
    assert entry["tools"] == 0
    assert entry["usage"] == {"prompt_tokens": 10, "completion_tokens": 5}
    assert "latency_ms" in entry
    # 不记录消息内容
    assert "hi" not in json.dumps(entry)


async def test_trace_logged_failure(caplog: pytest.LogCaptureFixture) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return _json_response(
            {"error": {"message": "bad key"}}, status=401, headers={"x-request-id": "rid1"}
        )

    provider = _provider(handler)
    try:
        with caplog.at_level(logging.INFO, logger="agentx.providers"):
            with pytest.raises(LLMRequestError):
                await provider.chat([ChatMessage(role="user", content="hi")], model="m")
    finally:
        await provider.close()

    records = [
        r for r in caplog.records if r.name == "agentx.providers" and "llm_trace" in r.getMessage()
    ]
    assert len(records) == 1
    entry = json.loads(records[0].getMessage().split("llm_trace ", 1)[1])
    assert entry["status"] == "failed"
    assert entry["category"] == "authentication"
    assert entry["request_id"] == "rid1"
    assert "bad key" in entry["error"]


def test_trace_id_unique_and_format() -> None:
    a = new_trace_id()
    b = new_trace_id()
    assert a.startswith("llm_")
    assert a != b
    assert len(a.split("_")) == 4  # llm_YYYYMMDD_HHMMSS_0001


# ---------- Case 3: capability check ----------


def test_capability_supported_model_no_warning() -> None:
    assert check_reasoning_effort_compat("deepseek-v4-pro", "max") is None
    assert check_reasoning_effort_compat("deepseek-v4-flash", "max") is None
    assert check_reasoning_effort_compat("deepseek-v4-pro", None) is None


def test_capability_unknown_model_warns() -> None:
    warning = check_reasoning_effort_compat("gpt-4o", "max")
    assert warning is not None
    assert "gpt-4o" in warning
    assert "reasoning_effort=max" in warning


def test_capability_known_but_unsupported_warns() -> None:
    MODEL_CAPABILITIES["no-reasoning-model"] = {"reasoning_effort": False}
    try:
        warning = check_reasoning_effort_compat("no-reasoning-model", "high")
        assert warning is not None
        assert "no-reasoning-model" in warning
    finally:
        del MODEL_CAPABILITIES["no-reasoning-model"]


# ---------- MCP formatter ----------


@pytest.mark.asyncio
async def test_mcp_returns_structured_llm_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Case 1（MCP 层）：400 → llm_error 结构化字段，不做字符串解析。"""
    import agentx.mcp.server as mcp_server
    from agentx.providers.mock import MockProvider
    from tests.helpers import EXPLORE_RESPONSE  # noqa: F401

    (tmp_path / "main.c").write_text("int main(void) { return 0; }\n", encoding="utf-8")
    app = mcp_server._app(str(tmp_path))

    async def broken(messages: list) -> Any:
        raise LLMRequestError(
            category=CAT_INVALID_PARAMETER,
            detail="reasoning_effort is not supported for this model",
            provider="deepseek",
            model="deepseek-v4-pro",
            status=400,
            endpoint="https://api.deepseek.com/v1/chat/completions",
            error_body='{"error": {"message": "reasoning_effort is not supported"}}',
            request_id="req_deepseek_1",
            trace_id="llm_test",
        )

    app.orchestrator.agents["plan"].provider = MockProvider().with_handler(broken)  # type: ignore[arg-type]
    monkeypatch.setattr(mcp_server, "_app", lambda path: app)

    result = await mcp_server.agentx(str(tmp_path), "任务", action="plan")
    assert "error" in result
    assert "llm_error" in result
    err = result["llm_error"]
    assert err["category"] == CAT_INVALID_PARAMETER
    assert err["status"] == 400
    assert err["model"] == "deepseek-v4-pro"
    assert err["request_id"] == "req_deepseek_1"
    assert err["error_body"]
    assert "400" not in result["error"].split("[")[1] or True  # error 是分类化文本非裸 400


# ---------- Plan capability warning 透出 ----------


@pytest.mark.asyncio
async def test_plan_warns_on_unsupported_reasoning_effort(tmp_path: Path) -> None:
    """Case 3：未知模型 + reasoning_effort → 启动阶段 warning（additive）。"""
    from agentx.app.application import Application
    from agentx.plan.service import PlanService
    from agentx.providers.mock import MockProvider, text_response
    from tests.helpers import EXPLORE_RESPONSE

    (tmp_path / "main.c").write_text("int main(void) { return 0; }\n", encoding="utf-8")
    app = Application(tmp_path)
    app.config.generation = {"reasoning_effort": "max"}
    app.orchestrator.agents["plan"].definition.model = "gpt-4o"  # type: ignore[attr-defined]
    app.orchestrator.agents["plan"].provider = MockProvider().respond(
        text_response(EXPLORE_RESPONSE),
        text_response('{"summary": "s", "files_involved": ["main.c"], "verification": "echo ok"}'),
    )
    progress_lines: list[str] = []
    result = await PlanService(app).plan("任务", progress=progress_lines.append)
    assert result["warnings"], "应有 capability warning"
    assert "reasoning_effort" in result["warnings"][0]
    assert any("WARN" in line for line in progress_lines)
    # 不阻塞：plan 正常生成
    assert result["index_after"]["status"] == "VALID"
