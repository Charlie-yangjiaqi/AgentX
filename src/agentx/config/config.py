"""AgentX 配置：~/.agentx/config.json 持久化。

优先级：环境变量 > 配置文件 > 默认值。
API Key 建议用环境变量或系统密钥库；配置文件仅保存引用与常规选项。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from agentx.state.models import AgentXModel

CONFIG_FILENAME = "config.json"

DEFAULT_BASE_URL = "https://api.deepseek.com/v1"
DEFAULT_MODEL = "deepseek-v4-flash"


class AgentModelConfig(AgentXModel):
    """单个 Agent 角色的模型/Key 覆盖配置。

    配置了 api_key 的角色使用独立 Provider（如 plan 用 Key A、review 用 Key B）；
    只配置 model 的角色复用全局 Provider，仅换模型。
    """

    provider: str | None = None
    model: str | None = None
    api_key: str | None = None
    base_url: str | None = None


class AgentXConfig(AgentXModel):
    api_key: str | None = None
    base_url: str | None = DEFAULT_BASE_URL
    model: str | None = DEFAULT_MODEL
    no_proxy: bool = True
    permission_mode: str = "review"  # review | auto
    model_source: str = "agentx"  # agentx（自有 API）| reasonix（宿主模型）
    fallback_provider: str | None = None
    generation: dict[str, Any] = {}
    agents: dict[str, AgentModelConfig] = {}
    # Phase 7.8.1：semantic 稳定性配置（可选，默认值兜底）
    semantic: dict[str, Any] = {}
    # Phase 8.2：Index Freshness 阈值配置（source_large_files/scope_impact_*/build_impact_*）
    freshness: dict[str, Any] = {}
    # LLM Provider 配置（agentx config api；key 存 ~/.agentx/.env，此处只存引用）
    llm: dict[str, Any] = {}


def default_config_path() -> Path:
    return Path.home() / ".agentx" / CONFIG_FILENAME


def load_config(path: Path | None = None) -> AgentXConfig:
    p = path or default_config_path()
    if not p.exists():
        return AgentXConfig()
    try:
        # utf-8-sig 兼容 PowerShell/记事本写入的 BOM
        return AgentXConfig.model_validate_json(p.read_text(encoding="utf-8-sig"))
    except Exception:
        return AgentXConfig()


def save_config(cfg: AgentXConfig, path: Path | None = None) -> Path:
    p = path or default_config_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(cfg.model_dump_json(indent=2), encoding="utf-8")
    return p


def resolve_api_key(cfg: AgentXConfig, override: str | None = None) -> str | None:
    return override or os.environ.get("OPENAI_API_KEY") or cfg.api_key


def resolve_base_url(cfg: AgentXConfig) -> str:
    return os.environ.get("OPENAI_BASE_URL") or cfg.base_url or DEFAULT_BASE_URL


def resolve_model(cfg: AgentXConfig, override: str | None = None) -> str:
    return override or os.environ.get("AGENTX_MODEL") or cfg.model or DEFAULT_MODEL


def resolve_no_proxy(cfg: AgentXConfig) -> bool:
    env = os.environ.get("AGENTX_NO_PROXY")
    if env is not None:
        return env != "0"
    return cfg.no_proxy


def resolve_permission_mode(cfg: AgentXConfig, override: str | None = None) -> str:
    return override or cfg.permission_mode or "review"


def resolve_model_source(cfg: AgentXConfig) -> str:
    return cfg.model_source or "agentx"


def resolve_semantic_config(cfg: AgentXConfig | None = None) -> dict[str, Any]:
    """semantic 稳定性配置：配置文件 > 环境变量 > 默认值。

    返回 {"max_file_size_mb": float, "worker_mode": bool, "worker_timeout_seconds": float}
    worker_mode 默认 true（Phase 7.8.2：MCP server 长期运行，native crash 不允许
    影响主进程）。
    """
    raw: dict[str, Any] = {}
    if cfg is not None:
        raw = dict(cfg.semantic or {})

    def _env_float(name: str) -> float | None:
        v = os.environ.get(name)
        if v is None:
            return None
        try:
            return float(v)
        except ValueError:
            return None

    max_mb = _env_float("AGENTX_SEMANTIC_MAX_FILE_SIZE_MB")
    if max_mb is None:
        try:
            max_mb = float(raw.get("max_file_size_mb", 5.0))
        except (TypeError, ValueError):
            max_mb = 5.0

    worker_mode = raw.get("worker_mode", True)
    if os.environ.get("AGENTX_SEMANTIC_WORKER_MODE") is not None:
        worker_mode = os.environ.get("AGENTX_SEMANTIC_WORKER_MODE") != "0"

    timeout = _env_float("AGENTX_SEMANTIC_WORKER_TIMEOUT_SECONDS")
    if timeout is None:
        timeout = _env_float("AGENTX_SEMANTIC_TIMEOUT_SECONDS")  # 旧环境变量兼容
    if timeout is None:
        # 兼容旧键名 timeout_seconds
        timeout_raw = raw.get("worker_timeout_seconds", raw.get("timeout_seconds", 30.0))
        try:
            timeout = float(timeout_raw)
        except (TypeError, ValueError):
            timeout = 30.0

    return {
        "max_file_size_mb": max(0.1, max_mb),
        "worker_mode": bool(worker_mode),
        "worker_timeout_seconds": max(1.0, timeout),
    }


def resolve_agent_model(cfg: AgentXConfig, role: str) -> tuple[str | None, str | None]:
    """per-agent 模型覆盖：(provider_ref, model)。未配置则 None。"""
    agent_cfg = cfg.agents.get(role)
    if agent_cfg is None:
        return None, None
    return agent_cfg.provider, agent_cfg.model


def resolve_agent_provider_cfg(
    cfg: AgentXConfig, role: str
) -> tuple[str | None, str | None, str | None]:
    """per-agent 独立 Provider 配置：(api_key, base_url, model)。

    只有 api_key 配置了才会为该角色创建独立 Provider。
    """
    agent_cfg = cfg.agents.get(role)
    if agent_cfg is None or not agent_cfg.api_key:
        return None, None, None
    return agent_cfg.api_key, agent_cfg.base_url, agent_cfg.model
