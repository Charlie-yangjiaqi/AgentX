"""outbound User-Agent 规范测试。

Case 1: 默认请求 → User-Agent=AgentX/<version>
Case 2: 用户指定 User-Agent=Custom → 保持 Custom（不覆盖）
Case 3: OpenAIProvider client 实际携带默认 UA
"""

from __future__ import annotations

import re

import pytest

from agentx.http import (
    agentx_version,
    default_user_agent,
    with_default_user_agent,
)


def test_default_ua_added() -> None:
    headers = with_default_user_agent({"Authorization": "Bearer x"})
    assert headers["User-Agent"] == f"AgentX/{agentx_version()}"
    assert headers["Authorization"] == "Bearer x"


def test_default_ua_no_headers() -> None:
    assert with_default_user_agent()["User-Agent"].startswith("AgentX/")


def test_custom_ua_preserved() -> None:
    headers = with_default_user_agent({"User-Agent": "Custom/1.0"})
    assert headers["User-Agent"] == "Custom/1.0"


def test_custom_ua_case_insensitive() -> None:
    headers = with_default_user_agent({"user-agent": "Custom/2.0"})
    # 已有（大小写不敏感）不覆盖；httpx 发送时规范化 header 名
    assert headers["user-agent"] == "Custom/2.0"
    assert len(headers) == 1


def test_input_not_mutated() -> None:
    original = {"Authorization": "Bearer x"}
    with_default_user_agent(original)
    assert original == {"Authorization": "Bearer x"}


def test_version_shape() -> None:
    assert re.match(r"^\d+\.\d+\.\d+", agentx_version())
    assert default_user_agent().startswith("AgentX/")


def test_provider_client_sends_default_ua(monkeypatch: pytest.MonkeyPatch) -> None:
    """OpenAIProvider 统一 client 层实际携带 UA（无自定义时不覆盖）。"""
    from agentx.providers.openai import OpenAIProvider

    captured: dict = {}

    def _fake_factory(*args, **kwargs):
        captured.update(kwargs)
        return None

    monkeypatch.setattr("agentx.providers.openai.httpx.AsyncClient", _fake_factory)
    provider = OpenAIProvider(api_key="sk-test", base_url="https://x/v1")
    provider.client  # noqa: B018 触发 client 构造
    headers = captured.get("headers", {})
    assert headers.get("User-Agent") == f"AgentX/{agentx_version()}"


def test_provider_custom_ua_not_overridden(monkeypatch: pytest.MonkeyPatch) -> None:
    from agentx.providers.openai import OpenAIProvider

    captured: dict = {}

    def _fake_factory(*args, **kwargs):
        captured.update(kwargs)
        return None

    monkeypatch.setattr("agentx.providers.openai.httpx.AsyncClient", _fake_factory)
    provider = OpenAIProvider(
        api_key="sk-test",
        base_url="https://x/v1",
        headers={"User-Agent": "Custom/9.9"},
    )
    provider.client  # noqa: B018
    headers = captured.get("headers", {})
    assert headers.get("User-Agent") == "Custom/9.9"
