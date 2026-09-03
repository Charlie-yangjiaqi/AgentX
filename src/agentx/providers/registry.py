"""Provider 注册表：一个 API Key / 客户端可以被多个 Agent 引用。

"一个 API Key 多 Agent"在实现上不是复制客户端，而是多个 Agent
共享同一个 Provider 实例，各自引用 provider_ref + model。
"""

from __future__ import annotations

from typing import Any


class ProviderRegistry:
    def __init__(self) -> None:
        self._providers: dict[str, Any] = {}

    def register(self, provider: Any) -> None:
        self._providers[provider.name] = provider

    def get(self, name: str) -> Any:
        if name not in self._providers:
            raise KeyError(f"Provider 未注册: {name}")
        return self._providers[name]

    def names(self) -> list[str]:
        return sorted(self._providers)

    def __contains__(self, name: str) -> bool:
        return name in self._providers
