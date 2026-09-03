"""Phase 7.6 Query 集成测试。

Case 5: 有语义 Index → symbol query 直接返回 signature/members/value（无需 Read 源码）
Case 6: 旧 Index（无 semantic 标记）→ 不崩、不猜、返回 reindex 建议
Case 8: CodeGraph + Tree-sitter + Build Reality 三者共存
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from agentx.index.index import ProjectIndex
from agentx.query.evidence import format_symbol_card
from agentx.query.symbol import search_symbol


def _index(symbols: list[dict[str, Any]], **overrides: Any) -> ProjectIndex:
    files = [
        {"path": "Drivers/BSP/KEY/key.c", "status": "active", "compile_status": "compiled"},
        {"path": "Drivers/BSP/KEY/key.h", "status": "active", "compile_status": "not_compiled"},
        {"path": "App/lv_shelf.c", "status": "active", "compile_status": "compiled"},
        {"path": "User/main.c", "status": "active", "compile_status": "compiled"},
    ]
    call_graph = [
        {"caller": "lv_shelf_key_handler", "callee": "key_scan", "file": "App/lv_shelf.c"},
        {"caller": "main", "callee": "key_scan", "file": "User/main.c"},
    ]
    kwargs: dict[str, Any] = dict(
        project_fingerprint="fp",
        index_version="1.4",
        generated_at=datetime(2026, 8, 29, tzinfo=UTC),
        file_count=4,
        files=files,
        symbols=symbols,
        call_graph=call_graph,
        build_info={
            "system": "keil",
            "target": "GD32F427",
            "compiled_files": [{"file": "key.c", "compiled": True}],
        },
    )
    kwargs.update(overrides)
    return ProjectIndex(**kwargs)


# ---------- Case 5: 有语义 Index ----------


SEMANTIC_SYMBOLS = [
    {
        "name": "key_scan",
        "type": "function",
        "file": "Drivers/BSP/KEY/key.c",
        "start_line": 37,
        "end_line": 57,
        "semantic": True,
        "signature": {
            "return_type": "uint8_t",
            "parameters": [{"name": "mode", "type": "uint8_t"}],
            "text": "uint8_t key_scan(uint8_t mode)",
        },
    },
    {
        "name": "KEY0_PRES",
        "type": "macro",
        "file": "Drivers/BSP/KEY/key.h",
        "start_line": 18,
        "end_line": 18,
        "semantic": True,
        "value": "1",
    },
    {
        "name": "key_t",
        "type": "struct",
        "file": "Drivers/BSP/KEY/key.h",
        "start_line": 51,
        "end_line": 55,
        "semantic": True,
        "members": [
            {"name": "width", "type": "uint16_t", "line": 52},
            {"name": "height", "type": "uint16_t", "line": 53},
        ],
    },
    {
        "name": "key_event_t",
        "type": "enum",
        "file": "Drivers/BSP/KEY/key.h",
        "start_line": 60,
        "end_line": 64,
        "semantic": True,
        "members": [
            {"name": "KEY_NONE", "value": "0", "line": 61},
            {"name": "KEY_PRESS", "value": "1", "line": 62},
        ],
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
        "end_line": 60,
    },
]


def test_symbol_query_returns_signature() -> None:
    result = search_symbol(_index(SEMANTIC_SYMBOLS), "key_scan")
    definition = result["definition"]
    assert definition["semantic"] is True
    assert definition["signature"]["text"] == "uint8_t key_scan(uint8_t mode)"
    assert definition["signature"]["return_type"] == "uint8_t"
    assert definition["signature"]["parameters"] == [{"name": "mode", "type": "uint8_t"}]
    assert result["callers"] == ["lv_shelf_key_handler", "main"]
    assert result["build"]["compile_status"] == "compiled"
    assert result["recommended_action"]["type"] == "answer"  # 有调用方 + 有语义 → 直接回答


def test_symbol_query_macro_value() -> None:
    result = search_symbol(_index(SEMANTIC_SYMBOLS), "KEY0_PRES")
    definition = result["definition"]
    assert definition["semantic"] is True
    assert definition["type"] == "macro"
    assert definition["value"] == "1"


def test_symbol_query_struct_members() -> None:
    result = search_symbol(_index(SEMANTIC_SYMBOLS), "key_t")
    definition = result["definition"]
    assert definition["semantic"] is True
    assert definition["members"] == [
        {"name": "width", "type": "uint16_t", "line": 52},
        {"name": "height", "type": "uint16_t", "line": 53},
    ]


def test_symbol_query_enum_members() -> None:
    result = search_symbol(_index(SEMANTIC_SYMBOLS), "key_event_t")
    definition = result["definition"]
    assert definition["members"][0]["value"] == "0"
    assert definition["members"][1]["value"] == "1"


def test_symbol_card_shows_semantic() -> None:
    card = format_symbol_card(search_symbol(_index(SEMANTIC_SYMBOLS), "key_scan"))
    assert "Signature: uint8_t key_scan(uint8_t mode)" in card
    assert "GD32F427" in card
    card_macro = format_symbol_card(search_symbol(_index(SEMANTIC_SYMBOLS), "KEY0_PRES"))
    assert "Value: 1" in card_macro


# ---------- Case 6: 旧 Index（无 semantic） ----------


OLD_SYMBOLS = [
    {
        "name": "key_scan",
        "type": "function",
        "file": "Drivers/BSP/KEY/key.c",
        "start_line": 37,
        "end_line": 57,
        "signature": None,
    },
    {
        "name": "KEY0_PRES",
        "type": "macro",
        "file": "Drivers/BSP/KEY/key.h",
        "start_line": 18,
        "end_line": 18,
    },
]


def test_old_index_suggests_reindex_not_guess() -> None:
    index = _index(OLD_SYMBOLS, index_version="1.3")
    result = search_symbol(index, "key_scan")
    # 不崩
    assert result["symbol"] == "key_scan"
    # 不猜：没有 signature 字段
    assert "signature" not in result["definition"]
    # 明确 reindex 建议
    assert result["recommended_action"] == {
        "type": "reindex",
        "reason": "semantic index data unavailable",
    }


def test_old_index_macro_suggests_reindex() -> None:
    index = _index(OLD_SYMBOLS, index_version="1.3")
    result = search_symbol(index, "KEY0_PRES")
    assert result["definition"]["type"] == "macro"
    assert result["recommended_action"]["type"] == "reindex"


def test_old_index_card_hints_reindex() -> None:
    card = format_symbol_card(search_symbol(_index(OLD_SYMBOLS, index_version="1.3"), "key_scan"))
    assert "semantic detail unavailable" in card


# ---------- Case 8: 三者共存（CodeGraph + Tree-sitter + Build Reality） ----------


def test_combined_query_full_evidence_chain() -> None:
    result = search_symbol(_index(SEMANTIC_SYMBOLS), "key_scan")
    # definition + signature + callers + build 全部一次给出
    assert result["definition"]["file"] == "Drivers/BSP/KEY/key.c"
    assert result["definition"]["signature"]["text"] == "uint8_t key_scan(uint8_t mode)"
    assert result["caller_files"] == ["App/lv_shelf.c", "User/main.c"]
    assert result["build"]["compile_status"] == "compiled"
    assert result["build"]["target"] == "GD32F427"
    assert result["recommended_action"]["type"] == "answer"
