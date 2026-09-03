"""共享测试 fixtures。

gate_bypass：Phase 7.8 Decision Guard 放行（旧流程测试兼容——
这些测试验证 plan/index 结构而非决策边界；7.8 专项测试用真实 gate）。

isolate_agentx_home（autouse）：测试套件 hermetic 保证——
把默认配置路径重定向到临时目录，任何测试都读不到开发者机器的真实
~/.agentx/config.json（含真实 API key / reasoning_effort / base_url）。
需要显式配置的测试自行 monkeypatch default_config_path / save_config。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agentx.decision.gate import GateVerdict


@pytest.fixture(autouse=True)
def isolate_agentx_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """隔离 ~/.agentx：默认配置/密钥路径指向空临时目录。"""
    fake_home = tmp_path / "agentx-home"
    monkeypatch.setattr(
        "agentx.config.config.default_config_path",
        lambda: fake_home / "config.json",
    )
    monkeypatch.setattr(
        "agentx.config.llm.secret_env_path",
        lambda: fake_home / ".env",
    )


@pytest.fixture
def gate_bypass(monkeypatch: pytest.MonkeyPatch) -> None:
    """Decision Gate 放行：唯一候选直通（不触发 decision_required）。"""

    def _pass(candidates, index, thresholds=None):  # type: ignore[no-untyped-def]
        return GateVerdict(
            confirm=False, reasons=[], selected=candidates[0] if candidates else None
        )

    monkeypatch.setattr("agentx.decision.gate.evaluate_gate", _pass)
    yield
