"""P2 测试：Provider 层（Mock + OpenAI 兼容）。"""

from __future__ import annotations

import httpx
import pytest

from agentx.providers.messages import ChatMessage, ToolCall, ToolSpec
from agentx.providers.mock import MockProvider, text_response
from agentx.providers.openai import LLMRequestError, OpenAIProvider


async def test_mock_provider_scripted() -> None:
    provider = MockProvider().respond(
        text_response("第一步"),
        text_response("第二步"),
    )
    r1 = await provider.chat([ChatMessage(role="user", content="hi")], model="m")
    r2 = await provider.chat([ChatMessage(role="user", content="hi")], model="m")
    assert r1.content == "第一步"
    assert r2.content == "第二步"
    assert len(provider.calls) == 2


async def test_mock_provider_handler() -> None:
    provider = MockProvider().with_handler(
        lambda messages: text_response(f"你说了: {messages[-1].content}")
    )
    r = await provider.chat([ChatMessage(role="user", content="你好")], model="m")
    assert r.content == "你说了: 你好"


async def test_mock_provider_echo_without_script() -> None:
    provider = MockProvider()
    r = await provider.chat([ChatMessage(role="user", content="x")], model="m")
    assert (r.content or "").startswith("[mock:")


async def test_openai_provider_parses_text_response() -> None:
    provider = _provider_with_transport(
        lambda req: _json_response(
            {
                "choices": [{"message": {"role": "assistant", "content": "你好，我是助手"}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5},
            }
        )
    )
    try:
        r = await provider.chat([ChatMessage(role="user", content="hi")], model="gpt-x")
        assert r.content == "你好，我是助手"
        assert r.usage.prompt_tokens == 10
        assert r.usage.completion_tokens == 5
        assert not r.has_tool_calls
    finally:
        await provider.close()


async def test_openai_provider_parses_tool_calls() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        body = _load_json(req.content)
        assert body["model"] == "gpt-x"
        assert body["messages"][0]["role"] == "system"
        assert body["tools"][0]["function"]["name"] == "read_file"
        return _json_response(
            {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "call_1",
                                    "type": "function",
                                    "function": {
                                        "name": "read_file",
                                        "arguments": '{"path": "a.c"}',
                                    },
                                }
                            ],
                        }
                    }
                ],
                "usage": {"prompt_tokens": 20, "completion_tokens": 8},
            }
        )

    provider = _provider_with_transport(handler)
    try:
        r = await provider.chat(
            [
                ChatMessage(role="system", content="sys"),
                ChatMessage(role="user", content="hi"),
            ],
            model="gpt-x",
            tools=[
                ToolSpec(
                    name="read_file",
                    description="读取文件",
                    parameters={"type": "object"},
                )
            ],
        )
        assert r.has_tool_calls
        tc = r.tool_calls[0]
        assert tc.name == "read_file"
        assert tc.arguments == {"path": "a.c"}
    finally:
        await provider.close()


async def test_openai_provider_serializes_tool_result_message() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        body = _load_json(req.content)
        tool_msg = body["messages"][-1]
        assert tool_msg["role"] == "tool"
        assert tool_msg["tool_call_id"] == "call_1"
        return _json_response({"choices": [{"message": {"role": "assistant", "content": "ok"}}]})

    provider = _provider_with_transport(handler)
    try:
        await provider.chat(
            [
                ChatMessage(
                    role="assistant",
                    tool_calls=[ToolCall(id="call_1", name="x", arguments={})],
                ),
                ChatMessage(role="tool", tool_call_id="call_1", content="42"),
            ],
            model="gpt-x",
        )
    finally:
        await provider.close()


async def test_openai_provider_retries_transport_error() -> None:
    calls = {"n": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] < 3:
            raise httpx.RemoteProtocolError("Server disconnected")
        return _json_response({"choices": [{"message": {"role": "assistant", "content": "ok"}}]})

    provider = _provider_with_transport(handler)
    provider._max_retries = 3  # type: ignore[attr-defined]
    try:
        r = await provider.chat([ChatMessage(role="user", content="hi")], model="gpt-x")
        assert r.content == "ok"
        assert calls["n"] == 3
    finally:
        await provider.close()


async def test_openai_provider_gives_up_after_retries() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        raise httpx.RemoteProtocolError("Server disconnected")

    provider = _provider_with_transport(handler)
    provider._max_retries = 2  # type: ignore[attr-defined]
    provider._retry_backoff = 0.01  # type: ignore[attr-defined]
    try:
        with pytest.raises(RuntimeError, match="模型调用失败"):
            await provider.chat([ChatMessage(role="user", content="hi")], model="gpt-x")
    finally:
        await provider.close()


async def test_openai_provider_does_not_retry_4xx() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": {"message": "bad key"}})

    provider = _provider_with_transport(handler)
    provider._max_retries = 2  # type: ignore[attr-defined]
    try:
        with pytest.raises(LLMRequestError) as excinfo:
            await provider.chat([ChatMessage(role="user", content="hi")], model="gpt-x")
        err = excinfo.value
        assert err.category == "authentication"
        assert err.status == 401
        assert err.model == "gpt-x"
        assert "bad key" in err.error_body
    finally:
        await provider.close()


async def test_openai_provider_handles_null_tool_calls() -> None:
    """OpenCode Go 等真实 API 会返回 tool_calls: null（键存在、值为 null）。"""

    def handler(req: httpx.Request) -> httpx.Response:
        return _json_response(
            {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": "ok",
                            "tool_calls": None,
                        }
                    }
                ],
                "usage": {},
            }
        )

    provider = _provider_with_transport(handler)
    try:
        r = await provider.chat([ChatMessage(role="user", content="hi")], model="gpt-x")
        assert r.content == "ok"
        assert not r.has_tool_calls
    finally:
        await provider.close()


async def test_openai_provider_adapts_tool_name_for_strict_apis() -> None:
    """DeepSeek 官方 API 只允许 [a-zA-Z0-9_-] 工具名：fs.read_file → fs_read_file。"""

    def handler(req: httpx.Request) -> httpx.Response:
        body = _load_json(req.content)
        assert body["tools"][0]["function"]["name"] == "fs_read_file"
        return _json_response(
            {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "call_1",
                                    "type": "function",
                                    "function": {
                                        "name": "fs_read_file",
                                        "arguments": '{"path": "a.c"}',
                                    },
                                }
                            ],
                        }
                    }
                ],
                "usage": {},
            }
        )

    provider = _provider_with_transport(handler)
    try:
        r = await provider.chat(
            [ChatMessage(role="user", content="hi")],
            model="gpt-x",
            tools=[
                ToolSpec(
                    name="fs.read_file",
                    description="读取文件",
                    parameters={"type": "object"},
                )
            ],
        )
        assert r.has_tool_calls
        assert r.tool_calls[0].name == "fs.read_file"
    finally:
        await provider.close()


def _provider_with_transport(handler) -> OpenAIProvider:
    transport = httpx.MockTransport(handler)
    return OpenAIProvider(
        api_key="test-key", base_url="https://fake.example/v1", transport=transport
    )


def _json_response(data: dict) -> httpx.Response:

    return httpx.Response(200, json=data)


def _load_json(data: bytes) -> dict:
    import json

    return json.loads(data)
