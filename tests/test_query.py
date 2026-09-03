"""Index Query Layer：关键词提取 + 直接命中 + 1-hop 扩展。"""

from __future__ import annotations

from datetime import UTC, datetime

from agentx.index.index import ProjectIndex
from agentx.understanding.query import (
    extract_keywords,
    format_query_result,
    query_index,
)


def _make_index() -> ProjectIndex:
    return ProjectIndex(
        project_fingerprint="fp",
        index_version="1.2",
        generated_at=datetime(2026, 1, 1, tzinfo=UTC),
        files=[
            {"path": "main.c", "status": "active"},
            {"path": "Drivers/BSP/LCD/lcd.c", "status": "active"},
            {"path": "Drivers/BSP/LCD/lcd.h", "status": "active"},
            {"path": "Drivers/BSP/SPI/spi.c", "status": "active"},
            {"path": "Common/utils/display.c", "status": "active"},
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
                "name": "LCD_ShowString",
                "type": "function",
                "file": "lcd.c",
                "start_line": 30,
                "end_line": 40,
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
            {
                "caller": "main",
                "callee": "SPI_Init",
                "confidence": "medium",
                "file": "main.c",
                "line": 8,
            },
        ],
        include_map={
            "main.c": ["Drivers/BSP/LCD/lcd.h", "Drivers/BSP/SPI/spi.h"],
            "Drivers/BSP/LCD/lcd.c": ["Drivers/BSP/LCD/lcd.h"],
            "Drivers/BSP/SPI/spi.c": ["Drivers/BSP/SPI/spi.h"],
        },
        build_info={"system": "makefile", "compiled_files": [{"file": "main.c"}]},
        file_count=5,
    )


def test_extract_keywords_splits_camel_and_snake() -> None:
    # "LCD初始化" 的英文 token；param_init 拆分；去停用词
    kws = extract_keywords("修复 LCD初始化 的问题，检查 param_init 的实现")
    assert "lcd" in kws
    assert "init" in kws
    assert "param" in kws
    assert "the" not in kws


def test_query_direct_hit_files_and_symbols() -> None:
    result = query_index(_make_index(), "LCD 初始化")
    paths = {f["path"] for f in result["files"]}
    # lcd.c/lcd.h 直接命中；main.c 经调用边（main→LCD_Init）带出
    assert "Drivers/BSP/LCD/lcd.c" in paths
    assert "Drivers/BSP/LCD/lcd.h" in paths
    names = {s["name"] for s in result["symbols"]}
    assert "LCD_Init" in names
    assert "LCD_ShowString" in names
    assert result["reason"], "应有命中依据"


def test_query_1hop_expansion() -> None:
    result = query_index(_make_index(), "LCD_Init")
    callers = {e["caller"] for e in result["call_graph"]}
    callees = {e["callee"] for e in result["call_graph"]}
    # 1-hop：main→LCD_Init（入边）、LCD_Init→SPI_Init（出边）
    assert "main" in callers
    assert "SPI_Init" in callees
    # 扩展不超过一跳：main→SPI_Init 的边（非命中符号两端）不应出现
    # （main 是 1-hop 符号，SPI_Init 也是 1-hop 符号——此边两端都是 1-hop，可保留或省略均可）
    assert any(
        e["caller"] == "LCD_Init" and e["callee"] == "SPI_Init" for e in result["call_graph"]
    )


def test_query_no_hit_returns_empty() -> None:
    result = query_index(_make_index(), "网络协议栈")
    assert result["files"] == []
    assert result["symbols"] == []
    assert result["call_graph"] == []


def test_query_include_map_related() -> None:
    result = query_index(_make_index(), "lcd")
    # lcd.c 的 include 关系带出
    assert "Drivers/BSP/LCD/lcd.c" in result["include_map"] or "main.c" in result["include_map"]


def test_format_query_result_readable() -> None:
    result = query_index(_make_index(), "LCD")
    text = format_query_result(result)
    assert "相关文件" in text
    assert "LCD_Init" in text
    assert "相关调用" in text
