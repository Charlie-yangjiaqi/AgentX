"""Phase 6：Engineering Planner Upgrade——Impact 数据卡 + Plan 新结构 + 兼容。"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from agentx.index.index import ProjectIndex
from agentx.plan.service import (
    PlanOutput,
    normalize_plan,
    parse_plan,
)
from agentx.understanding.impact import build_impact_data, format_impact_data
from agentx.understanding.query import query_index


def _make_index() -> ProjectIndex:
    return ProjectIndex(
        project_fingerprint="fp",
        index_version="1.3",
        generated_at=datetime(2026, 1, 1, tzinfo=UTC),
        files=[
            {"path": "main.c", "status": "active", "compile_status": "compiled"},
            {"path": "lcd.c", "status": "active", "compile_status": "compiled"},
            {"path": "spi.c", "status": "active", "compile_status": "compiled"},
            {"path": "lcd_test.c", "status": "active", "compile_status": "excluded"},
        ],
        symbols=[
            {"name": "main", "type": "function", "file": "main.c", "start_line": 4, "end_line": 14},
            {
                "name": "LCD_Init",
                "type": "function",
                "file": "lcd.c",
                "start_line": 10,
                "end_line": 24,
            },
            {
                "name": "SPI_Init",
                "type": "function",
                "file": "spi.c",
                "start_line": 5,
                "end_line": 9,
            },
        ],
        call_graph=[
            {
                "caller": "main",
                "callee": "LCD_Init",
                "confidence": "high",
                "file": "main.c",
                "line": 5,
            },
            {
                "caller": "LCD_Init",
                "callee": "SPI_Init",
                "confidence": "high",
                "file": "lcd.c",
                "line": 12,
            },
        ],
        include_map={"main.c": ["lcd.h"], "lcd.c": ["spi.h"]},
        build_info={"system": "makefile", "build_source": "make"},
        project_understanding={
            "critical_files": ["spi.c"],
            "architecture_summary": "s",
            "entry_points": [],
        },
        file_count=4,
    )


# ---------- Impact 数据卡（规则化证据） ----------


def test_impact_data_card_evidence() -> None:
    index = _make_index()
    result = query_index(index, "LCD 初始化")
    data = build_impact_data(index, result)

    syms = {s["name"]: s for s in data["symbols"]}
    # LCD_Init：callers=[main]，compile=compiled
    assert syms["LCD_Init"]["callers"] == ["main"]
    assert syms["LCD_Init"]["compile_status"] == "compiled"
    # SPI_Init 是 indirect（经 LCD_Init 带出），且 critical（understanding）
    assert "SPI_Init" in syms
    assert syms["SPI_Init"]["critical"] is True

    files = {f["path"]: f for f in data["files"]}
    assert files["spi.c"]["critical"] is True
    # lcd_test.c 命中（文件名匹配）但 compile_status=excluded —— 证据明确它不是主要目标
    assert files["lcd_test.c"]["compile_status"] == "excluded"

    # 依赖链：main → LCD_Init（direct）、LCD_Init → SPI_Init（带出）
    chain = {(e["from"], e["to"]): e for e in data["dependency_chain"]}
    assert chain[("main", "LCD_Init")]["impact"] == "direct"


def test_format_impact_data_readable() -> None:
    index = _make_index()
    result = query_index(index, "LCD")
    text = format_impact_data(build_impact_data(index, result))
    assert "影响分析证据" in text
    assert "compile=" in text
    assert "critical=" in text


# ---------- Plan 新结构解析 ----------


def test_parse_plan_new_structure() -> None:
    content = (
        '{"summary": "改 LCD 初始化", '
        '"analysis": {"affected_files": ["lcd.c", "spi.c"], '
        '"dependency_chain": [{"from": "main", "to": "LCD_Init", "impact": "direct"}], '
        '"risk": "spi.c 被多模块依赖，修改 LCD_Init 可能影响 SPI 初始化"}, '
        '"implementation_steps": [{"step": 1, "file": "lcd.c", "change": "修改初始化顺序", '
        '"reason": "main -> LCD_Init 直接调用"}], '
        '"validation": {"commands": ["gcc -o main main.c lcd.c spi.c"], '
        '"expected_result": "编译通过并运行成功"}, '
        '"execution_context": {"goal": "改 LCD", "allowed_files": ["lcd.c"], '
        '"forbidden_files": ["spi.c"], "change_strategy": "最小改动", '
        '"validation_commands": ["gcc -o main main.c lcd.c spi.c"]}}'
    )
    plan = parse_plan(content)
    assert plan is not None
    assert plan.analysis.affected_files == ["lcd.c", "spi.c"]
    assert plan.analysis.dependency_chain[0]["from"] == "main"
    assert plan.analysis.dependency_chain[0]["impact"] == "direct"
    assert plan.implementation_steps[0].file == "lcd.c"
    assert plan.validation.commands == ["gcc -o main main.c lcd.c spi.c"]
    assert plan.execution_context.forbidden_files == ["spi.c"]
    assert plan.execution_context.validation_commands == ["gcc -o main main.c lcd.c spi.c"]


def test_normalize_plan_old_format() -> None:
    old = PlanOutput(
        summary="s",
        steps=[{"action": "a", "files": ["param.c"]}],
        files_involved=["param.c"],
        risks=["API 兼容"],
        verification="gcc -o main main.c param.c",
    )
    normalized = normalize_plan(old, goal="任务")
    assert normalized is not None
    # 旧字段 → 新结构
    assert normalized.analysis.affected_files == ["param.c"]
    assert "API 兼容" in normalized.analysis.risk
    assert normalized.validation.commands == ["gcc -o main main.c param.c"]
    assert normalized.execution_context.goal == "任务"
    assert normalized.execution_context.allowed_files == ["param.c"]
    assert normalized.execution_context.validation_commands == ["gcc -o main main.c param.c"]


def test_load_plan_normalizes_old(tmp_path: Path) -> None:
    (tmp_path / ".agentx").mkdir()
    (tmp_path / ".agentx" / "plan.json").write_text(
        '{"summary": "s", "files_involved": ["a.c"], "risks": ["r"], "verification": "echo ok"}',
        encoding="utf-8",
    )
    from agentx.plan.service import load_plan

    plan = load_plan(tmp_path)
    assert plan is not None
    assert plan.analysis.affected_files == ["a.c"]
    assert plan.validation.commands == ["echo ok"]


def test_verify_uses_validation_commands(tmp_path: Path) -> None:
    """Verify 从 validation.commands 取验证命令（Phase 6 兼容）。"""
    import asyncio

    from agentx.app.application import Application
    from agentx.plan.service import PlanService
    from agentx.providers.mock import MockProvider, text_response
    from agentx.verify.service import VerifyService
    from tests.helpers import EXPLORE_RESPONSE

    (tmp_path / "main.c").write_text("int main(void) { return 0; }\n", encoding="utf-8")
    app = Application(tmp_path)
    app.orchestrator.agents["plan"].provider = MockProvider().respond(
        text_response(EXPLORE_RESPONSE),
        text_response(
            '{"summary": "s", "analysis": {"affected_files": ["main.c"]}, '
            '"implementation_steps": [], '
            '"validation": {"commands": ["echo build-ok"], "expected_result": "ok"}, '
            '"execution_context": {}}'
        ),
    )
    asyncio.run(PlanService(app).plan("任务"))

    result = asyncio.run(VerifyService(app).verify("任务"))
    assert result["verdict"] == "PASS"
    assert result["tests"][0]["command"] == "echo build-ok"
