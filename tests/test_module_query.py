"""Module Query 层测试（Phase 7.7）。

Case 5: search_module 完整卡片（KEY：files/symbols/entry/consumers/dependencies/build）
Case 6: 普通 symbol 查询自动附带模块
Case 8: 旧 Index（modules 空）→ 不炸、reindex 建议
Case 9: 真实嵌入式风格（GD32 + Keil groups + LVGL）端到端
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from agentx.index.index import ProjectIndex  # noqa: E402
from agentx.module.discover import discover_modules  # noqa: E402
from agentx.module.infer import infer_module_relations  # noqa: E402
from agentx.query.evidence import build_evidence_card, format_symbol_card  # noqa: E402
from agentx.query.module_query import (  # noqa: E402
    format_module_card,
    format_module_view,
    module_of_file,
    module_of_symbol,
    search_module,
)
from agentx.query.symbol import search_symbol  # noqa: E402
from test_module_discovery import _index  # noqa: E402


def _module_index(**overrides: object) -> ProjectIndex:
    """含模块知识的 Index（discover + infer 真实管线）。"""
    idx = _index()
    modules, dependencies = infer_module_relations(discover_modules(idx), idx)
    idx.modules = modules
    idx.dependencies = dependencies
    if overrides:
        for k, v in overrides.items():
            setattr(idx, k, v)
    return idx


# ---------- Case 5: search_module 完整卡片 ----------


def test_module_query_full_card() -> None:
    result = search_module(_module_index(), "KEY")
    assert result["confidence"] == "high"
    assert result["recommended_action"]["type"] == "answer"
    m = result["module"]
    assert m["name"] == "KEY"
    assert m["type"] == "bsp"
    assert m["files"] == ["Drivers/BSP/KEY/key.c", "Drivers/BSP/KEY/key.h"]
    assert m["symbols"] == ["key_init", "key_scan", "key_get_state"]
    assert m["entry_points"] == ["key_init", "key_scan"]
    assert set(m["consumers"]) == {"UI", "MAIN"}
    assert m["dependencies"] == ["GPIO"]
    assert m["build_status"] == "compiled"
    assert "keil_group" not in " ".join(m["evidence"]["basis"])  # 目录证据
    assert any("path:KEY" in b for b in m["evidence"]["basis"])
    card = format_module_card(result)
    assert "Module: KEY (bsp)" in card
    assert "Entry: key_init, key_scan" in card
    assert "Build: compiled (2/2 compiled)" in card


def test_module_query_insensitive_and_substring() -> None:
    assert search_module(_module_index(), "key")["module"]["name"] == "KEY"
    assert search_module(_module_index(), "GpIo")["module"]["name"] == "GPIO"


def test_module_query_missing() -> None:
    result = search_module(_module_index(), "bluetooth")
    assert result["confidence"] == "low"
    assert "no matching module" in result["reason"][0]
    assert result["recommended_action"]["type"] == "read_source"


# ---------- Case 6: 普通查询自动附带模块 ----------


def test_symbol_query_attaches_module() -> None:
    result = search_symbol(_module_index(), "key_scan")
    assert result["module"]["name"] == "KEY"
    card = format_symbol_card(result)
    assert "[Module]" in card
    assert "KEY (bsp)" in card
    assert "Consumers: UI, MAIN" in card


def test_feature_query_attaches_module() -> None:
    from agentx.query.feature import search_feature

    result = search_feature(_module_index(), "按键扫描")
    card = build_evidence_card(_module_index(), result)
    if "[Module]" in card:
        assert "KEY" in card


def test_module_of_primitives() -> None:
    idx = _module_index()
    assert module_of_file(idx, "Drivers/BSP/KEY/key.c")["name"] == "KEY"
    assert module_of_symbol(idx, "key_init")["name"] == "KEY"
    assert module_of_file(idx, "unknown/file.c") is None


# ---------- Case 8: 旧 Index（modules 空）优雅降级 ----------


def test_old_index_no_modules() -> None:
    old = _index()  # modules 默认空（旧 Index 数据）
    assert old.modules == []
    result = search_module(old, "KEY")
    assert result["confidence"] == "low"
    assert result["module"] is None
    assert result["recommended_action"]["type"] == "reindex"
    assert "module knowledge unavailable" in result["recommended_action"]["reason"]
    # 普通查询不受影响
    sym = search_symbol(old, "key_scan")
    assert sym["confidence"] == "high"
    assert sym.get("module") is None
    card = format_symbol_card(sym)
    assert "[Module]" not in card  # 无模块时不显示模块段


# ---------- Planner 模块视图 ----------


def test_module_view_for_planner() -> None:
    from agentx.understanding.query import query_index

    idx = _module_index()
    qr = query_index(idx, "按键在哪里")
    view = format_module_view(idx, qr)
    assert "模块视图" in view
    assert "KEY" in view
    assert "MAIN" in view or "UI" in view  # 相邻模块


# ---------- Case 9: 真实嵌入式端到端（GD32 + Keil groups + LVGL） ----------


def test_realistic_embedded_query() -> None:
    files = [
        {"path": "Drivers/BSP/KEY/key.c", "compile_status": "compiled"},
        {"path": "Drivers/BSP/KEY/key.h", "compile_status": "compiled"},
        {"path": "Drivers/BSP/LCD/lcd.c", "compile_status": "compiled"},
        {"path": "Drivers/BSP/USART/usart.c", "compile_status": "compiled"},
        {"path": "User/ui_shelf.c", "compile_status": "compiled"},
        {"path": "User/main.c", "compile_status": "compiled"},
        {"path": "Middlewares/LVGL/src/lv_obj.c", "compile_status": "compiled"},
        {"path": "Middlewares/FreeRTOS/tasks.c", "compile_status": "compiled"},
    ]
    symbols = [
        {"name": "key_init", "type": "function", "file": "Drivers/BSP/KEY/key.c"},
        {"name": "key_scan", "type": "function", "file": "Drivers/BSP/KEY/key.c"},
        {"name": "lcd_show", "type": "function", "file": "Drivers/BSP/LCD/lcd.c"},
        {"name": "ui_shelf_refresh", "type": "function", "file": "User/ui_shelf.c"},
        {"name": "main", "type": "function", "file": "User/main.c"},
        {"name": "lv_obj_create", "type": "function", "file": "Middlewares/LVGL/src/lv_obj.c"},
    ]
    call_graph = [
        {"caller": "main", "callee": "key_init", "confidence": "high", "file": "User/main.c"},
        {
            "caller": "ui_shelf_refresh",
            "callee": "lcd_show",
            "confidence": "high",
            "file": "User/ui_shelf.c",
        },
        {
            "caller": "ui_shelf_refresh",
            "callee": "lv_obj_create",
            "confidence": "medium",
            "file": "User/ui_shelf.c",
        },
    ]
    include_map = {
        "User/ui_shelf.c": ["Drivers/BSP/KEY/key.h", "Drivers/BSP/LCD/lcd.h"],
        "User/main.c": ["User/ui_shelf.h", "Drivers/BSP/USART/usart.h"],
    }
    idx = ProjectIndex.model_validate(
        {
            "project_fingerprint": "fp",
            "index_version": "1.5",
            "generated_at": "2026-01-01T00:00:00Z",
            "files": files,
            "symbols": symbols,
            "call_graph": call_graph,
            "include_map": include_map,
            "build_info": {
                "system": "keil",
                "has_build_config": True,
                "target": "GD32F427VET6",
                "defines": ["GD32F427"],
                "groups": [
                    {
                        "name": "BSP_KEY",
                        "files": ["Drivers/BSP/KEY/key.c", "Drivers/BSP/KEY/key.h"],
                    },
                ],
            },
        }
    )
    modules, dependencies = infer_module_relations(discover_modules(idx), idx)
    idx.modules = modules
    idx.dependencies = dependencies

    # --module KEY（Keil group 真值）
    key = search_module(idx, "KEY")
    assert key["module"]["name"] == "BSP_KEY"  # 人工分组优先于目录
    assert "keil_group:BSP_KEY" in key["module"]["evidence"]["basis"]
    assert key["module"]["build_status"] == "compiled"
    assert key["module"]["entry_points"] == ["key_init"]
    assert set(key["module"]["consumers"]) == {"MAIN", "UI"}

    # 普通 symbol 查询自动带模块
    sym = search_symbol(idx, "lcd_show")
    assert sym["module"]["name"] == "LCD"
    assert "UI" in sym["module"]["consumers"]

    # 第三方模块可见但标记
    lvgl = search_module(idx, "LVGL")
    assert lvgl["module"]["third_party"] is True
    assert lvgl["module"]["type"] == "middleware"

    # Planner 模块视图
    from agentx.understanding.query import query_index

    qr = query_index(idx, "屏幕刷新")
    view = format_module_view(idx, qr)
    assert "LCD" in view
    assert "UI" in view
