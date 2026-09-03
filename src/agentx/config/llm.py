"""LLM Provider 配置模块（CLI 配置入口的后端）。

- PROVIDER_PRESETS：OpenAI / Anthropic / DeepSeek / OpenAI Compatible 预设
- Secret 管理：API Key 不写入 config.json，保存到 ~/.agentx/.env
  （config.json 只存 api_key_env 引用，环境变量优先级最高）
- resolve_llm：环境变量 > ~/.agentx/.env > 配置文件 > provider 默认
- test_llm_connection：OpenAI Compatible 协议连通性测试（GET /models）
"""

from __future__ import annotations

import contextlib
import time
from pathlib import Path
from typing import Any

import httpx

from agentx.state.models import AgentXModel

# Provider 预设：base_url / api_key_env / 默认 model（OpenAI Compatible 协议）
PROVIDER_PRESETS: dict[str, dict[str, str]] = {
    "openai": {
        "base_url": "https://api.openai.com/v1",
        "api_key_env": "OPENAI_API_KEY",
        "model": "gpt-4o-mini",
    },
    "anthropic": {
        "base_url": "https://api.anthropic.com/v1",
        "api_key_env": "ANTHROPIC_API_KEY",
        "model": "claude-sonnet-4-6",
    },
    "deepseek": {
        "base_url": "https://api.deepseek.com/v1",
        "api_key_env": "DEEPSEEK_API_KEY",
        "model": "deepseek-chat",
    },
    "compatible": {
        "base_url": "",
        "api_key_env": "OPENAI_API_KEY",
        "model": "",
    },
}

PROVIDER_NAMES = {  # 显示名
    "openai": "OpenAI",
    "anthropic": "Anthropic",
    "deepseek": "DeepSeek",
    "compatible": "OpenAI Compatible API",
}

SECRET_FILENAME = ".env"


class LLMConfig(AgentXModel):
    """配置文件的 llm 段（不含明文 key，只存 env 引用）。"""

    provider: str = "openai"
    model: str | None = None
    base_url: str | None = None
    api_key_env: str | None = None


def secret_env_path() -> Path:
    """~/.agentx/.env：API Key 等 secret 的存放位置（不入 config.json）。"""
    return Path.home() / ".agentx" / SECRET_FILENAME


def read_secret_env(path: Path | None = None) -> dict[str, str]:
    """读取 .env（KEY=VALUE 行，跳过 # 注释）。"""
    p = path or secret_env_path()
    out: dict[str, str] = {}
    if not p.is_file():
        return out
    try:
        for line in p.read_text(encoding="utf-8-sig").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            out[key.strip()] = value.strip()
    except OSError:
        pass
    return out


def write_secret_env(updates: dict[str, str], path: Path | None = None) -> Path:
    """写入 .env：保留已有条目，更新/追加指定 key。"""
    p = path or secret_env_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    env = read_secret_env(p)
    env.update(updates)
    lines = [f"{k}={v}" for k, v in sorted(env.items())]
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return p


def _resolve_api_key(cfg: Any, llm: LLMConfig) -> tuple[str | None, str | None]:
    """API Key 解析：(key, source)。优先级：环境变量 > ~/.agentx/.env > 配置文件。"""
    env_name = llm.api_key_env or PROVIDER_PRESETS.get(llm.provider, {}).get("api_key_env", "")
    if env_name:
        import os

        if os.environ.get(env_name):
            return os.environ[env_name], f"env:{env_name}"
        env_file = read_secret_env()
        if env_file.get(env_name):
            return env_file[env_name], f"env_file:{env_name}"
    # 兼容：旧全局字段（明文，不推荐）——仅限默认 openai provider
    legacy = getattr(cfg, "api_key", None)
    if legacy and llm.provider == "openai":
        return legacy, "config(legacy)"
    return None, None


def resolve_llm(cfg: Any = None) -> dict[str, Any]:
    """解析生效的 LLM 配置。

    返回 {"provider", "model", "base_url", "api_key", "api_key_env",
           "key_source", "configured"}。configured = key 与 base_url 均可用。
    """
    from agentx.config.config import load_config

    if cfg is None:
        cfg = load_config()
    raw = dict(getattr(cfg, "llm", None) or {})
    llm = LLMConfig.model_validate(raw) if raw else LLMConfig()

    preset = PROVIDER_PRESETS.get(llm.provider, PROVIDER_PRESETS["compatible"])
    # Legacy 兼容：旧配置（cfg.api_key/base_url/model，无 llm 段）继续生效。
    # 仅限默认 openai provider，且必须存在 legacy key（否则视为全新用户，用预设）。
    is_legacy = llm.provider == "openai" and bool(getattr(cfg, "api_key", None))
    legacy_base = cfg.base_url if is_legacy else None
    legacy_model = cfg.model if is_legacy else None
    model = llm.model or legacy_model or preset["model"]
    base_url = llm.base_url or legacy_base or preset["base_url"]
    api_key, key_source = _resolve_api_key(cfg, llm)

    configured = bool(api_key) and bool(base_url)
    return {
        "provider": llm.provider,
        "provider_name": PROVIDER_NAMES.get(llm.provider, llm.provider),
        "model": model,
        "base_url": base_url,
        "api_key": api_key,
        "api_key_env": llm.api_key_env or preset["api_key_env"],
        "key_source": key_source,
        "configured": configured,
    }


def test_llm_connection(resolved: dict[str, Any], timeout: float = 15.0) -> dict[str, Any]:
    """OpenAI Compatible 连通性测试：GET {base_url}/models。

    返回 {"ok", "status", "latency_ms", "detail"}。失败 detail 给出明确原因。
    """
    base = (resolved.get("base_url") or "").rstrip("/")
    key = resolved.get("api_key")
    model = resolved.get("model") or ""
    if not base:
        return {
            "ok": False,
            "status": None,
            "latency_ms": None,
            "detail": "Base URL 未配置（运行 agentx config api）",
        }
    if not key:
        return {
            "ok": False,
            "status": None,
            "latency_ms": None,
            "detail": "API Key 缺失（运行 agentx config api，或设置环境变量）",
        }
    start = time.perf_counter()
    try:
        from agentx.http import with_default_user_agent

        with httpx.Client(timeout=timeout, trust_env=True) as client:
            resp = client.get(
                f"{base}/models",
                headers=with_default_user_agent({"Authorization": f"Bearer {key}"}),
            )
    except httpx.TimeoutException:
        return {
            "ok": False,
            "status": None,
            "latency_ms": None,
            "detail": f"连接超时（{timeout}s）：{base}",
        }
    except httpx.HTTPError as e:
        return {
            "ok": False,
            "status": None,
            "latency_ms": None,
            "detail": f"网络错误：{type(e).__name__}: {e}",
        }
    latency_ms = int((time.perf_counter() - start) * 1000)
    if resp.status_code == 200:
        data: dict[str, Any] = {}
        with contextlib.suppress(Exception):
            data = resp.json()
        models = data.get("data", []) if isinstance(data, dict) else []
        if model and not any(str(m.get("id", "")) == model for m in models if isinstance(m, dict)):
            return {
                "ok": True,
                "status": resp.status_code,
                "latency_ms": latency_ms,
                "detail": f"endpoint 可达，但 model '{model}' 不在 /models 列表",
            }
        return {
            "ok": True,
            "status": resp.status_code,
            "latency_ms": latency_ms,
            "detail": "OK",
        }
    if resp.status_code in (401, 403):
        return {
            "ok": False,
            "status": resp.status_code,
            "latency_ms": latency_ms,
            "detail": f"认证失败（HTTP {resp.status_code}）：API Key 无效或无权访问",
        }
    if resp.status_code == 404:
        return {
            "ok": False,
            "status": resp.status_code,
            "latency_ms": latency_ms,
            "detail": (f"HTTP 404：{base}/models 不存在——该端点可能不是 OpenAI Compatible 服务"),
        }
    return {
        "ok": False,
        "status": resp.status_code,
        "latency_ms": latency_ms,
        "detail": f"HTTP {resp.status_code}: {resp.text[:200]}",
    }
