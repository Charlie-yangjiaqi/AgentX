"""Phase 7.0：Knowledge First & Feature Query。

Case 1: 实体按键 → key.c/key.h/key_scan + callers + 证据卡（不扫描工程）
Case 2: UART 协议 → 调用链 uart_protocol_adapter → uart callback → app_controller
Case 3: 蓝牙（不存在）→ confidence=low + no matching symbols/files（不编造）
Case 4: VALID Index 查询不触发重建/扫描
+ 符号命中优先、target 提取、Evidence Card 固定格式
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from agentx.index.index import ProjectIndex
from agentx.query.evidence import build_evidence_card
from agentx.query.feature import (
    CONF_HIGH,
    CONF_LOW,
    CONF_MEDIUM,
    NEXT_ANSWER,
    NEXT_READ_SOURCE,
    search_feature,
)


def _index(**overrides: Any) -> ProjectIndex:
    files = [
        {"path": "Drivers/BSP/KEY/key.c", "status": "active", "compile_status": "compiled"},
        {"path": "Drivers/BSP/KEY/key.h", "status": "active", "compile_status": "compiled"},
        {"path": "App/lv_shelf.c", "status": "active", "compile_status": "compiled"},
        {"path": "Drivers/UART/uart.c", "status": "active", "compile_status": "compiled"},
        {"path": "App/uart_protocol_adapter.c", "status": "active", "compile_status": "compiled"},
        {"path": "App/app_controller.c", "status": "active", "compile_status": "compiled"},
        {"path": "Drivers/Display/lcd.c", "status": "active", "compile_status": "compiled"},
    ]
    symbols = [
        {
            "name": "key_scan",
            "type": "function",
            "file": "Drivers/BSP/KEY/key.c",
            "start_line": 10,
            "end_line": 30,
        },
        {
            "name": "KEY0_PRES",
            "type": "macro",
            "file": "Drivers/BSP/KEY/key.h",
            "start_line": 5,
            "end_line": 5,
        },
        {
            "name": "lv_shelf_key_handler",
            "type": "function",
            "file": "App/lv_shelf.c",
            "start_line": 20,
            "end_line": 40,
        },
        {
            "name": "uart_protocol_adapter",
            "type": "function",
            "file": "App/uart_protocol_adapter.c",
            "start_line": 5,
            "end_line": 50,
        },
        {
            "name": "uart_callback",
            "type": "function",
            "file": "Drivers/UART/uart.c",
            "start_line": 15,
            "end_line": 45,
        },
        {
            "name": "app_controller_handle_uart",
            "type": "function",
            "file": "App/app_controller.c",
            "start_line": 60,
            "end_line": 90,
        },
    ]
    call_graph = [
        {
            "caller": "key_scan",
            "callee": "lv_shelf_key_handler",
            "confidence": "high",
            "file": "Drivers/BSP/KEY/key.c",
            "line": 25,
        },
        {
            "caller": "uart_protocol_adapter",
            "callee": "uart_callback",
            "confidence": "high",
            "file": "App/uart_protocol_adapter.c",
            "line": 30,
        },
        {
            "caller": "uart_callback",
            "callee": "app_controller_handle_uart",
            "confidence": "high",
            "file": "Drivers/UART/uart.c",
            "line": 40,
        },
    ]
    include_map = {"key.c": ["key.h"], "lv_shelf.c": ["key.h"]}
    build_info = {
        "system": "keil",
        "build_source": "keil",
        "target": "GD32F427",
        "has_build_config": True,
    }
    kwargs: dict[str, Any] = {
        "project_fingerprint": "fp",
        "index_version": "1.3",
        "generated_at": datetime(2026, 8, 29, tzinfo=UTC),
        "files": files,
        "symbols": symbols,
        "call_graph": call_graph,
        "include_map": include_map,
        "build_info": build_info,
        "file_count": len(files),
    }
    kwargs.update(overrides)
    return ProjectIndex(**kwargs)


# ---------- Case 1: 实体按键 ----------


def test_case1_physical_key_evidence() -> None:
    result = search_feature(_index(), "找实体按键作用")
    assert result["confidence"] == CONF_HIGH
    paths = {f["path"] for f in result["files"]}
    assert "Drivers/BSP/KEY/key.c" in paths
    assert "Drivers/BSP/KEY/key.h" in paths
    assert any(s["name"] == "key_scan" for s in result["symbols"])
    assert any(s["file"] == "Drivers/BSP/KEY/key.c" for s in result["symbols"])
    # 调用链：key_scan → lv_shelf_key_handler
    assert result["call_chain"][0]["from"] == "key_scan"
    assert result["call_chain"][0]["to"] == "lv_shelf_key_handler"
    assert result["call_chain"][0]["type"] == "caller"
    # Build Reality
    assert result["build"]["compile_status"] == "compiled"
    assert result["build"]["target"] == "GD32F427"
    # 证据充分 → answer（枚举对象）
    assert result["recommended_action"] == {"type": NEXT_ANSWER}
    # evidence 事实数组
    assert any("key_scan defined in" in e for e in result["evidence"])
    # summary 非空
    assert result["summary"]


def test_case1_evidence_card_driver_consumer() -> None:
    index = _index()
    result = search_feature(index, "找实体按键作用")
    card = build_evidence_card(index, result)
    assert "Conclusion:" in card
    assert "Evidence:" in card
    # [Driver] 分组：谁提供
    assert "[Driver]" in card
    assert "File: Drivers/BSP/KEY/key.c" in card
    assert "Provides: key_scan()" in card
    # [Consumer] 分组：谁使用
    assert "[Consumer]" in card
    assert "Consumes: lv_shelf_key_handler" in card
    # Build 事实
    assert "compile_status: compiled" in card
    assert "GD32F427" in card
    assert "Confidence: high" in card


# ---------- Case 2: UART 协议调用链 ----------


def test_case2_uart_protocol_chain() -> None:
    result = search_feature(_index(), "找UART协议处理流程")
    chain = result["call_chain"]
    froms = [e["from"] for e in chain]
    [e["to"] for e in chain]
    # uart_protocol_adapter → uart_callback → app_controller_handle_uart
    assert "uart_protocol_adapter" in froms
    assert ("uart_protocol_adapter", "uart_callback") in [(e["from"], e["to"]) for e in chain]
    assert ("uart_callback", "app_controller_handle_uart") in [(e["from"], e["to"]) for e in chain]
    assert result["confidence"] == CONF_HIGH
    assert any(s["name"] == "uart_protocol_adapter" for s in result["symbols"])
    assert "App/uart_protocol_adapter.c" in {f["path"] for f in result["files"]}


# ---------- Case 3: 不存在的功能 ----------


def test_case3_missing_feature_no_hallucination() -> None:
    result = search_feature(_index(), "找蓝牙模块")
    assert result["confidence"] == CONF_LOW
    assert "no matching symbols/files" in result["reason"]
    assert result["files"] == []
    assert result["symbols"] == []
    assert result["summary"] == ""
    assert result["recommended_action"]["type"] == NEXT_READ_SOURCE


def test_case3b_empty_task() -> None:
    result = search_feature(_index(), "")
    assert result["confidence"] == CONF_LOW
    assert "no query keywords" in result["reason"][0]


# ---------- Case 4: 不触发扫描/重建 ----------


def test_case4_search_feature_is_pure_index_query() -> None:
    """search_feature 只接受 ProjectIndex 对象——根本没有文件系统/LLM 调用点。"""
    index = _index()
    before = index.project_fingerprint
    result = search_feature(index, "实体按键")
    assert index.project_fingerprint == before  # 不改 Index
    # 无扫描证据：命中全部来自 Index 结构（file 数 == index.files 数以内）
    assert len(result["files"]) <= len(index.files)


# ---------- 匹配优先级：symbol > file > dependency ----------


def test_symbol_hit_priority_over_file() -> None:
    """符号命中（key_scan）与仅文件命中（lcd.c 场景）置信度分层。"""
    # 只有文件命中（无符号命中）→ medium
    index = _index()
    result = search_feature(index, "lcd 屏幕显示")
    assert result["confidence"] == CONF_MEDIUM
    assert "Drivers/Display/lcd.c" in {f["path"] for f in result["files"]}


def test_dependency_hit_adds_files() -> None:
    """include/dependency：被包含文件纳入 dependency_files。"""
    index = _index()
    result = search_feature(index, "key 头文件")
    dep_paths = {d["path"] for d in result.get("dependency_files", [])}
    assert dep_paths  # key.h 相关依赖出现


# ---------- Build target 提取（Keil/IAR） ----------


def test_keil_target_extraction(tmp_path: Path) -> None:
    from agentx.build import parse_keil_project

    uvprojx = tmp_path / "proj.uvprojx"
    uvprojx.write_text(
        """<Project><Targets><Target>
        <TargetName>GD32F427_FW</TargetName>
        <Cpu>GD32F427</Cpu>
        <Groups><Group><Files>
        <File><FileName>key.c</FileName></File>
        <File><FileName>test_key.c</FileName><IncludeInBuild>0</IncludeInBuild></File>
        </Files></Group></Groups>
        </Target></Targets></Project>""",
        encoding="utf-8",
    )
    project = parse_keil_project(uvprojx)
    assert project.target_name == "GD32F427_FW"
    assert project.target_cpu == "GD32F427"
    compiled = project.active_target.compiled_files if project.active_target else []
    excluded = project.active_target.excluded_files if project.active_target else []
    assert any(f.path == "key.c" for f in compiled)
    assert any(f.path == "test_key.c" for f in excluded)


def test_iar_target_extraction(tmp_path: Path) -> None:
    from agentx.understanding.graph import _parse_iar

    ewp = tmp_path / "proj.ewp"
    ewp.write_text(
        """<project><configuration><Cpu><Name>GD32F427</Name></Cpu></configuration>
        <group><file><name>key.c</name></file></group></project>""",
        encoding="utf-8",
    )
    compiled, excluded, target = _parse_iar(ewp)
    assert target == "GD32F427"
    assert any(f["file"] == "key.c" for f in compiled)


# ---------- MCP action ----------


@pytest.mark.asyncio
async def test_mcp_search_feature_action(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import agentx.mcp.server as mcp_server
    from agentx.index.index import save_index
    from agentx.providers.mock import MockProvider, text_response
    from tests.helpers import EXPLORE_RESPONSE

    (tmp_path / "key.c").write_text("int key_scan(void) { return 1; }\n", encoding="utf-8")
    app = mcp_server._app(str(tmp_path))
    app.orchestrator.agents["plan"].provider = MockProvider().respond(
        text_response(EXPLORE_RESPONSE),
        text_response('{"summary": "s", "files_involved": ["key.c"], "verification": "echo ok"}'),
    )
    monkeypatch.setattr(mcp_server, "_app", lambda path: app)

    # 先建 Index（plan 一次）
    await mcp_server.agentx(str(tmp_path), "任务", action="plan")
    # 再注入符号认知（模拟 CodeGraph 已有结构）
    from agentx.index.index import load_index

    index = load_index(tmp_path)
    assert index is not None
    index.symbols = [
        {"name": "key_scan", "type": "function", "file": "key.c", "start_line": 1, "end_line": 2}
    ]
    index.call_graph = [
        {"caller": "key_scan", "callee": "main", "confidence": "high", "file": "key.c", "line": 1}
    ]
    index.build_info = {"system": "makefile", "build_source": "make", "target": None}
    save_index(tmp_path, index)

    result = await mcp_server.agentx(str(tmp_path), "实体按键实现", action="search_feature")
    assert result["result"]["confidence"] == CONF_HIGH
    assert any(s["name"] == "key_scan" for s in result["result"]["symbols"])
    assert "key.c" in str(result["result"]["files"])
    assert result["result"]["evidence_card"] is not None
    assert "key_scan" in result["result"]["evidence_card"]
    # runtime 仍正常
    assert result["runtime"]["index_state"] == "VALID"


@pytest.mark.asyncio
async def test_mcp_search_feature_no_index(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import agentx.mcp.server as mcp_server

    (tmp_path / "key.c").write_text("int key_scan(void) { return 1; }\n", encoding="utf-8")
    app = mcp_server._app(str(tmp_path))
    monkeypatch.setattr(mcp_server, "_app", lambda path: app)

    result = await mcp_server.agentx(str(tmp_path), "实体按键", action="search_feature")
    assert result["result"]["error"]
    assert "先调用 agentx.plan" in result["result"]["error"]
