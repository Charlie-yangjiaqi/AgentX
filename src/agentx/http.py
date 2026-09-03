"""统一 HTTP header 构建（outbound User-Agent 规范）。

规则：
- 请求无 User-Agent → 自动添加 AgentX/<version>
- 已有（用户自定义 / provider preset / 兼容模式）→ 保持不覆盖
"""

from __future__ import annotations

from typing import Any


def agentx_version() -> str:
    """当前 AgentX 版本（importlib.metadata；缺失时兜底）。"""
    from importlib import metadata

    try:
        return metadata.version("agentx")
    except metadata.PackageNotFoundError:
        return "0.1.0"


def default_user_agent() -> str:
    return f"AgentX/{agentx_version()}"


def with_default_user_agent(headers: dict[str, Any] | None = None) -> dict[str, Any]:
    """返回补齐 User-Agent 的 headers（不修改入参）；已有 UA 不覆盖。"""
    out: dict[str, Any] = dict(headers) if headers else {}
    if not any(str(k).lower() == "user-agent" for k in out):
        out["User-Agent"] = default_user_agent()
    return out
