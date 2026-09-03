"""Phase 7：Project Knowledge Query Layer（统一查询层）。

- symbol query：definition/callers（文件级）/callees/related_files
- architecture query：Evidence Flow（事实链，不解释）
- MCP action=query（query_type 分发）+ search_feature 兼容
- 共享底座：feature/symbol/architecture 同一查询逻辑
- Evidence Card [Driver]/[Consumer] 分组
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from agentx.index.index import ProjectIndex
from agentx.query.architecture import search_architecture
from agentx.query.evidence import build_evidence_card, format_flow
from agentx.query.feature import search_feature
from agentx.query.symbol import search_symbol


def _index(**overrides: Any) -> ProjectIndex:
    files = [
        {"path": "Drivers/BSP/KEY/key.c", "status": "active", "compile_status": "compiled"},
        {"path": "Drivers/BSP/KEY/key.h", "status": "active", "compile_status": "compiled"},
        {"path": "App/lv_shelf.c", "status": "active", "compile_status": "compiled"},
        {"path": "User/main.c", "status": "active", "compile_status": "compiled"},
        {"path": "Drivers/UART/uart.c", "status": "active", "compile_status": "compiled"},
        {"path": "App/uart_protocol_adapter.c", "status": "active", "compile_status": "compiled"},
        {"path": "App/app_controller.c", "status": "active", "compile_status": "compiled"},
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
            "name": "WKUP_PRES",
            "type": "macro",
            "file": "Drivers/BSP/KEY/key.h",
            "start_line": 6,
            "end_line": 6,
        },
        {
            "name": "lv_shelf_key_handler",
            "type": "function",
            "file": "App/lv_shelf.c",
            "start_line": 20,
            "end_line": 40,
        },
        {
            "name": "main",
            "type": "function",
            "file": "User/main.c",
            "start_line": 1,
            "end_line": 50,
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
            "caller": "main",
            "callee": "key_scan",
            "confidence": "high",
            "file": "User/main.c",
            "line": 5,
        },
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
    include_map = {"key.c": ["key.h"], "lv_shelf.c": ["key.h"], "main.c": ["key.h"]}
    build_info = {
        "system": "keil",
        "build_source": "keil",
        "target": "GD32F427",
        "has_build_config": True,
    }
    understanding = {
        "startup_flow": ["main", "key_scan"],
        "entry_points": [{"file": "User/main.c", "symbol": "main"}],
        "core_modules": ["Drivers/BSP/KEY/key.c"],
        "critical_files": ["Drivers/BSP/KEY/key.c"],
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
        "project_understanding": understanding,
        "file_count": len(files),
    }
    # Phase 7.6：fixture 视为已过语义提取（function/struct/enum/macro）
    kwargs.update(overrides)
    for sym in kwargs["symbols"]:
        if sym.get("type") in ("function", "struct", "enum", "macro"):
            sym["semantic"] = True
    return ProjectIndex(**kwargs)


# ---------- Symbol Query ----------


def test_symbol_query_definition_and_callers() -> None:
    result = search_symbol(_index(), "key_scan")
    assert result["confidence"] == "high"
    assert result["symbol"] == "key_scan"
    assert result["definition"]["file"] == "Drivers/BSP/KEY/key.c"
    assert result["definition"]["start_line"] == 10
    # callers 符号级 + 文件级
    assert result["callers"] == ["main"]
    assert result["caller_files"] == ["User/main.c"]
    # callees
    assert result["callees"] == ["lv_shelf_key_handler"]
    # related_files 包含调用方文件
    assert "User/main.c" in result["related_files"]
    # evidence 事实
    assert any("key_scan defined in" in e for e in result["evidence"])
    assert any("called by main" in e for e in result["evidence"])
    assert result["recommended_action"] == {"type": "answer"}


def test_symbol_query_case_insensitive_and_substring() -> None:
    assert search_symbol(_index(), "KEY_SCAN")["symbol"] == "key_scan"
    assert search_symbol(_index(), "uart_protocol")["symbol"] == "uart_protocol_adapter"


def test_symbol_query_missing() -> None:
    result = search_symbol(_index(), "bluetooth_module")
    assert result["confidence"] == "low"
    assert "no matching symbol" in result["reason"][0]
    assert result["recommended_action"]["type"] == "read_source"


def test_symbol_query_no_relations_reads_source() -> None:
    index = _index(
        symbols=[
            {
                "name": "orphan_func",
                "type": "function",
                "file": "x.c",
                "start_line": 1,
                "end_line": 2,
            }
        ],
        call_graph=[],
    )
    result = search_symbol(index, "orphan_func")
    assert result["confidence"] == "medium"
    assert result["recommended_action"] == {"type": "read_source", "files": ["x.c"]}


# ---------- Architecture Query ----------


def test_architecture_uart_flow() -> None:
    result = search_architecture(_index(), "UART通信流程")
    assert result["confidence"] == "high"
    flow = result["flow"]
    # 事实链：适配器 → 回调 → 控制器
    assert "uart_protocol_adapter" in flow
    assert "uart_callback" in flow
    assert "app_controller_handle_uart" in flow
    # 顺序：上游在前
    assert (
        flow.index("uart_protocol_adapter")
        < flow.index("uart_callback")
        < flow.index("app_controller_handle_uart")
    )
    # evidence 每步有定义文件
    assert any("uart_callback defined in Drivers/UART/uart.c" in e for e in result["evidence"])
    assert result["recommended_action"] == {"type": "answer"}


def test_architecture_startup_flow_uses_understanding() -> None:
    result = search_architecture(_index(), "启动流程")
    assert result["confidence"] == "high"
    flow = result["flow"]
    assert flow and "main" in flow  # understanding.startup_flow
    assert any("startup step" in e for e in result["evidence"])


def test_architecture_flow_format() -> None:
    card = format_flow(
        ["uart_rx_handler", "uart_protocol_adapter", "app_controller"],
        ["uart_rx_handler defined in uart.c"],
    )
    assert "Evidence Flow:" in card
    assert "uart_protocol_adapter" in card
    assert "v" in card  # 箭头
    assert "Evidence:" in card


def test_architecture_missing_topic() -> None:
    result = search_architecture(_index(), "蓝牙通信流程")
    assert result["confidence"] == "low"
    assert "no matching symbols" in result["reason"][0]
    assert result["recommended_action"]["type"] == "read_source"


# ---------- 共享底座：同一查询逻辑 ----------


def test_shared_base_same_symbols_used() -> None:
    """feature/symbol/architecture 都从同一 Index 结构取符号（单一认知来源）。"""
    index = _index()
    f = search_feature(index, "找实体按键作用")
    s = search_symbol(index, "key_scan")
    # 同一符号的事实一致
    assert any(sym["name"] == "key_scan" for sym in f["symbols"])
    assert s["symbol"] == "key_scan"
    assert s["definition"]["file"] == "Drivers/BSP/KEY/key.c"


# ---------- Evidence Card [Driver]/[Consumer] ----------


def test_evidence_card_driver_consumer_groups() -> None:
    index = _index()
    result = search_feature(index, "找实体按键作用")
    card = build_evidence_card(index, result)
    assert "[Driver]" in card
    assert "Provides: key_scan()" in card
    assert "[Consumer]" in card
    # main.c 调用 key_scan（消费）
    assert "Consumes: key_scan" in card
    # lv_shelf 消费 lv_shelf_key_handler 边：key_scan -> lv_shelf_key_handler
    assert "lv_shelf" in card
    assert "Confidence: high" in card


# ---------- MCP action=query 统一入口 ----------


@pytest.mark.asyncio
async def test_mcp_query_feature_type(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
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

    await mcp_server.agentx(str(tmp_path), "任务", action="plan")
    from agentx.index.index import load_index

    index = load_index(tmp_path)
    assert index is not None
    index.symbols = [
        {"name": "key_scan", "type": "function", "file": "key.c", "start_line": 1, "end_line": 2},
        {"name": "main", "type": "function", "file": "main.c", "start_line": 1, "end_line": 5},
    ]
    index.call_graph = [
        {"caller": "main", "callee": "key_scan", "confidence": "high", "file": "main.c", "line": 2}
    ]
    index.build_info = {"system": "makefile", "build_source": "make", "target": None}
    save_index(tmp_path, index)

    # query type=feature
    r = await mcp_server.agentx(str(tmp_path), "实体按键", action="query", query_type="feature")
    assert r["result"]["query_type"] == "feature"
    assert any(s["name"] == "key_scan" for s in r["result"]["symbols"])
    assert r["result"]["evidence_card"] is not None
    assert "Provides: key_scan()" in r["result"]["evidence_card"]

    # query type=symbol
    r2 = await mcp_server.agentx(str(tmp_path), "key_scan", action="query", query_type="symbol")
    assert r2["result"]["query_type"] == "symbol"
    assert r2["result"]["definition"]["file"] == "key.c"
    assert r2["result"]["callers"] == ["main"]

    # query type=architecture
    r3 = await mcp_server.agentx(
        str(tmp_path), "启动流程", action="query", query_type="architecture"
    )
    assert r3["result"]["query_type"] == "architecture"
    assert "flow" in r3["result"]

    # search_feature 兼容
    r4 = await mcp_server.agentx(str(tmp_path), "实体按键", action="search_feature")
    assert any(s["name"] == "key_scan" for s in r4["result"]["symbols"])


@pytest.mark.asyncio
async def test_mcp_query_unknown_type_defaults_to_feature(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import agentx.mcp.server as mcp_server

    (tmp_path / "key.c").write_text("int key_scan(void) { return 1; }\n", encoding="utf-8")
    app = mcp_server._app(str(tmp_path))
    monkeypatch.setattr(mcp_server, "_app", lambda path: app)

    r = await mcp_server.agentx(str(tmp_path), "实体按键", action="query", query_type="weird")
    # 未知 type → feature 兜底；无 Index → error
    assert r["result"]["error"]
    assert "先调用 agentx.plan" in r["result"]["error"]
