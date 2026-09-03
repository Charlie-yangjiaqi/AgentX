"""Module Discovery / Infer 单测（Phase 7.7）。

Case 1: Keil group → 模块映射
Case 2: 纯目录（无 Keil）→ KEY 模块
Case 3: 前缀族跨目录合并（App/ui_*.c + User/ui_*.c → UI）
Case 4: 第三方库冻结（Middlewares/LVGL）
Case 5: 模块依赖（KEY→GPIO，call weight=3 > include weight=2）
Case 6: consumers（LVGL_UI → KEY）
Case 7: 模块级 build 聚合
Case 8: entry_points（key_init 被外部调用、模块内零调用）
Case 9: 真实嵌入式风格（GD32 + Keil groups + LVGL）端到端
"""

from __future__ import annotations

from agentx.index.index import ProjectIndex
from agentx.module.discover import discover_modules
from agentx.module.infer import infer_module_relations


def _index(**overrides: object) -> ProjectIndex:
    files = [
        {"path": "Drivers/BSP/KEY/key.c", "compile_status": "compiled"},
        {"path": "Drivers/BSP/KEY/key.h", "compile_status": "compiled"},
        {"path": "Drivers/BSP/GPIO/gpio.c", "compile_status": "compiled"},
        {"path": "Drivers/BSP/GPIO/gpio.h", "compile_status": "compiled"},
        {"path": "App/lv_shelf.c", "compile_status": "compiled"},
        {"path": "User/main.c", "compile_status": "compiled"},
        {"path": "Middlewares/LVGL/lv_obj.c", "compile_status": "compiled"},
    ]
    symbols = [
        {
            "name": "key_init",
            "type": "function",
            "file": "Drivers/BSP/KEY/key.c",
            "start_line": 1,
            "end_line": 10,
        },
        {
            "name": "key_scan",
            "type": "function",
            "file": "Drivers/BSP/KEY/key.c",
            "start_line": 11,
            "end_line": 30,
        },
        {
            "name": "key_get_state",
            "type": "function",
            "file": "Drivers/BSP/KEY/key.c",
            "start_line": 31,
            "end_line": 40,
        },
        {
            "name": "gpio_write",
            "type": "function",
            "file": "Drivers/BSP/GPIO/gpio.c",
            "start_line": 1,
            "end_line": 10,
        },
        {
            "name": "ui_shelf_init",
            "type": "function",
            "file": "App/lv_shelf.c",
            "start_line": 1,
            "end_line": 20,
        },
        {
            "name": "ui_shelf_refresh",
            "type": "function",
            "file": "App/lv_shelf.c",
            "start_line": 21,
            "end_line": 40,
        },
        {
            "name": "ui_shelf_input",
            "type": "function",
            "file": "User/ui_input.c",
            "start_line": 1,
            "end_line": 15,
        },
        {
            "name": "main",
            "type": "function",
            "file": "User/main.c",
            "start_line": 1,
            "end_line": 60,
        },
        {
            "name": "lv_obj_create",
            "type": "function",
            "file": "Middlewares/LVGL/lv_obj.c",
            "start_line": 1,
            "end_line": 50,
        },
    ]
    call_graph = [
        {"caller": "main", "callee": "key_init", "confidence": "high", "file": "User/main.c"},
        {"caller": "main", "callee": "ui_shelf_init", "confidence": "high", "file": "User/main.c"},
        {
            "caller": "key_scan",
            "callee": "gpio_write",
            "confidence": "high",
            "file": "Drivers/BSP/KEY/key.c",
        },
        {
            "caller": "ui_shelf_input",
            "callee": "key_scan",
            "confidence": "high",
            "file": "User/ui_input.c",
        },
        {
            "caller": "ui_shelf_refresh",
            "callee": "lv_obj_create",
            "confidence": "medium",
            "file": "App/lv_shelf.c",
        },
    ]
    include_map = {
        "App/lv_shelf.c": ["Drivers/BSP/KEY/key.h"],
        "User/main.c": ["App/lv_shelf.h", "Drivers/BSP/KEY/key.h"],
        "User/ui_input.c": ["Drivers/BSP/KEY/key.h"],
    }
    base = {
        "project_fingerprint": "fp",
        "index_version": "1.5",
        "generated_at": "2026-01-01T00:00:00Z",
        "files": files,
        "symbols": symbols,
        "call_graph": call_graph,
        "include_map": include_map,
        "build_info": {},
    }
    base.update(overrides)
    return ProjectIndex.model_validate(base)


def _modules(index: ProjectIndex) -> list[dict]:
    modules, _ = infer_module_relations(discover_modules(index), index)
    return modules


def _by_name(modules: list[dict], name: str) -> dict:
    return next(m for m in modules if m["name"] == name)


# ---------- Case 1: Keil group → 模块 ----------


def test_keil_group_modules() -> None:
    index = _index(
        build_info={
            "groups": [
                {"name": "BSP_KEY", "files": ["Drivers/BSP/KEY/key.c", "Drivers/BSP/KEY/key.h"]},
                {"name": "BSP_GPIO", "files": ["gpio.c", "gpio.h"]},
                {"name": "Application", "files": ["App/lv_shelf.c"]},
            ]
        }
    )
    modules = _modules(index)
    names = {m["name"] for m in modules}
    assert "BSP_KEY" in names
    assert "BSP_GPIO" in names  # basename 唯一匹配
    assert "Application" in names
    m = _by_name(modules, "BSP_KEY")
    assert m["files"] == ["Drivers/BSP/KEY/key.c", "Drivers/BSP/KEY/key.h"]
    assert "keil_group:BSP_KEY" in m["evidence"]["basis"]
    assert m["confidence"] == 0.95


# ---------- Case 2: 纯目录（无 Keil）→ KEY 模块 ----------


def test_directory_only_modules() -> None:
    modules = _modules(_index())
    key = _by_name(modules, "KEY")
    assert key["files"] == ["Drivers/BSP/KEY/key.c", "Drivers/BSP/KEY/key.h"]
    assert key["symbols"] == ["key_init", "key_scan", "key_get_state"]
    assert key["type"] == "bsp"
    assert "path:KEY" in key["evidence"]["basis"]
    assert "prefix:key" in key["evidence"]["basis"]
    assert key["confidence"] == 0.85


# ---------- Case 3: 前缀族跨目录合并（App/ui_*.c + User/ui_input.c → UI） ----------


def test_prefix_merge_across_dirs() -> None:
    modules = _modules(_index())
    ui = _by_name(modules, "UI")
    assert "App/lv_shelf.c" in ui["files"]
    assert "User/ui_input.c" in ui["files"]
    assert ui["symbols"] == ["ui_shelf_init", "ui_shelf_refresh", "ui_shelf_input"]
    assert any("prefix:ui" in b or "symbol:ui" in b for b in ui["evidence"]["basis"])
    assert ui["type"] == "app"


# ---------- Case 4: 第三方库冻结 ----------


def test_third_party_frozen() -> None:
    modules = _modules(_index())
    lvgl = _by_name(modules, "LVGL")
    assert lvgl["third_party"] is True
    # LVGL 不被 UI 合并（第三方冻结）
    ui = _by_name(modules, "UI")
    assert "Middlewares/LVGL/lv_obj.c" not in ui["files"]
    assert "third_party" in lvgl["evidence"]["basis"]
    assert lvgl["type"] == "middleware"


# ---------- Case 5: 模块依赖（call weight=3 > include weight=2） ----------


def test_module_dependencies() -> None:
    modules, dependencies = infer_module_relations(discover_modules(_index()), _index())
    key = _by_name(modules, "KEY")
    assert key["dependencies"] == ["GPIO"]
    gpio_dep = next(d for d in dependencies if d["from"] == "KEY" and d["to"] == "GPIO")
    assert "call" in gpio_dep["kind"]
    assert gpio_dep["weight"] == 4  # call(3) + high confidence(+1)
    ui_dep = next(d for d in dependencies if d["from"] == "UI" and d["to"] == "KEY")
    assert "call" in ui_dep["kind"] and "include" in ui_dep["kind"]
    assert ui_dep["weight"] >= 6  # call(3+high1) + include(2)×2
    main_dep = next(d for d in dependencies if d["from"] == "MAIN" and d["to"] == "UI")
    assert main_dep is not None


# ---------- Case 6: consumers ----------


def test_module_consumers() -> None:
    modules = _modules(_index())
    key = _by_name(modules, "KEY")
    assert "UI" in key["consumers"]
    assert "MAIN" in key["consumers"]
    gpio = _by_name(modules, "GPIO")
    assert "KEY" in gpio["consumers"]


# ---------- Case 7: 模块级 build 聚合 ----------


def test_module_build_status() -> None:
    index = _index()
    index.files[0].compile_status = "excluded"  # key.c 被排除
    modules = _modules(index)
    # KEY：key.c excluded + key.h compiled → compiled（任一 compiled）
    assert _by_name(modules, "KEY")["build_status"] == "compiled"
    # 全 excluded 模块
    index2 = _index()
    for f in index2.files:
        f.compile_status = "excluded"
    modules2 = _modules(index2)
    assert _by_name(modules2, "KEY")["build_status"] == "excluded"
    # 无构建配置 → unknown
    index3 = _index()
    for f in index3.files:
        f.compile_status = "unknown"
    modules3 = _modules(index3)
    assert _by_name(modules3, "KEY")["build_status"] == "unknown"


# ---------- Case 8: entry_points ----------


def test_entry_points() -> None:
    modules = _modules(_index())
    key = _by_name(modules, "KEY")
    # key_init（被 main 调用）与 key_scan（被 UI 调用）均为模块入口
    assert key["entry_points"] == ["key_init", "key_scan"]
    # UI：ui_shelf_init 被 main 调用 → 入口
    assert _by_name(modules, "UI")["entry_points"] == ["ui_shelf_init"]
    # GPIO：gpio_write 被 key_scan（KEY 模块）调用 → 入口
    assert _by_name(modules, "GPIO")["entry_points"] == ["gpio_write"]


# ---------- Case 8: 垃圾 token 防护（函数指针类型不成为模块） ----------


def test_invalid_token_never_becomes_module() -> None:
    files = [
        {"path": "User/app.c", "compile_status": "compiled"},
        {"path": "Drivers/BSP/KEY/key.c", "compile_status": "compiled"},
    ]
    symbols = [
        {
            "name": "(*TASKFUNCTION)",
            "type": "function",
            "file": "User/app.c",
            "start_line": 1,
        },
        {
            "name": "(*VECTOR)",
            "type": "function",
            "file": "User/app.c",
            "start_line": 2,
        },
        {"name": "key_scan", "type": "function", "file": "Drivers/BSP/KEY/key.c"},
    ]
    index = ProjectIndex.model_validate(
        {
            "project_fingerprint": "fp",
            "index_version": "1.5",
            "generated_at": "2026-01-01T00:00:00Z",
            "files": files,
            "symbols": symbols,
            "call_graph": [],
            "include_map": {},
            "build_info": {},
        }
    )
    modules = discover_modules(index)
    names = {m["name"] for m in modules}
    # 函数指针类型 token 不得成为模块名
    assert not any("(" in n or "*" in n or "TASKFUNCTION" in n for n in names)
    assert not any("VECTOR" in n for n in names)
    # 合法模块正常
    assert "KEY" in names
    # 文件仍被归属（fallback 到文件名 stem）
    app = next(m for m in modules if any("app.c" in f for f in m["files"]))
    assert "(" not in app["name"] and "*" not in app["name"]


# ---------- Case 9: 真实嵌入式风格（GD32 + Keil groups + LVGL） ----------


def test_realistic_embedded_project() -> None:
    files = [
        {"path": "Drivers/BSP/KEY/key.c", "compile_status": "compiled"},
        {"path": "Drivers/BSP/KEY/key.h", "compile_status": "compiled"},
        {"path": "Drivers/BSP/LCD/lcd.c", "compile_status": "compiled"},
        {"path": "Drivers/BSP/LCD/lcd.h", "compile_status": "compiled"},
        {"path": "Drivers/BSP/USART/usart.c", "compile_status": "compiled"},
        {"path": "User/ui_shelf.c", "compile_status": "compiled"},
        {"path": "User/ui_shelf.h", "compile_status": "compiled"},
        {"path": "User/main.c", "compile_status": "compiled"},
        {"path": "Middlewares/LVGL/src/lv_obj.c", "compile_status": "compiled"},
        {"path": "Middlewares/FreeRTOS/tasks.c", "compile_status": "compiled"},
    ]
    symbols = [
        {"name": "key_init", "type": "function", "file": "Drivers/BSP/KEY/key.c"},
        {"name": "key_scan", "type": "function", "file": "Drivers/BSP/KEY/key.c"},
        {"name": "key_event_t", "type": "enum", "file": "Drivers/BSP/KEY/key.h"},
        {"name": "KEY0_PIN", "type": "macro", "file": "Drivers/BSP/KEY/key.h"},
        {"name": "lcd_init", "type": "function", "file": "Drivers/BSP/LCD/lcd.c"},
        {"name": "lcd_show", "type": "function", "file": "Drivers/BSP/LCD/lcd.c"},
        {"name": "usart_init", "type": "function", "file": "Drivers/BSP/USART/usart.c"},
        {"name": "ui_shelf_init", "type": "function", "file": "User/ui_shelf.c"},
        {"name": "ui_shelf_refresh", "type": "function", "file": "User/ui_shelf.c"},
        {"name": "main", "type": "function", "file": "User/main.c"},
        {"name": "lv_obj_create", "type": "function", "file": "Middlewares/LVGL/src/lv_obj.c"},
        {"name": "xTaskCreate", "type": "function", "file": "Middlewares/FreeRTOS/tasks.c"},
    ]
    call_graph = [
        {"caller": "main", "callee": "key_init", "confidence": "high", "file": "User/main.c"},
        {"caller": "main", "callee": "lcd_init", "confidence": "high", "file": "User/main.c"},
        {"caller": "main", "callee": "usart_init", "confidence": "high", "file": "User/main.c"},
        {"caller": "main", "callee": "ui_shelf_init", "confidence": "high", "file": "User/main.c"},
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
        {"caller": "main", "callee": "xTaskCreate", "confidence": "medium", "file": "User/main.c"},
    ]
    include_map = {
        "User/ui_shelf.c": ["Drivers/BSP/KEY/key.h", "Drivers/BSP/LCD/lcd.h"],
        "User/main.c": ["User/ui_shelf.h", "Drivers/BSP/USART/usart.h"],
    }
    index = ProjectIndex.model_validate(
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
                "defines": ["GD32F427", "USE_STDPERIPH_DRIVER"],
            },
        }
    )
    modules, dependencies = infer_module_relations(discover_modules(index), index)
    names = {m["name"] for m in modules}
    assert {"KEY", "LCD", "USART", "UI", "MAIN"} <= names
    assert "LVGL" in names  # 第三方
    assert "FreeRTOS" in names  # 第三方
    key = _by_name(modules, "KEY")
    assert key["entry_points"] == ["key_init"]
    assert set(key["consumers"]) == {"MAIN", "UI"}
    assert key["dependencies"] == []
    assert key["build_status"] == "compiled"
    ui = _by_name(modules, "UI")
    assert set(ui["dependencies"]) >= {"KEY", "LCD", "LVGL"}
    lcd = _by_name(modules, "LCD")
    assert "UI" in lcd["consumers"]
    lvgl = _by_name(modules, "LVGL")
    assert lvgl["third_party"] is True
    # 模块级依赖边可回溯
    lcd_dep = next(d for d in dependencies if d["from"] == "UI" and d["to"] == "LCD")
    assert lcd_dep["weight"] >= 4  # call high
    assert "lcd_show" in lcd_dep["via"]
