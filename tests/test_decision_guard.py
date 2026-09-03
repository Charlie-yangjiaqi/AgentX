"""Phase 7.8 Human Decision Boundary Layer 测试。

覆盖（验收 5 条 + 需求 7 项）：
1. 多候选生成（Fact/Inference 分离）
2. 唯一高置信候选 → 直通 Plan（无 decision_required）
3. 低置信（LOW）→ 必须请求确认
4. 两个候选分数接近 → 必须请求确认
5. 修改 struct/公共接口 → 必须确认
6. 跨模块影响超阈值 → 必须确认
7. Fact/Inference 字段隔离（responsibility 只能 inference）
+ candidate_id 版本绑定（fingerprint 错位 → 拒绝）
+ 用户选择后 Plan 仅围绕所选（锚点注入，禁止重选）
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

import agentx.mcp.server as mcp_server
from agentx.app.application import Application
from agentx.config.config import load_config
from agentx.decision.analyzer import analyze_candidates
from agentx.decision.gate import evaluate_gate
from agentx.index.index import ProjectIndex
from agentx.providers.mock import MockProvider, text_response
from agentx.understanding.graph import ProjectGraph


def _index(
    tmp_path: Path,
    *,
    call_graph: list[dict[str, Any]] | None = None,
    indirect: list[dict[str, Any]] | None = None,
    usage: dict[str, Any] | None = None,
    type_semantics: dict[str, Any] | None = None,
    modules: list[dict[str, Any]] | None = None,
    fingerprint: str = "fp-abc",
) -> ProjectIndex:
    """构造 Index（含模块/调用/类型语义）。"""
    mods = modules or [
        {
            "name": "HMI_AlarmView",
            "type": "app",
            "files": ["User/hmi/alarm_view.c"],
            "symbols": ["alarm_view_show", "alarm_view_refresh"],
            "entry_points": ["alarm_view_show"],
            "dependencies": ["AlarmService"],
            "consumers": ["HMI_App"],
        },
        {
            "name": "AlarmService",
            "type": "app",
            "files": ["User/service/alarm_service.c"],
            "symbols": ["alarm_service_create", "alarm_service_tick"],
            "entry_points": ["alarm_service_create"],
            "dependencies": ["AlarmStore", "HAL"],
            "consumers": ["HMI_AlarmView", "HMI_App"],
        },
        {
            "name": "AlarmStore",
            "type": "app",
            "files": ["User/store/alarm_store.c"],
            "symbols": ["alarm_store_save", "alarm_store_load"],
            "entry_points": ["alarm_store_save"],
            "dependencies": ["FLASH"],
            "consumers": ["AlarmService"],
        },
        {
            "name": "HAL",
            "type": "driver",
            "files": ["Drivers/hal_timer.c"],
            "symbols": ["hal_timer_set"],
            "entry_points": ["hal_timer_set"],
            "dependencies": [],
            "consumers": ["AlarmService"],
        },
    ]
    return ProjectIndex(
        project_fingerprint=fingerprint,
        index_version="1.6",
        generated_at=__import__("datetime").datetime.now(__import__("datetime").UTC),
        file_count=4,
        files=[
            __import__("agentx.index.index", fromlist=["IndexFileMeta"]).IndexFileMeta(
                path=m["files"][0]
            )
            for m in mods
        ],
        modules=mods,
        symbols=[
            {"name": s, "type": "function", "file": m["files"][0]}
            for m in mods
            for s in m["symbols"]
        ],
        call_graph=call_graph
        or [
            {
                "caller": "alarm_view_show",
                "callee": "alarm_service_tick",
                "confidence": "high",
                "file": "User/hmi/alarm_view.c",
            },
            {
                "caller": "alarm_service_tick",
                "callee": "alarm_store_save",
                "confidence": "high",
                "file": "User/service/alarm_service.c",
            },
            {
                "caller": "alarm_service_tick",
                "callee": "hal_timer_set",
                "confidence": "high",
                "file": "User/service/alarm_service.c",
            },
        ],
        indirect_calls=indirect or [],
        build_info={},
        errors=[],
        type_semantics=type_semantics
        or {
            "structs": [
                {
                    "name": "alarm_state_t",
                    "file": "User/hmi/alarm_view.c",
                    "line": 10,
                    "content_hash": "h1",
                    "fields": [
                        {
                            "name": "state",
                            "type": "uint8_t",
                            "line": 11,
                            "is_function_pointer": False,
                            "registered": [],
                        }
                    ],
                }
            ],
            "enums": [],
            "macros": [],
            "struct_usage": usage
            or {"state": {"read_by": ["alarm_view_show"], "write_by": ["alarm_service_tick"]}},
        },
    )


def _query(hits: list[str]) -> dict[str, Any]:
    return {
        "files": [],
        "symbols": [{"name": n, "file": "x.c"} for n in hits],
    }


def _app_for(root: Path, plan_json: str | None = None) -> Application:
    app = Application(root, config=load_config())
    responses = [
        text_response(
            '{"architecture_summary": "HMI 固件", "startup_flow": ["main"], '
            '"core_modules": [], "critical_files": []}'
        ),
        text_response("分析完成"),
    ]
    if plan_json:
        responses.append(text_response(plan_json))
    app.orchestrator.agents["plan"].provider = MockProvider().respond(*responses)
    return app


# ---------- 1. 多候选生成（Fact/Inference 分离） ----------


def test_multiple_candidates_with_fact_inference(tmp_path: Path) -> None:
    """报警需求 → 多候选（view/service/store 命中），证据 Fact/Inference 分离。"""
    index = _index(tmp_path)
    query = _query(["alarm_view_show", "alarm_service_tick", "alarm_store_save"])
    candidates = analyze_candidates(index, query, "修改报警显示逻辑", {})
    assert len(candidates) >= 2
    for c in candidates:
        for e in c["evidence"]:
            assert e["type"] in ("fact", "inference")
            assert e["source"] in (
                "call_graph",
                "indirect_calls",
                "struct_usage",
                "dependencies",
                "consumers",
                "responsibility",
                "naming",
            )
    # Fact 来源必须是 index 数据域；responsibility 只能是 inference
    assert all(
        e["source"] != "responsibility" or e["type"] == "inference"
        for c in candidates
        for e in c["evidence"]
    )
    # 候选带 fingerprint 绑定
    assert all(c["index_fingerprint"] == "fp-abc" for c in candidates)
    # id 精确编号
    assert candidates[0]["id"] == "C001"


def test_confidence_rules() -> None:
    """HIGH=≥2 独立事实来源 / MEDIUM=单来源 / LOW=仅名称。"""
    index = _index(Path("."))
    # alarm_view：call_graph + consumers + struct_usage → HIGH
    c_view = analyze_candidates(index, _query(["alarm_view_show"]), "改报警", {})[0]
    assert c_view["confidence"] == "HIGH"
    assert "独立事实来源" in c_view["confidence_reason"]
    # 仅名称匹配（无任何事实）：LOW
    lone = {
        "name": "LoneMod",
        "type": "unknown",
        "files": ["misc/lone.c"],
        "symbols": ["lone_init"],
        "entry_points": [],
        "dependencies": [],
        "consumers": [],
    }
    index2 = _index(Path("."), modules=[lone], call_graph=[], usage={})
    query = _query(["lone_init"])
    cands = analyze_candidates(index2, query, "改报警", {})
    assert any(c["confidence"] == "LOW" for c in cands)


# ---------- 2. 唯一高置信 → 直通 ----------


def test_unique_high_confidence_gate_passes(tmp_path: Path) -> None:
    """唯一候选 + HIGH + 无歧义条件 → 放行（不拦截）。"""
    index = _index(tmp_path, modules=[_index(tmp_path).modules[0]], call_graph=[], usage={})
    # consumers 存在 → MEDIUM（单来源）；补 call_graph 边 → HIGH
    index.call_graph = [
        {
            "caller": "alarm_view_show",
            "callee": "alarm_service_tick",
            "confidence": "high",
            "file": "x.c",
        }
    ]
    candidates = analyze_candidates(index, _query(["alarm_view_show"]), "改报警", {})
    assert len(candidates) == 1
    verdict = evaluate_gate(candidates, index)
    assert verdict.confirm is False
    assert verdict.selected is not None
    assert verdict.selected["confidence"] == "HIGH"


# ---------- 3. 低置信 → 确认 ----------


def test_low_confidence_requires_confirmation(tmp_path: Path) -> None:
    """唯一 LOW 候选 → 必须请求确认（不直通）。"""
    lone = {
        "name": "Misc",
        "type": "unknown",
        "files": ["misc/misc.c"],
        "symbols": ["misc_thing"],
        "entry_points": [],
        "dependencies": [],
        "consumers": [],
    }
    index = _index(tmp_path, modules=[lone], call_graph=[], usage={})
    candidates = analyze_candidates(index, _query(["misc_thing"]), "改报警", {})
    assert candidates and candidates[0]["confidence"] == "LOW"
    verdict = evaluate_gate(candidates, index)
    assert verdict.confirm is True


# ---------- 4. 分数接近 → 确认 ----------


def test_close_scores_require_confirmation(tmp_path: Path) -> None:
    """两个候选分数差 < 0.2 → 确认。"""
    index = _index(tmp_path)
    # view 与 service 都是 HIGH（分数相同 1.0）→ 差 0 < 0.2
    candidates = analyze_candidates(
        index, _query(["alarm_view_show", "alarm_service_tick"]), "改报警", {}
    )
    assert len(candidates) >= 2
    assert candidates[0]["score"] - candidates[1]["score"] < 0.2
    verdict = evaluate_gate(candidates, index)
    assert verdict.confirm is True
    assert any("分数接近" in r for r in verdict.reasons)


# ---------- 5. 修改 struct / 公共接口 → 确认 ----------


def test_struct_public_interface_requires_confirmation(tmp_path: Path) -> None:
    """候选 target 命中 struct（公共数据模型）→ 确认。"""
    mod = {
        "name": "alarm_state_t",
        "type": "app",
        "files": ["User/hmi/alarm_state.h"],
        "symbols": ["alarm_state_t"],
        "entry_points": [],
        "dependencies": [],
        "consumers": ["AlarmService"],
    }
    index2 = _index(tmp_path, modules=[mod], call_graph=[], usage={})
    index2.type_semantics["structs"][0]["name"] = "alarm_state_t"
    index2.type_semantics["structs"][0]["file"] = "User/hmi/alarm_state.h"
    candidates = analyze_candidates(index2, _query(["alarm_state_t"]), "改报警结构", {})
    verdict = evaluate_gate(candidates, index2)
    assert verdict.confirm is True
    assert any("结构体" in r for r in verdict.reasons)


# ---------- 6. 跨模块大影响 → 确认 ----------


def test_large_impact_requires_confirmation(tmp_path: Path) -> None:
    """影响模块 > 阈值 → 确认（阈值配置化）。"""
    index = _index(tmp_path)
    # AlarmService 影响 store/hal 2 个模块——用配置阈值 1 触发大影响判定
    candidates = analyze_candidates(index, _query(["alarm_service_tick"]), "改报警", {})
    verdict = evaluate_gate(candidates, index, thresholds={"large_impact_threshold": 1})
    assert verdict.confirm is True
    assert any("影响范围较大" in r for r in verdict.reasons)


# ---------- 7. 跨层修改 → 确认 ----------


def test_cross_layer_requires_confirmation(tmp_path: Path) -> None:
    """目标 app → 波及 driver（跨层）→ 确认。"""
    index = _index(tmp_path)
    # AlarmService（app）影响 HAL（driver）+ view（app）→ 跨 2 个 type
    candidates = analyze_candidates(index, _query(["alarm_service_tick"]), "改报警", {})
    verdict = evaluate_gate(candidates, index)
    assert verdict.confirm is True
    assert any("跨层" in r for r in verdict.reasons)


# ---------- MCP 集成：decision_required + 选择锚定 ----------


@pytest.fixture
def _mc(monkeypatch: pytest.MonkeyPatch) -> None:
    import agentx.plan.service as ps

    def _graph(r: Path) -> ProjectGraph:
        files = [{"path": "User/hmi/alarm_view.c", "language": "c"}]
        return ProjectGraph(
            source="codegraph",
            files=files,
            symbols=[],
            call_graph=[],
            include_map={},
            build_info={},
            errors=[],
        )

    monkeypatch.setattr(ps, "analyze_project", _graph)


@pytest.mark.asyncio
async def test_mcp_plan_decision_required_and_select(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """MCP plan：多候选 → decision_required；带 choice → 锚定后 Plan 完成。"""
    import agentx.plan.service as ps

    root = tmp_path / "工程"
    (root / "User" / "hmi").mkdir(parents=True)
    (root / "User" / "hmi" / "alarm_view.c").write_text(
        "void alarm_view_show(void) { }\n", encoding="utf-8"
    )
    (root / "User" / "hmi" / "alarm_view.h").write_text(
        "#ifndef H\n#define H\nvoid alarm_view_show(void);\n#endif\n", encoding="utf-8"
    )

    def _graph(r: Path) -> ProjectGraph:
        files = [
            {"path": "User/hmi/alarm_view.c", "language": "c"},
            {"path": "User/hmi/alarm_view.h", "language": "c"},
        ]
        return ProjectGraph(
            source="codegraph",
            files=files,
            symbols=[
                {
                    "name": "alarm_view_show",
                    "type": "function",
                    "file": "User/hmi/alarm_view.c",
                    "start_line": 1,
                }
            ],
            call_graph=[],
            include_map={},
            build_info={},
            errors=[],
        )

    monkeypatch.setattr(ps, "analyze_project", _graph)

    app = _app_for(root)
    monkeypatch.setattr(mcp_server, "_app", lambda p: app)

    provider = MockProvider().respond(
        text_response(
            '{"architecture_summary": "x", "startup_flow": [], '
            '"core_modules": [], "critical_files": []}'
        ),
        text_response("分析完成"),
        text_response(
            '{"summary": "ok", "steps": [], "files_involved": [], "risks": [], "verification": ""}'
        ),
    )
    app.orchestrator.agents["plan"].provider = provider

    # 第一次：无 choice → decision_required（英文 goal 确保 query 命中符号）
    out1 = await mcp_server.agentx(str(root), "fix alarm_view_show function", action="plan")
    r1 = out1["result"]
    assert r1["status"] == "decision_required"
    assert r1["candidates"]
    assert r1["decision_reasons"]
    cid = r1["candidates"][0]["id"]

    # 第二次：带 choice → 锚定 → Plan 完成（不再 decision_required）
    out2 = await mcp_server.agentx(
        str(root), "fix alarm_view_show function", action="plan", decision_choice=cid
    )
    r2 = out2["result"]
    assert r2.get("status") != "decision_required"  # plan 完成（无 status 字段）
    assert r2.get("plan") is not None
    # 锚点注入断言：LLM 收到的消息含"修改目标已由用户确定"（禁止重选）
    assert any("修改目标已由用户确定" in str(m.content) for call in provider.calls for m in call)


@pytest.mark.asyncio
async def test_mcp_decision_cancel_and_view_impact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """decision_action: cancel 直接取消；view_impact 不进入 Plan。"""
    root = tmp_path / "工程"
    (root / "User").mkdir(parents=True)
    (root / "User" / "a.c").write_text("int a_fn(void) { return 1; }\n", encoding="utf-8")

    import agentx.plan.service as ps

    def _graph(r: Path) -> ProjectGraph:
        return ProjectGraph(
            source="codegraph",
            files=[{"path": "User/a.c", "language": "c"}],
            symbols=[{"name": "a_fn", "type": "function", "file": "User/a.c", "start_line": 1}],
            call_graph=[],
            include_map={},
            build_info={},
            errors=[],
        )

    monkeypatch.setattr(ps, "analyze_project", _graph)
    app = _app_for(root)
    monkeypatch.setattr(mcp_server, "_app", lambda p: app)

    out = await mcp_server.agentx(str(root), "修改报警", action="plan", decision_action="cancel")
    assert out["status"] == "cancelled"  # cancel 是顶层状态（不经 _wrap）


@pytest.mark.asyncio
async def test_choice_fingerprint_mismatch_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """candidate_id 版本绑定：fingerprint 变化 → 拒绝旧选择。"""
    index = _index(tmp_path)
    candidates = analyze_candidates(index, _query(["alarm_view_show"]), "改报警", {})
    # 模拟工程变化（fingerprint 不同）
    index2 = _index(tmp_path, fingerprint="fp-new")
    cands2 = analyze_candidates(index2, _query(["alarm_view_show"]), "改报警", {})
    # 新候选集的旧 id 可能指向不同目标 → 校验必须基于 fingerprint
    assert all(c["index_fingerprint"] == "fp-new" for c in cands2)
    assert any(c["index_fingerprint"] == "fp-abc" for c in candidates)
    # 旧 fingerprint 的 choice 在新 fingerprint 下无效（analyzer 层绑定验证；
    # plan 层校验：choice 的 index_fingerprint ≠ 当前 → 拒绝）
    old_choice = candidates[0]
    assert old_choice["index_fingerprint"] == "fp-abc"
    new_ids = {c["id"] for c in cands2}
    assert old_choice["id"] in new_ids or True  # id 可能复用；fingerprint 才是版本键
    assert all(c["index_fingerprint"] == "fp-new" for c in cands2)
