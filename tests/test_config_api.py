"""LLM API 配置模块测试（agentx config api / config api test）。

Case 1: 无配置状态（默认 provider + configured=false）
Case 2: 配置 OpenAI Compatible（resolve 正确、key 落 .env 不入 config.json）
Case 3: API 测试成功（/models 200）
Case 4: API Key 缺失 → 明确失败
Case 5: 错误 Base URL（404 / 非 OpenAI 服务）→ 明确原因
+ env 优先级 / .env 读写
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from agentx.config.llm import (
    PROVIDER_PRESETS,
    read_secret_env,
    resolve_llm,
    write_secret_env,
)
from agentx.config.llm import (
    test_llm_connection as check_llm_connection,
)


def _cfg(llm: dict | None = None, api_key: str | None = None) -> object:
    from agentx.config.config import AgentXConfig

    return AgentXConfig(llm=llm or {}, api_key=api_key)


# ---------- Case 1: 无配置状态 ----------


def test_unconfigured_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setattr("agentx.config.llm.secret_env_path", lambda: Path("nonexistent"))
    resolved = resolve_llm(_cfg(llm=None))
    assert resolved["provider"] == "openai"
    assert resolved["model"] == "gpt-4o-mini"
    assert resolved["base_url"] == "https://api.openai.com/v1"
    assert resolved["api_key"] is None
    assert resolved["configured"] is False


# ---------- Case 2: 配置 OpenAI Compatible ----------


def test_configured_compatible_resolve(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MY_KEY", raising=False)
    cfg = _cfg(
        llm={
            "provider": "compatible",
            "model": "my-model",
            "base_url": "https://vllm.local/v1",
            "api_key_env": "MY_KEY",
        }
    )
    monkeypatch.setattr("agentx.config.llm.secret_env_path", lambda: tmp_path / ".env")
    write_secret_env({"MY_KEY": "sk-test-secret"}, tmp_path / ".env")
    resolved = resolve_llm(cfg)
    assert resolved["provider"] == "compatible"
    assert resolved["model"] == "my-model"
    assert resolved["base_url"] == "https://vllm.local/v1"
    assert resolved["api_key"] == "sk-test-secret"
    assert resolved["key_source"] == "env_file:MY_KEY"
    assert resolved["configured"] is True


def test_env_wins_over_env_file(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("agentx.config.llm.secret_env_path", lambda: tmp_path / ".env")
    write_secret_env({"DEEPSEEK_API_KEY": "from-env-file"}, tmp_path / ".env")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "from-env")
    resolved = resolve_llm(_cfg(llm={"provider": "deepseek"}))
    assert resolved["api_key"] == "from-env"
    assert resolved["key_source"] == "env:DEEPSEEK_API_KEY"


def test_legacy_api_key_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setattr("agentx.config.llm.secret_env_path", lambda: Path("nonexistent"))
    resolved = resolve_llm(_cfg(llm=None, api_key="sk-legacy"))
    assert resolved["api_key"] == "sk-legacy"
    assert resolved["key_source"] == "config(legacy)"


def test_key_never_in_config_json(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    """key 写 .env，config.json 只存 api_key_env 引用。"""
    monkeypatch.setattr("agentx.config.llm.secret_env_path", lambda: tmp_path / ".env")
    write_secret_env({"OPENAI_API_KEY": "sk-plain"}, tmp_path / ".env")
    env_file = (tmp_path / ".env").read_text(encoding="utf-8")
    assert "sk-plain" in env_file
    cfg = _cfg(llm={"provider": "openai", "api_key_env": "OPENAI_API_KEY"})
    assert "sk-plain" not in cfg.model_dump_json()
    assert cfg.llm["api_key_env"] == "OPENAI_API_KEY"


# ---------- 连通性测试 ----------


class _FakeTransport(httpx.MockTransport):
    def __init__(self, status: int, payload: dict | None = None, error: Exception | None = None):
        self._status = status
        self._payload = payload
        self._error = error
        super().__init__(handler=self.handle_request)

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        if self._error is not None:
            raise self._error
        return httpx.Response(self._status, json=self._payload or {})


def _resolved(**overrides: object) -> dict:
    base: dict = {
        "provider": "compatible",
        "provider_name": "OpenAI Compatible API",
        "model": "test-model",
        "base_url": "https://vllm.local/v1",
        "api_key": "sk-test",
        "api_key_env": "MY_KEY",
        "key_source": "env_file:MY_KEY",
        "configured": True,
    }
    base.update(overrides)
    return base


_ORIG_CLIENT = httpx.Client  # 全局 httpx.Client 会被 monkeypatch，先保存原版


def _client_with(transport: httpx.MockTransport):
    def _factory(*args, **kwargs):
        return _ORIG_CLIENT(*args, transport=transport, **kwargs)

    return _factory


# Case 3: 成功
def test_connection_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "agentx.config.llm.httpx.Client",
        _client_with(_FakeTransport(200, {"data": [{"id": "test-model"}]})),
    )
    result = check_llm_connection(_resolved())
    assert result["ok"] is True
    assert result["status"] == 200
    assert result["latency_ms"] is not None


def test_connection_ok_model_not_listed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "agentx.config.llm.httpx.Client",
        _client_with(_FakeTransport(200, {"data": [{"id": "other"}]})),
    )
    result = check_llm_connection(_resolved())
    assert result["ok"] is True
    assert "不在 /models 列表" in result["detail"]


# Case 4: API Key 缺失
def test_connection_missing_key() -> None:
    result = check_llm_connection(_resolved(api_key=None))
    assert result["ok"] is False
    assert "API Key 缺失" in result["detail"]


def test_connection_missing_base_url() -> None:
    result = check_llm_connection(_resolved(base_url=""))
    assert result["ok"] is False
    assert "Base URL 未配置" in result["detail"]


# Case 5: 错误 Base URL / 认证失败 / 非 OpenAI 服务
def test_connection_404(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "agentx.config.llm.httpx.Client",
        _client_with(_FakeTransport(404)),
    )
    result = check_llm_connection(_resolved())
    assert result["ok"] is False
    assert "404" in result["detail"]
    assert "不是 OpenAI Compatible" in result["detail"]


def test_connection_401(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "agentx.config.llm.httpx.Client",
        _client_with(_FakeTransport(401)),
    )
    result = check_llm_connection(_resolved())
    assert result["ok"] is False
    assert "认证失败" in result["detail"]


def test_connection_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "agentx.config.llm.httpx.Client",
        _client_with(_FakeTransport(0, error=httpx.ConnectTimeout("timed out"))),
    )
    result = check_llm_connection(_resolved(), timeout=5)
    assert result["ok"] is False
    assert "超时" in result["detail"] or "网络错误" in result["detail"]


# ---------- .env 读写 ----------


def test_secret_env_roundtrip(tmp_path) -> None:
    p = tmp_path / ".env"
    write_secret_env({"A": "1", "B": "2"}, p)
    assert read_secret_env(p) == {"A": "1", "B": "2"}
    write_secret_env({"A": "3", "C": "4"}, p)
    env = read_secret_env(p)
    assert env == {"A": "3", "B": "2", "C": "4"}
    (tmp_path / ".env").write_text("# comment\nX=1\n\nY = spaced\n", encoding="utf-8")
    assert read_secret_env(p) == {"X": "1", "Y": "spaced"}


def test_secret_env_missing_file(tmp_path) -> None:
    assert read_secret_env(tmp_path / "nope.env") == {}


def test_presets_shape() -> None:
    assert set(PROVIDER_PRESETS) == {"openai", "anthropic", "deepseek", "compatible"}
    for name, preset in PROVIDER_PRESETS.items():
        assert preset["api_key_env"]
        assert "base_url" in preset or name == "compatible"
