"""P0 修复测试：配置层（resolve_llm）真正驱动运行时（Application provider）。

Test 1: config.json provider=deepseek → Application 得到 DeepSeek provider
Test 2: 环境变量覆盖配置（key 来源 env > .env > config）
Test 3: legacy 配置（api_key/base_url/model）仍然可以调用
Test 4: 无任何 API 配置 → fallback/mock 行为保持
Test 5: MCP/Application 调用路径的 provider 来自 resolve_llm
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from agentx.app.application import Application
from agentx.config.config import AgentXConfig, save_config
from agentx.config.llm import PROVIDER_PRESETS, resolve_llm
from agentx.providers.mock import MockProvider
from agentx.providers.openai import OpenAIProvider


@pytest.fixture
def iso_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """隔离真实环境与 ~/.agentx/.env（避免测试被本机配置污染）。"""
    for name in (
        "OPENAI_API_KEY",
        "OPENAI_BASE_URL",
        "AGENTX_MODEL",
        "DEEPSEEK_API_KEY",
        "ANTHROPIC_API_KEY",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr("agentx.config.llm.secret_env_path", lambda: Path("nonexistent"))


def _mk_app(tmp_path: Path, cfg: AgentXConfig) -> Application:
    save_config(cfg, tmp_path / "config.json")
    return Application(tmp_path / "proj", config=cfg)


def test_deepseek_provider_from_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, iso_env: None
) -> None:
    """Test 1: config provider=deepseek → Application 使用 DeepSeek endpoint。"""
    monkeypatch.setattr("agentx.config.llm.secret_env_path", lambda: tmp_path / ".env")
    from agentx.config.llm import write_secret_env

    write_secret_env({"DEEPSEEK_API_KEY": "sk-ds"}, tmp_path / ".env")
    cfg = AgentXConfig(
        llm={"provider": "deepseek", "model": "test", "base_url": "https://api.deepseek.com/v1"}
    )
    app = _mk_app(tmp_path, cfg)
    try:
        assert isinstance(app.provider, OpenAIProvider)
        assert app.provider_name == "deepseek"
        assert app.base_url == "https://api.deepseek.com/v1"
        assert app.model == "test"
        assert app.api_key == "sk-ds"
    finally:
        app.store.close()


def test_env_overrides_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Test 2: env（DEEPSEEK_API_KEY）> config key，provider=deepseek 生效。"""
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-env-deepseek")
    cfg = AgentXConfig(llm={"provider": "deepseek", "model": "deepseek-chat"})
    resolved = resolve_llm(cfg)
    assert resolved["api_key"] == "sk-env-deepseek"
    assert resolved["key_source"] == "env:DEEPSEEK_API_KEY"
    app = _mk_app(tmp_path, cfg)
    try:
        assert isinstance(app.provider, OpenAIProvider)
        assert app.provider_name == "deepseek"
        assert app.api_key == "sk-env-deepseek"
    finally:
        app.store.close()


def test_legacy_config_still_works(tmp_path: Path, iso_env: None) -> None:
    """Test 3: legacy api_key/base_url/model 继续生效（provider=openai）。"""
    cfg = AgentXConfig(
        api_key="sk-legacy-123",
        base_url="http://127.0.0.1:8000/v1",
        model="legacy-model",
    )
    resolved = resolve_llm(cfg)
    assert resolved["api_key"] == "sk-legacy-123"
    assert resolved["key_source"] == "config(legacy)"
    assert resolved["base_url"] == "http://127.0.0.1:8000/v1"
    assert resolved["model"] == "legacy-model"
    app = _mk_app(tmp_path, cfg)
    try:
        assert isinstance(app.provider, OpenAIProvider)
        assert app.base_url == "http://127.0.0.1:8000/v1"
        assert app.model == "legacy-model"
    finally:
        app.store.close()


def test_no_config_falls_back_to_mock(tmp_path: Path, iso_env: None) -> None:
    """Test 4: 无任何 API 配置 → mock 行为保持（不创建真实 provider）。"""
    cfg = AgentXConfig()
    app = _mk_app(tmp_path, cfg)
    try:
        assert isinstance(app.provider, MockProvider)
        assert app.provider_name == "mock"
        assert app.api_key is None
    finally:
        app.store.close()


def test_provider_from_resolve_llm_single_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, iso_env: None
) -> None:
    """Test 5: Application provider 与 resolve_llm 单点解析一致（唯一配置入口）。"""
    monkeypatch.setattr("agentx.config.llm.secret_env_path", lambda: tmp_path / ".env")
    from agentx.config.llm import write_secret_env

    write_secret_env({"OPENAI_API_KEY": "sk-custom-gw"}, tmp_path / ".env")
    cfg = AgentXConfig(
        llm={"provider": "compatible", "model": "local-model", "base_url": "http://gw:8080/v1"},
        api_key="sk-legacy",  # legacy key 不应干扰 llm 段（provider 非 openai）
    )
    resolved = resolve_llm(cfg)
    assert resolved["provider"] == "compatible"
    assert resolved["base_url"] == "http://gw:8080/v1"
    assert resolved["api_key"] == "sk-custom-gw"
    assert resolved["key_source"] == "env_file:OPENAI_API_KEY"
    app = _mk_app(tmp_path, cfg)
    try:
        assert isinstance(app.provider, OpenAIProvider)
        assert app.provider_name == "compatible"
        assert app.base_url == "http://gw:8080/v1"
        assert app.api_key == "sk-custom-gw"
    finally:
        app.store.close()


def test_resolved_llm_fields_shape() -> None:
    """ResolvedLLMConfig 至少包含 provider/model/base_url/api_key/api_key_env。"""
    cfg = AgentXConfig()
    resolved = resolve_llm(cfg)
    required = (
        "provider",
        "model",
        "base_url",
        "api_key",
        "api_key_env",
        "key_source",
        "configured",
    )
    for key in required:
        assert key in resolved, f"missing {key}"
    assert resolved["api_key_env"] == PROVIDER_PRESETS["openai"]["api_key_env"]


def test_mcp_path_same_entry_point(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, iso_env: None
) -> None:
    """MCP server 走 Application，即 resolve_llm 链路（monkeypatch load_config）。"""
    from agentx.mcp.server import _app as mcp_app

    cfg = AgentXConfig(llm={"provider": "deepseek", "model": "deepseek-chat"})
    monkeypatch.setattr("agentx.mcp.server.load_config", lambda: cfg)
    os.environ["DEEPSEEK_API_KEY"] = "sk-mcp-deepseek"
    app = mcp_app(str(tmp_path / "proj"))
    try:
        assert isinstance(app.provider, OpenAIProvider)
        assert app.provider_name == "deepseek"
        assert app.api_key == "sk-mcp-deepseek"
        assert app.base_url == PROVIDER_PRESETS["deepseek"]["base_url"]
    finally:
        app.store.close()
