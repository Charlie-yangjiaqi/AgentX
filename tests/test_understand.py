"""Phase 3：Core Path Understanding——入口发现（规则化）+ 理解生命周期。"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from agentx.app.application import Application
from agentx.index.index import ProjectIndex, index_status, load_index
from agentx.plan.service import PlanService
from agentx.providers.mock import MockProvider, text_response
from agentx.understanding.understand import (
    _parse_understanding,
    discover_entry_candidates,
    ensure_understanding,
    understanding_hits_goal,
    understanding_status,
)
from tests.helpers import EXPLORE_RESPONSE


def _make_index() -> ProjectIndex:
    return ProjectIndex(
        project_fingerprint="fp",
        index_version="1.3",
        generated_at=datetime(2026, 1, 1, tzinfo=UTC),
        files=[
            {"path": "main.c", "status": "active"},
            {"path": "startup_stm32.c", "status": "active"},
            {"path": "Drivers/BSP/LCD/lcd.c", "status": "active"},
            {"path": "app/tasks.c", "status": "active"},
            {"path": "demo/example.c", "status": "active"},
        ],
        symbols=[
            {"name": "main", "type": "function", "file": "main.c", "start_line": 4, "end_line": 14},
            {
                "name": "startup_stm32",
                "type": "function",
                "file": "startup_stm32.c",
                "start_line": 1,
                "end_line": 50,
            },
            {
                "name": "osThreadNew",
                "type": "function",
                "file": "app/tasks.c",
                "start_line": 2,
                "end_line": 3,
            },
            {
                "name": "app_main",
                "type": "function",
                "file": "app/tasks.c",
                "start_line": 5,
                "end_line": 9,
            },
        ],
        call_graph=[
            {
                "caller": "app_main",
                "callee": "osThreadNew",
                "confidence": "high",
                "file": "app/tasks.c",
                "line": 6,
            },
        ],
        build_info={
            "system": "makefile",
            "compiled_files": [{"file": "main.c"}, {"file": "startup_stm32.c"}],
        },
        file_count=5,
    )


def _make_c_project(tmp_path: Path) -> None:
    (tmp_path / "main.c").write_text("int main(void) { return 0; }\n", encoding="utf-8")
    (tmp_path / "param.c").write_text("// TODO\n", encoding="utf-8")
    (tmp_path / "param.h").write_text("#ifndef P\n#define P\n#endif\n", encoding="utf-8")


# ---------- 入口发现（规则化、零 LLM） ----------


def test_discover_entry_main_and_startup() -> None:
    cands = discover_entry_candidates(_make_index())
    pairs = {(c.file, c.symbol): c for c in cands}
    assert ("main.c", "main") in pairs
    assert pairs[("main.c", "main")].confidence == "high"
    assert ("startup_stm32.c", "startup_stm32") in pairs
    # 顺序：high 在前
    confs = [c.confidence for c in cands]
    assert confs == sorted(confs)


def test_discover_entry_rtos_task_creator() -> None:
    cands = discover_entry_candidates(_make_index())
    # osThreadNew 的调用者 app_main 被标记为任务创建点
    assert any(c.file == "app/tasks.c" and c.symbol == "app_main" for c in cands)


def test_discover_entry_build_cross_validation() -> None:
    index = _make_index()
    # app_main 经 RTOS 边 medium；构建交叉：app/tasks.c 不在编译集 → 不上调
    cands = discover_entry_candidates(index)
    rtos = [c for c in cands if c.symbol == "app_main"]
    assert rtos and rtos[0].confidence == "medium"


# ---------- 理解生命周期 ----------


def test_understanding_status_missing_stale_valid() -> None:
    index = _make_index()
    ok, reason = understanding_status(index, "fp")
    assert not ok and "未建立" in reason
    index.project_understanding = {
        "architecture_summary": "s",
        "based_on_fingerprint": "fp",
        "source": "llm",
    }
    ok, reason = understanding_status(index, "fp")
    assert ok and "有效" in reason
    ok, reason = understanding_status(index, "changed-fp")
    assert not ok and "过期" in reason


def test_understanding_hits_goal_by_file() -> None:
    index = _make_index()
    index.project_understanding = {
        "core_modules": ["Drivers/BSP/LCD/lcd.c"],
        "critical_files": [],
        "startup_flow": [],
        "entry_points": [],
    }
    assert understanding_hits_goal(index, "修改 LCD 初始化")
    assert not understanding_hits_goal(index, "网络协议栈")


def test_parse_understanding_handles_noise() -> None:
    u = _parse_understanding(
        '好的，分析如下：\n{"architecture_summary": "分层架构", "startup_flow": ["main"], '
        '"core_modules": ["lcd.c"]}'
    )
    assert u["architecture_summary"] == "分层架构"
    assert u["startup_flow"] == ["main"]
    assert u["critical_files"] == []
    assert _parse_understanding("完全无法解析") == {
        "architecture_summary": "",
        "startup_flow": [],
        "core_modules": [],
        "critical_files": [],
    }


# ---------- A+B 混合策略集成 ----------


@pytest.mark.asyncio
async def test_ensure_understanding_creates_once_then_reuses(tmp_path: Path) -> None:
    _make_c_project(tmp_path)
    app = Application(tmp_path)
    app.orchestrator.agents["plan"].provider = MockProvider().respond(
        text_response(EXPLORE_RESPONSE),
        text_response(EXPLORE_RESPONSE),
        text_response('{"summary": "s", "steps": [], "files_involved": [], "risks": []}'),
    )
    await PlanService(app).plan("任务一")
    index = load_index(tmp_path)
    assert index is not None and index.project_understanding is not None
    assert index.project_understanding["architecture_summary"] == "test project"
    assert "based_on_fingerprint" in index.project_understanding
    # filescan 降级（无符号）→ 入口候选为空是正确行为

    # 第二次：理解有效 → reused，不重新探索
    result = await ensure_understanding(app, tmp_path, goal="任务二")
    assert result["status"] == "reused"


@pytest.mark.asyncio
async def test_ensure_understanding_force_refresh(tmp_path: Path) -> None:
    _make_c_project(tmp_path)
    app = Application(tmp_path)
    app.orchestrator.agents["plan"].provider = MockProvider().respond(
        text_response(EXPLORE_RESPONSE),
        text_response('{"summary": "s", "steps": [], "files_involved": [], "risks": []}'),
        text_response(EXPLORE_RESPONSE),
    )
    await PlanService(app).plan("任务一")
    result = await ensure_understanding(app, tmp_path, force=True)
    assert result["status"] == "refreshed"


@pytest.mark.asyncio
async def test_ensure_understanding_stale_skipped_when_unrelated(tmp_path: Path) -> None:
    _make_c_project(tmp_path)
    app = Application(tmp_path)
    app.orchestrator.agents["plan"].provider = MockProvider().respond(
        text_response(EXPLORE_RESPONSE),
        text_response('{"summary": "s", "steps": [], "files_involved": [], "risks": []}'),
    )
    await PlanService(app).plan("任务一")
    # 改文件 → 指纹变化 → 理解过期
    (tmp_path / "param.c").write_text("int changed;\n", encoding="utf-8")
    status, _ = index_status(tmp_path)
    assert status.value == "STALE"
    # 无关任务：跳过刷新
    result = await ensure_understanding(app, tmp_path, goal="完全无关的网络任务")
    assert result["status"] == "skipped"
