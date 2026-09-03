"""Scope Setup Wizard 测试（Phase 7.8 引导层）。

- 无 scope 首次初始化触发 wizard（scope_config_exists=False + 决策函数被调用）
- 用户确认生成配置（全部采纳 / 单项取消 / 手动增加路径）
- 有 scope 不触发（.agentxscope.yaml / .agentxignore 存在 → 跳过）
- cancel 不写文件
- 生成内容与 scope/config.py 解析兼容（不改变 matcher）
"""

from __future__ import annotations

from pathlib import Path

from agentx.scope.config import (
    LEGACY_IGNORE_FILENAME,
    SCOPE_CONFIG_FILENAME,
    parse_scope_config,
    scope_of_file,
)
from agentx.scope.wizard import (
    build_scope_yaml,
    scope_config_exists,
    wizard_result,
    write_scope_config,
)

_SUGGESTIONS = {
    "third_party": [
        {"path": "Middlewares/LVGL", "name": "LVGL", "reason": "第三方库目录特征"},
        {"path": "Middlewares/FreeRTOS", "name": "FreeRTOS", "reason": "第三方库目录特征"},
    ],
    "ignore": [
        {"path": "docs", "reason": "文档目录"},
        {"path": "tools", "reason": "工具目录"},
    ],
}


def test_wizard_triggered_when_no_scope(tmp_path: Path) -> None:
    """无 scope 配置：向导应触发（scope_config_exists=False，决策函数被调用）。"""
    assert scope_config_exists(tmp_path) is False
    decisions: list[str] = []

    def _decide(kind: str, path: str) -> bool:
        decisions.append(path)
        return True

    result = wizard_result(_SUGGESTIONS, _decide)
    assert result is not None
    assert set(decisions) == {
        "Middlewares/LVGL",
        "Middlewares/FreeRTOS",
        "docs",
        "tools",
    }
    assert len(result["third_party"]) == 2
    assert len(result["ignore"]) == 2


def test_confirm_generates_config(tmp_path: Path) -> None:
    """用户确认后：生成 .agentxscope.yaml，且与 matcher 兼容。"""
    chosen = wizard_result(_SUGGESTIONS, lambda _k, _p: True)
    assert chosen is not None
    path = write_scope_config(tmp_path, build_scope_yaml(chosen))
    assert path == tmp_path / SCOPE_CONFIG_FILENAME
    assert path.is_file()
    parsed = parse_scope_config(path.read_text(encoding="utf-8"))
    assert {tp["path"] for tp in parsed["third_party"]} == {
        "Middlewares/LVGL",
        "Middlewares/FreeRTOS",
    }
    assert "docs/**" in parsed["ignore"] and "tools/**" in parsed["ignore"]
    # matcher 行为不变：ignore 优先于 third_party
    assert scope_of_file("docs/readme.md", parsed)[0] == "ignored"
    assert scope_of_file("Middlewares/LVGL/lv_conf.h", parsed) == ("third_party", "LVGL")
    assert scope_of_file("User/main.c", parsed)[0] == "project"


def test_scope_present_skips_wizard(tmp_path: Path) -> None:
    """已有 .agentxscope.yaml：不触发向导。"""
    write_scope_config(tmp_path, build_scope_yaml(wizard_result(_SUGGESTIONS, lambda _k, _p: True)))
    assert scope_config_exists(tmp_path) is True


def test_legacy_ignore_present_skips_wizard(tmp_path: Path) -> None:
    """已有旧 .agentxignore：同样视为已配置（兼容旧项目）。"""
    (tmp_path / LEGACY_IGNORE_FILENAME).write_text("build/**\n", encoding="utf-8")
    assert scope_config_exists(tmp_path) is True


def test_cancel_writes_nothing(tmp_path: Path) -> None:
    """全部取消且无手动路径：不写文件。"""
    chosen = wizard_result(_SUGGESTIONS, lambda _k, _p: False)
    assert chosen is None
    assert not (tmp_path / SCOPE_CONFIG_FILENAME).exists()


def test_partial_cancel_and_manual_add(tmp_path: Path) -> None:
    """单项取消 + 手动增加路径：只包含选中项与手动项。"""
    chosen = wizard_result(
        _SUGGESTIONS,
        lambda k, p: p == "Middlewares/LVGL" or p == "docs",
        extra_ignore=["build", "examples"],
        extra_third_party=["SDK/vendor"],
    )
    assert chosen is not None
    tp = {s["path"] for s in chosen["third_party"]}
    ig = {s["path"] for s in chosen["ignore"]}
    assert tp == {"Middlewares/LVGL", "SDK/vendor"}
    assert ig == {"docs", "build", "examples"}
    # 手动路径规范化（去头尾斜杠/反斜杠转正斜杠）
    chosen2 = wizard_result(
        _SUGGESTIONS, lambda _k, _p: False, extra_ignore=["/build/"], extra_third_party=["\\SDK\\x"]
    )
    assert chosen2 is not None
    assert [s["path"] for s in chosen2["ignore"]] == ["build"]
    assert [s["path"] for s in chosen2["third_party"]] == ["SDK/x"]


def test_empty_suggestions_returns_none() -> None:
    """无建议：向导结果 None（不写文件）。"""
    assert wizard_result({"third_party": [], "ignore": []}, lambda _k, _p: True) is None


def test_manual_only_no_decide_used(tmp_path: Path) -> None:
    """仅手动路径（无建议采纳）：仍生成配置。"""
    called: list[str] = []
    chosen = wizard_result(
        {"third_party": [], "ignore": []},
        lambda k, p: called.append(p) or True,
        extra_ignore=["tmp_gen"],
    )
    assert chosen is not None
    assert [s["path"] for s in chosen["ignore"]] == ["tmp_gen"]
    assert called == []
