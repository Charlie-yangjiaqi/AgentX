"""Phase 7.8 三层 Scope（project / third_party / ignore）测试。

Case 1: LVGL 冻结模块（不拆 lv_obj/lv_draw/lv_font）
Case 2: FreeRTOS 边保留（project→xTaskCreate external）；库内部边删除
Case 3: ignore 文件不进 files/symbols/modules
Case 4: 旧 Index 无 scope 字段 → 默认 project（兼容读取）
Case 5: 真实 GD32+LVGL 端到端（project 模块 + third_party 冻结）
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from agentx.index.index import IndexFileMeta, ProjectIndex, load_index
from agentx.module.discover import discover_modules
from agentx.module.infer import infer_module_relations
from agentx.plan.service import enrich_index
from agentx.scope.config import parse_scope_config, scope_of_file
from agentx.scope.resolver import ScopeResolver
from agentx.understanding.graph import ProjectGraph, _read_codegraph_db

# ---------- 配置解析 / 分类 ----------


def test_parse_three_layer_config() -> None:
    text = """# comment
project:
  include:
    - "User/**"
    - "Drivers/**"

third_party:
  - path: "Middlewares/LVGL"
    name: "LVGL"
  - path: "Middlewares/FreeRTOS"
    name: "FreeRTOS"

ignore:
  - "LT758_DEMO/**"
  - "tools/**"
  - "*.py"
"""
    cfg = parse_scope_config(text)
    assert cfg["project_include"] == ["User/**", "Drivers/**"]
    assert cfg["project_include_set"] is True
    assert cfg["third_party"] == [
        {"path": "Middlewares/LVGL", "name": "LVGL"},
        {"path": "Middlewares/FreeRTOS", "name": "FreeRTOS"},
    ]
    assert cfg["ignore"] == ["LT758_DEMO/**", "tools/**", "*.py"]


def test_scope_priority_ignore_wins() -> None:
    cfg = parse_scope_config(
        "third_party:\n  - path: Middlewares/LVGL\n    name: LVGL\nignore:\n  - Middlewares/**\n"
    )
    assert scope_of_file("Middlewares/LVGL/src/lv_obj.c", cfg) == ("ignored", None)


def test_scope_default_project() -> None:
    cfg = parse_scope_config("third_party:\n  - path: Middlewares/LVGL\n    name: LVGL\n")
    assert scope_of_file("User/main.c", cfg) == ("project", None)
    assert scope_of_file("Middlewares/LVGL/src/lv_obj.c", cfg) == ("third_party", "LVGL")


def test_scope_project_whitelist_mode() -> None:
    cfg = parse_scope_config("project:\n  include:\n    - User/**\n    - Drivers/**\n")
    assert scope_of_file("User/main.c", cfg) == ("project", None)
    assert scope_of_file("Docs/x.c", cfg) == ("ignored", None)  # 白名单之外不进入


def test_legacy_agentxignore_compat(tmp_path) -> None:
    (tmp_path / ".agentxignore").write_text("ignore:\n  - demo/**\n", encoding="utf-8")
    resolver = ScopeResolver(tmp_path)
    assert resolver.is_ignored("demo/x.c")
    assert not resolver.is_ignored("User/main.c")


# ---------- Case 1: LVGL 冻结模块 ----------


def _third_party_index() -> ProjectIndex:
    files = [
        {"path": "User/hmi_app.c", "compile_status": "compiled", "scope_type": "project"},
        {"path": "User/main.c", "compile_status": "compiled", "scope_type": "project"},
        {
            "path": "Middlewares/LVGL/src/lv_obj.c",
            "compile_status": "compiled",
            "scope_type": "third_party",
            "scope_name": "LVGL",
        },
        {
            "path": "Middlewares/LVGL/src/lv_draw.c",
            "compile_status": "compiled",
            "scope_type": "third_party",
            "scope_name": "LVGL",
        },
        {
            "path": "Middlewares/LVGL/src/lv_font.c",
            "compile_status": "compiled",
            "scope_type": "third_party",
            "scope_name": "LVGL",
        },
    ]
    symbols = [
        {"name": "hmi_init", "type": "function", "file": "User/hmi_app.c", "start_line": 1},
        {"name": "main", "type": "function", "file": "User/main.c", "start_line": 1},
        {"name": "lv_obj_create", "type": "function", "file": "Middlewares/LVGL/src/lv_obj.c"},
        {"name": "lv_draw_rect", "type": "function", "file": "Middlewares/LVGL/src/lv_draw.c"},
    ]
    return ProjectIndex.model_validate(
        {
            "project_fingerprint": "fp",
            "index_version": "1.6",
            "generated_at": "2026-01-01T00:00:00Z",
            "files": files,
            "symbols": symbols,
            "call_graph": [],
            "include_map": {},
            "build_info": {},
        }
    )


def test_third_party_frozen_module() -> None:
    modules = discover_modules(_third_party_index())
    names = {m["name"] for m in modules}
    # 冻结：只有一个 LVGL 模块，不拆出 lv_obj/lv_draw/lv_font
    assert "LVGL" in names
    assert not any(n.startswith("lv_") for n in names)
    lvgl = next(m for m in modules if m["name"] == "LVGL")
    assert lvgl["scope_type"] == "third_party"
    assert lvgl["third_party"] is True
    assert len(lvgl["files"]) == 3
    assert set(lvgl["symbols"]) == {"lv_obj_create", "lv_draw_rect"}
    assert "scope_config:LVGL" in lvgl["evidence"]["basis"]
    # project 模块正常
    assert "HMI" in names
    assert "MAIN" in names


def test_third_party_relations_frozen() -> None:
    index = _third_party_index()
    modules, deps = infer_module_relations(discover_modules(index), index)
    lvgl = next(m for m in modules if m["name"] == "LVGL")
    assert lvgl["scope_type"] == "third_party"
    assert lvgl["entry_points"] == [] or lvgl["entry_points"] == ["lv_obj_create", "lv_draw_rect"]
    assert "MAIN" not in lvgl["consumers"] or lvgl["consumers"] == []


# ---------- Case 2: 调用边边界处理 ----------


def _make_db_with_edges(root: Path) -> None:
    db_dir = root / ".codegraph"
    db_dir.mkdir(exist_ok=True)
    con = sqlite3.connect(db_dir / "codegraph.db")
    con.executescript(
        """
        CREATE TABLE nodes (
            id TEXT PRIMARY KEY, kind TEXT, name TEXT, qualified_name TEXT,
            file_path TEXT, start_line INTEGER, end_line INTEGER, signature TEXT
        );
        CREATE TABLE edges (
            source TEXT, target TEXT, kind TEXT, metadata TEXT, line INTEGER, col INTEGER
        );
        """
    )
    nodes = [
        ("n1", "function", "hmi_task", "", "User/hmi_app.c", 1, 10, ""),
        ("n2", "function", "xTaskCreate", "", "Middlewares/FreeRTOS/tasks.c", 1, 50, ""),
        ("n3", "function", "vListInsert", "", "Middlewares/FreeRTOS/list.c", 1, 30, ""),
        ("n4", "function", "key_scan", "", "Drivers/BSP/KEY/key.c", 1, 10, ""),
    ]
    con.executemany("INSERT INTO nodes VALUES (?,?,?,?,?,?,?,?)", nodes)
    edges = [
        ("n1", "n2", "calls", '{"confidence": 0.9}', 5, 1),  # project → FreeRTOS
        ("n2", "n3", "calls", '{"confidence": 0.9}', 10, 1),  # FreeRTOS 内部
        ("n1", "n4", "calls", '{"confidence": 0.9}', 8, 1),  # project → project
    ]
    con.executemany("INSERT INTO edges VALUES (?,?,?,?,?,?)", edges)
    con.commit()
    con.close()


def test_call_graph_third_party_boundary(tmp_path) -> None:
    (tmp_path / "User").mkdir()
    (tmp_path / "Middlewares").mkdir()
    (tmp_path / "Drivers").mkdir()
    _make_db_with_edges(tmp_path)
    (tmp_path / ".agentxscope.yaml").write_text(
        "third_party:\n  - path: Middlewares/FreeRTOS\n    name: FreeRTOS\n",
        encoding="utf-8",
    )
    knowledge = _read_codegraph_db(tmp_path)
    edges = knowledge["call_graph"]
    # project → FreeRTOS 保留 + external 标记
    external = [e for e in edges if e.get("external")]
    assert any(e["caller"] == "hmi_task" and e["callee"] == "xTaskCreate" for e in external)
    # FreeRTOS 内部边（xTaskCreate → vListInsert）删除
    assert not any(e["callee"] == "vListInsert" for e in edges)
    # project 内部边保留，无 external 标记
    internal = [e for e in edges if e["caller"] == "hmi_task" and e["callee"] == "key_scan"]
    assert internal and not internal[0].get("external")


# ---------- Case 3: ignore 文件不进入任何数据 ----------


def test_enrich_ignore_full_filter(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "User").mkdir()
    (tmp_path / "User" / "main.c").write_text("int main(void) { return 0; }\n", encoding="utf-8")
    tools = tmp_path / "tools"
    tools.mkdir()
    (tools / "test.py").write_text("print('x')\n", encoding="utf-8")
    (tmp_path / ".agentxscope.yaml").write_text("ignore:\n  - tools/**\n", encoding="utf-8")
    monkeypatch.setattr(
        "agentx.plan.service.analyze_project",
        lambda root: ProjectGraph(
            source="filescan",
            files=[{"path": "User/main.c", "language": "c"}],
            build_info={},
            include_map={},
            errors=[],
        ),
    )
    index, _ = enrich_index(tmp_path)
    assert not any("tools" in f.path for f in index.files)
    assert not any("tools" in str(s.get("file", "")) for s in index.symbols)
    assert not any("tools" in str(m.get("name", "")) for m in index.modules)


# ---------- Case 4: 旧 Index 兼容（无 scope 字段 → project） ----------


def test_old_index_defaults_project(tmp_path) -> None:
    (tmp_path / "main.c").write_text("int main(void) { return 0; }\n", encoding="utf-8")
    # 构造无 scope 字段的旧 Index
    old = ProjectIndex(
        project_fingerprint="fp",
        index_version="1.5",
        generated_at=datetime(2026, 1, 1, tzinfo=UTC),
        files=[{"path": "main.c"}],  # 无 scope_type/scope_name
    )
    from agentx.index.index import save_index

    save_index(tmp_path, old)
    loaded = load_index(tmp_path)
    assert loaded is not None
    assert loaded.files[0].scope_type == "project"
    assert loaded.files[0].scope_name is None
    # 旧 Index 模块层行为不变
    assert isinstance(loaded.files[0], IndexFileMeta)


# ---------- Case 5: 真实 GD32 + LVGL 端到端 ----------


def test_realistic_gd32_lvgl_end_to_end(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / ".agentxscope.yaml").write_text(
        "third_party:\n"
        "  - path: Middlewares/LVGL\n    name: LVGL\n"
        "  - path: Middlewares/FreeRTOS\n    name: FreeRTOS\n"
        "  - path: Drivers/CMSIS\n    name: CMSIS\n",
        encoding="utf-8",
    )
    files = [
        {"path": "User/hmi_app.c", "language": "c", "scope_type": "project", "scope_name": None},
        {"path": "User/main.c", "language": "c", "scope_type": "project", "scope_name": None},
        {
            "path": "Drivers/BSP/KEY/key.c",
            "language": "c",
            "scope_type": "project",
            "scope_name": None,
        },
        {
            "path": "Drivers/BSP/LCD/lcd.c",
            "language": "c",
            "scope_type": "project",
            "scope_name": None,
        },
        {
            "path": "Middlewares/LVGL/src/lv_obj.c",
            "language": "c",
            "scope_type": "third_party",
            "scope_name": "LVGL",
        },
        {
            "path": "Middlewares/FreeRTOS/tasks.c",
            "language": "c",
            "scope_type": "third_party",
            "scope_name": "FreeRTOS",
        },
        {
            "path": "Drivers/CMSIS/core_cm4.h",
            "language": "c",
            "scope_type": "third_party",
            "scope_name": "CMSIS",
        },
    ]
    symbols = [
        {"name": "hmi_init", "type": "function", "file": "User/hmi_app.c", "start_line": 1},
        {"name": "main", "type": "function", "file": "User/main.c", "start_line": 1},
        {"name": "key_scan", "type": "function", "file": "Drivers/BSP/KEY/key.c", "start_line": 1},
        {"name": "lcd_show", "type": "function", "file": "Drivers/BSP/LCD/lcd.c", "start_line": 1},
        {"name": "lv_obj_create", "type": "function", "file": "Middlewares/LVGL/src/lv_obj.c"},
        {"name": "xTaskCreate", "type": "function", "file": "Middlewares/FreeRTOS/tasks.c"},
        {"name": "NVIC_EnableIRQ", "type": "function", "file": "Drivers/CMSIS/core_cm4.h"},
    ]
    graph = ProjectGraph(
        source="codegraph",
        files=files,
        symbols=symbols,
        call_graph=[
            {
                "caller": "hmi_init",
                "callee": "lv_obj_create",
                "confidence": "high",
                "file": "User/hmi_app.c",
                "external": True,
            },
            {
                "caller": "main",
                "callee": "xTaskCreate",
                "confidence": "high",
                "file": "User/main.c",
                "external": True,
            },
            {
                "caller": "main",
                "callee": "hmi_init",
                "confidence": "high",
                "file": "User/main.c",
            },
        ],
        include_map={},
        build_info={},
        errors=[],
    )
    monkeypatch.setattr("agentx.plan.service.analyze_project", lambda root: graph)

    index, _ = enrich_index(tmp_path)
    mods = {m["name"]: m for m in index.modules}
    # project 模块
    assert mods["HMI"]["scope_type"] == "project"
    assert mods["KEY"]["scope_type"] == "project"
    assert mods["LCD"]["scope_type"] == "project"
    # third_party 冻结模块
    assert mods["LVGL"]["scope_type"] == "third_party"
    assert set(mods["LVGL"]["symbols"]) == {"lv_obj_create"}
    assert mods["FreeRTOS"]["scope_type"] == "third_party"
    assert mods["CMSIS"]["scope_type"] == "third_party"
    # 没有库内部碎片模块
    assert not any("lv_" in n and n != "LVGL" for n in mods)
    # symbols 带 scope_type 标注
    lvgl_sym = next(s for s in index.symbols if s["name"] == "lv_obj_create")
    assert lvgl_sym["scope_type"] == "third_party"
    assert lvgl_sym["scope_name"] == "LVGL"
    hmi_sym = next(s for s in index.symbols if s["name"] == "hmi_init")
    assert hmi_sym["scope_type"] == "project"
    # 文件带 scope_type
    lvgl_file = next(f for f in index.files if f.path.endswith("lv_obj.c"))
    assert lvgl_file.scope_type == "third_party"
    assert lvgl_file.scope_name == "LVGL"
