"""_read_codegraph_db：CodeGraph 知识库（sqlite）→ symbols/call_graph/include_map。

知识库为 CodeGraph 私有格式，测试构造最小 schema（与真实 db 一致），
验证提取逻辑与置信度映射，不依赖真实 CodeGraph。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from agentx.understanding.graph import _read_codegraph_db


def _make_db(root: Path) -> Path:
    """构造最小 .codegraph/codegraph.db（nodes + edges，仿真实 schema）。"""
    db_dir = root / ".codegraph"
    db_dir.mkdir(parents=True)
    db = db_dir / "codegraph.db"
    con = sqlite3.connect(db)
    con.executescript(
        """
        CREATE TABLE nodes (
            id TEXT PRIMARY KEY, kind TEXT, name TEXT, qualified_name TEXT,
            file_path TEXT, language TEXT, start_line INTEGER, end_line INTEGER,
            start_column INTEGER, end_column INTEGER, docstring TEXT,
            signature TEXT, visibility TEXT, is_exported INTEGER, is_async INTEGER,
            is_static INTEGER, is_abstract INTEGER, decorators TEXT,
            type_parameters TEXT, updated_at INTEGER
        );
        CREATE TABLE edges (
            id INTEGER PRIMARY KEY, source TEXT, target TEXT, kind TEXT,
            metadata TEXT, line INTEGER, col INTEGER, provenance TEXT
        );
        """
    )
    con.executemany(
        "INSERT INTO nodes (id, kind, name, qualified_name, file_path, start_line, "
        "end_line, signature) VALUES (?,?,?,?,?,?,?,?)",
        [
            ("file:main.c", "file", "main.c", "main.c", "main.c", 1, 14, None),
            ("file:param.c", "file", "param.c", "param.c", "param.c", 1, 74, None),
            ("file:param.h", "file", "param.h", "param.h", "param.h", 1, 12, None),
            ("function:1", "function", "main", "main", "main.c", 4, 14, None),
            ("function:2", "function", "param_init", "param_init", "param.c", 16, 19, None),
            ("function:3", "function", "param_set", "param_set", "param.c", 21, 52, None),
            ("struct:4", "struct", "ParamEntry", "ParamEntry", "param.c", 6, 10, None),
            ("type_alias:5", "type_alias", "ParamCtx", "ParamCtx", "param.h", 5, 5, None),
            ("import:6", "import", "param.h", "param.h", "main.c", 2, 3, '#include "param.h"'),
        ],
    )
    con.executemany(
        "INSERT INTO edges (source, target, kind, metadata, line, col) VALUES (?,?,?,?,?,?)",
        [
            # 高置信度调用
            (
                "function:1",
                "function:2",
                "calls",
                '{"confidence":0.9,"resolvedBy":"exact-match"}',
                5,
                20,
            ),
            # 中置信度调用
            (
                "function:1",
                "function:3",
                "calls",
                '{"confidence":0.6,"resolvedBy":"approximate"}',
                10,
                4,
            ),
            # 低置信度调用（无 metadata 视为 medium，这里给低分）
            (
                "function:2",
                "function:1",
                "calls",
                '{"confidence":0.3,"resolvedBy":"inference"}',
                18,
                4,
            ),
            # 文件间 include（应进 include_map）
            (
                "file:main.c",
                "file:param.h",
                "imports",
                '{"confidence":0.9,"resolvedBy":"import"}',
                2,
                0,
            ),
            # include 到 import 节点（非文件，不进 include_map）
            (
                "file:main.c",
                "import:6",
                "imports",
                '{"confidence":0.95,"resolvedBy":"qualified-name"}',
                1,
                0,
            ),
            # 包含关系（不是调用，不进 call_graph）
            ("file:main.c", "function:1", "contains", None, None, None),
        ],
    )
    con.commit()
    con.close()
    return db


def test_read_codegraph_db_symbols(tmp_path: Path) -> None:
    _make_db(tmp_path)
    out = _read_codegraph_db(tmp_path)

    names = {s["name"] for s in out["symbols"]}
    # 代码实体都在；file/import 节点排除
    assert names == {"main", "param_init", "param_set", "ParamEntry", "ParamCtx"}

    fns = {s["name"]: s for s in out["symbols"] if s["type"] == "function"}
    assert fns["param_init"]["file"] == "param.c"
    assert fns["param_init"]["start_line"] == 16
    assert fns["param_init"]["end_line"] == 19

    struct = next(s for s in out["symbols"] if s["type"] == "struct")
    assert struct["name"] == "ParamEntry"
    assert struct["file"] == "param.c"


def test_read_codegraph_db_call_graph_confidence(tmp_path: Path) -> None:
    _make_db(tmp_path)
    out = _read_codegraph_db(tmp_path)

    edges = {(e["caller"], e["callee"]): e for e in out["call_graph"]}
    # 0.9 → high
    assert edges[("main", "param_init")]["confidence"] == "high"
    assert edges[("main", "param_init")]["confidence_score"] == 0.9
    assert edges[("main", "param_init")]["line"] == 5
    # 0.6 → medium
    assert edges[("main", "param_set")]["confidence"] == "medium"
    # 0.3 → low
    assert edges[("param_init", "main")]["confidence"] == "low"
    # contains 边不混入
    assert len(out["call_graph"]) == 3


def test_read_codegraph_db_include_map_files_only(tmp_path: Path) -> None:
    _make_db(tmp_path)
    out = _read_codegraph_db(tmp_path)

    assert out["include_map"] == {"main.c": ["param.h"]}


def test_read_codegraph_db_missing_raises(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="不存在"):
        _read_codegraph_db(tmp_path)


def test_read_codegraph_db_broken_schema(tmp_path: Path) -> None:
    """schema 不兼容时抛异常（由 analyze_project 降级为 filescan，不失败）。"""
    db_dir = tmp_path / ".codegraph"
    db_dir.mkdir(parents=True)
    db = db_dir / "codegraph.db"
    db.write_text("not a database", encoding="utf-8")
    with pytest.raises(sqlite3.DatabaseError):
        _read_codegraph_db(tmp_path)
