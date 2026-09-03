"""Phase 7.9 Evidence Validation 验收测试。

验收清单（设计文档）：
1. LLM 幻造不存在文件 → BLOCK（Rule 1）
2. 正确函数修改（call_graph 证据）→ PASS
3. 仅文件名匹配（存在但无关系证据）→ WARNING
4. struct 字段修改（struct_usage 证据）→ PASS
5. 新增接口无 consumer → WARNING（Rule 4）
6. 跨模块修改无传播链 → BLOCK（Rule 5）
+ 修正流程：BLOCK → 一次修正 → PASS / 仍 BLOCK → plan_blocked / accept_blocked 强制接受

Phase 7.9 修复（issue A/B/C/D）：
- A. 字段聚合命中必须保留 struct_usage + field definition direct（不漏、不误判 weak/block）
- B. add 新目标无历史证据 → WARNING；不允许 BLOCK（Rule 5 跳过 add）
- C. 文件存在只能作为 weak evidence；不得因 file exists 判 PASS
- D. “无链”测试数据必须真正无链（无 symbol/call/indirect/usage/dependency 边）
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

from agentx.index.index import IndexFileMeta, ProjectIndex
from agentx.validation.validator import validate_plan


def _index(tmp_path: Path, fingerprint: str = "fp-abc") -> ProjectIndex:
    files = [
        {"path": "User/hmi/alarm_view.c", "status": "active"},
        {"path": "User/hmi/alarm_view.h", "status": "active"},
        {"path": "App/alarm_service.c", "status": "active"},
        {"path": "App/alarm_service.h", "status": "active"},
    ]
    symbols = [
        {
            "name": "alarm_view_update",
            "type": "function",
            "file": "User/hmi/alarm_view.c",
            "start_line": 10,
        },
        {
            "name": "alarm_service_update",
            "type": "function",
            "file": "App/alarm_service.c",
            "start_line": 20,
        },
        {
            "name": "alarm_state_t",
            "type": "struct",
            "file": "User/hmi/alarm_view.h",
            "start_line": 30,
        },
    ]
    call_graph = [
        {"caller": "alarm_task", "callee": "alarm_view_update"},
        {"caller": "alarm_view_update", "callee": "alarm_service_update"},
    ]
    include_map = {
        "User/hmi/alarm_view.h": ["User/hmi/alarm_view.c", "App/alarm_service.c"],
    }
    type_semantics = {
        "structs": [
            {
                "name": "alarm_state_t",
                "file": "User/hmi/alarm_view.h",
                "line": 30,
                "fields": [
                    {"name": "alarm_level", "type": "int", "line": 31},
                    {"name": "on_change", "type": "void (*)(void)", "line": 32},
                ],
            }
        ],
        "enums": [],
        "macros": [],
        "struct_usage": {
            "alarm_level": {
                "read_by": ["alarm_view_update"],
                "write_by": ["alarm_service_update"],
            }
        },
    }
    modules = [
        {
            "name": "HMI",
            "type": "app",
            "files": ["User/hmi/alarm_view.c", "User/hmi/alarm_view.h"],
            "symbols": ["alarm_view_update", "alarm_state_t"],
            "dependencies": ["AlarmService"],
            "consumers": [],
        },
        {
            "name": "AlarmService",
            "type": "app",
            "files": ["App/alarm_service.c", "App/alarm_service.h"],
            "symbols": ["alarm_service_update"],
            "dependencies": [],
            "consumers": ["HMI"],
        },
    ]
    index = ProjectIndex(
        project_fingerprint=fingerprint,
        index_version="test",
        generated_at=datetime.now(),
        files=[IndexFileMeta.model_validate(f) for f in files],
        modules=modules,
        symbols=symbols,
        dependencies=[{"from": "HMI", "to": "AlarmService"}],
        call_graph=call_graph,
        include_map=include_map,
        type_semantics=type_semantics,
    )
    return index


def _ch(file: str, symbol: str = "", op: str = "modify") -> dict[str, Any]:
    return {"file": file, "symbol": symbol, "operation": op, "reason": ""}


def test_hallucinated_file_blocked(tmp_path: Path) -> None:
    """验收 1：LLM 幻造不存在的文件 → BLOCK（Rule 1）。"""
    index = _index(tmp_path)
    result = validate_plan(index, [_ch("AlarmManager.c", "alarm_manager_init")])
    assert result.level == "block"
    assert result.changes[0].status == "block"
    assert any("不存在" in r for r in result.changes[0].reasons)


def test_hallucinated_symbol_blocked(tmp_path: Path) -> None:
    """Rule 1：文件存在但符号幻造 → BLOCK。"""
    index = _index(tmp_path)
    result = validate_plan(index, [_ch("User/hmi/alarm_view.c", "fake_never_defined")])
    assert result.level == "block"
    assert "目标符号不存在" in result.changes[0].reasons[0]


def test_function_with_callgraph_passes(tmp_path: Path) -> None:
    """验收 2：正确函数修改（call_graph 证据）→ PASS。"""
    index = _index(tmp_path)
    result = validate_plan(index, [_ch("User/hmi/alarm_view.c", "alarm_view_update")])
    assert result.level == "pass"
    assert result.changes[0].status == "pass"
    sources = {e.source for e in result.changes[0].direct}
    assert "call_graph" in sources
    assert "symbols" in sources


def test_filename_only_match_warns(tmp_path: Path) -> None:
    """场景2/7：文件存在（内含函数）但零关系证据 → WARNING（不得 PASS）。

    issue C：文件存在只能作为 weak evidence，不能因 file exists 判修改合理。
    """
    # 存在文件 + 存在符号，但零关系证据：不引用任何符号、无调用/读写/包含
    index2 = _index(tmp_path)
    index2.call_graph = []
    index2.include_map = {}
    index2.type_semantics = {"structs": [], "enums": [], "macros": [], "struct_usage": {}}
    index2.modules[0]["symbols"] = ["unrelated_fn"]
    index2.modules[0]["dependencies"] = []
    index2.modules[0]["consumers"] = []
    index2.symbols = [
        {
            "name": "unrelated_fn",
            "type": "function",
            "file": "User/hmi/alarm_view.c",
            "start_line": 1,
        }
    ]
    result = validate_plan(index2, [_ch("User/hmi/alarm_view.c")])
    assert result.level == "warning"
    assert result.changes[0].status == "warning"


def test_no_evidence_at_all_blocked(tmp_path: Path) -> None:
    """完全无证据（文件不存在 + 无符号）→ BLOCK。"""
    index = _index(tmp_path)
    result = validate_plan(index, [_ch("Nonexistent/ghost.c")])
    assert result.level == "block"


def test_struct_field_with_usage_passes(tmp_path: Path) -> None:
    """验收 4：struct 字段修改（struct_usage 证据）→ PASS。"""
    index = _index(tmp_path)
    result = validate_plan(index, [_ch("User/hmi/alarm_view.h", "alarm_level")])
    assert result.level == "pass"
    sources = {e.source for e in result.changes[0].direct}
    assert "struct_usage" in sources
    assert "symbols" in sources


def test_new_interface_without_consumer_warns(tmp_path: Path) -> None:
    """验收 5：新增接口无 consumer → WARNING（Rule 4）。"""
    index = _index(tmp_path)
    result = validate_plan(
        index, [_ch("App/alarm_service.c", "alarm_service_new_api", "add")]
    )
    assert result.level == "warning"
    assert any("Rule 4" in r for r in result.changes[0].reasons)


def test_new_interface_with_consumer_passes(tmp_path: Path) -> None:
    """新增接口但已有调用证据 → PASS。"""
    # 模拟新接口已被调用
    index2 = _index(tmp_path)
    index2.call_graph.append({"caller": "alarm_task", "callee": "alarm_service_new_api"})
    result = validate_plan(
        index2, [_ch("App/alarm_service.c", "alarm_service_new_api", "add")]
    )
    assert result.level == "pass"
    assert result.changes[0].status == "pass"


def test_cross_module_without_chain_blocked(tmp_path: Path) -> None:
    """验收 6：跨模块修改无传播链 → BLOCK（Rule 5）。

    “无链”必须真正无链（issue D）：无 call_graph / include_map / struct_usage /
    module dependencies 边连接两模块，仅保留符号定义（symbol → 文件）。
    """
    index2 = _index(tmp_path)
    index2.call_graph = []
    index2.include_map = {}
    index2.type_semantics = {"structs": [], "enums": [], "macros": [], "struct_usage": {}}
    index2.modules[0]["dependencies"] = []
    index2.modules[1]["consumers"] = []
    result = validate_plan(
        index2,
        [
            _ch("User/hmi/alarm_view.c", "alarm_view_update"),
            _ch("App/alarm_service.c", "alarm_service_update"),
        ],
    )
    assert result.level == "block"
    assert any("Rule 5" in r for r in result.changes[0].reasons)


def test_cross_module_with_chain_passes(tmp_path: Path) -> None:
    """跨模块修改有传播链（call_graph 1 跳）→ PASS。"""
    index = _index(tmp_path)
    result = validate_plan(
        index,
        [
            _ch("App/alarm_service.c", "alarm_service_update"),
            _ch("User/hmi/alarm_view.c", "alarm_view_update"),
        ],
    )
    assert result.level == "pass"
    assert result.changes[0].propagation, "应记录传播链"


def test_cross_module_chain_two_hops_passes(tmp_path: Path) -> None:
    """传播链 2 层可达（alarm_task → view_update → service_update）→ PASS。"""
    index = _index(tmp_path)
    result = validate_plan(
        index,
        [
            _ch("User/hmi/alarm_view.c", "alarm_view_update"),
            _ch("App/alarm_service.c", "alarm_service_update"),
        ],
    )
    assert result.level == "pass"
    assert result.changes[0].propagation


def test_add_new_file_allowed(tmp_path: Path) -> None:
    """operation=add 新建文件 → 不触发 Rule 1 BLOCK。"""
    index = _index(tmp_path)
    result = validate_plan(index, [_ch("App/new_module.c", "new_module_init", "add")])
    assert result.changes[0].status in ("pass", "warning")
    assert result.level in ("pass", "warning")


def test_validation_output_shape(tmp_path: Path) -> None:
    """输出结构：level / summary / reasons / changes[].as_dict。"""
    index = _index(tmp_path)
    result = validate_plan(
        index,
        [_ch("User/hmi/alarm_view.c", "alarm_view_update")],
    )
    d = result.as_dict()
    assert d["level"] == "pass"
    assert d["summary"].startswith("Validation: PASS")
    c = d["changes"][0]
    assert c["status"] == "pass"
    assert all("source" in e and "description" in e and "kind" in e for e in c["direct"])


def _isolated_file_index(tmp_path: Path) -> ProjectIndex:
    """文件/符号存在但零关系证据的 Index：无 call/include/usage/依赖边。"""
    idx = _index(tmp_path)
    idx.call_graph = []
    idx.include_map = {}
    idx.type_semantics = {"structs": [], "enums": [], "macros": [], "struct_usage": {}}
    idx.modules[0]["dependencies"] = []
    idx.modules[0]["consumers"] = []
    idx.modules[1]["dependencies"] = []
    idx.modules[1]["consumers"] = []
    return idx


def test_existing_function_no_evidence_blocked(tmp_path: Path) -> None:
    """场景3：目标函数无任何工程证据（文件不存在 + 符号不存在）→ BLOCK。"""
    index = _index(tmp_path)
    result = validate_plan(index, [_ch("App/ghost_service.c", "ghost_service_init")])
    assert result.level == "block"
    assert result.changes[0].status == "block"


def test_add_new_function_no_evidence_warns(tmp_path: Path) -> None:
    """场景4：新增函数/接口 + 无 evidence → WARNING（不允许 BLOCK）。"""
    index = _index(tmp_path)
    result = validate_plan(
        index, [_ch("User/hmi/alarm_view.c", "alarm_view_new_feature", "add")]
    )
    assert result.changes[0].status == "warning"
    assert result.level == "warning"


def test_field_aggregation_only_usage_passes(tmp_path: Path) -> None:
    """场景6a：字段仅存在于 struct_usage 聚合（structs 缺失）→ 保留 direct → PASS。"""
    index2 = _index(tmp_path)
    index2.type_semantics = {
        "structs": [],
        "enums": [],
        "macros": [],
        "struct_usage": {
            "alarm_level": {
                "read_by": ["alarm_view_update"],
                "write_by": ["alarm_service_update"],
            }
        },
    }
    result = validate_plan(index2, [_ch("User/hmi/alarm_view.h", "alarm_level")])
    assert result.level == "pass"
    assert result.changes[0].status == "pass"
    sources = {e.source for e in result.changes[0].direct}
    assert "struct_usage" in sources


def test_field_definition_without_usage_passes(tmp_path: Path) -> None:
    """场景6b：字段仅定义在 struct（无读写聚合）→ field definition 保留 → PASS。"""
    index2 = _index(tmp_path)
    index2.type_semantics = {
        "structs": [
            {
                "name": "alarm_state_t",
                "file": "User/hmi/alarm_view.h",
                "line": 30,
                "fields": [{"name": "alarm_level", "type": "int", "line": 31}],
            }
        ],
        "enums": [],
        "macros": [],
        "struct_usage": {},
    }
    result = validate_plan(index2, [_ch("User/hmi/alarm_view.h", "alarm_level")])
    assert result.level == "pass"
    assert result.changes[0].status == "pass"
    assert any(e.source == "symbols" for e in result.changes[0].direct)


def test_field_aggregation_keeps_definition_and_usage(tmp_path: Path) -> None:
    """场景6：字段同时命中 struct_usage 聚合 + struct 定义 → 两者都保留 → PASS。"""
    index = _index(tmp_path)
    result = validate_plan(index, [_ch("User/hmi/alarm_view.h", "alarm_level")])
    assert result.level == "pass"
    sources = {e.source for e in result.changes[0].direct}
    assert "struct_usage" in sources
    assert "symbols" in sources
    assert result.changes[0].status == "pass"


def test_file_exists_only_in_consumed_module_not_pass(tmp_path: Path) -> None:
    """场景7：文件存在（模块还被依赖）但无 symbol/call/type/usage → 不得 PASS。

    issue C：模块 consumers 不能单独支撑文件级修改（需符号锚点）。
    """
    index2 = _index(tmp_path)
    index2.call_graph = []
    index2.include_map = {}
    index2.type_semantics = {"structs": [], "enums": [], "macros": [], "struct_usage": {}}
    index2.modules[1]["consumers"] = ["HMI"]  # AlarmService 被依赖
    result = validate_plan(index2, [_ch("App/alarm_service.c")])
    assert result.level != "pass"
    assert result.changes[0].status in ("warning", "block")


def test_cross_module_with_add_does_not_block(tmp_path: Path) -> None:
    """场景B：新增目标跨模块无历史传播链 → 不 BLOCK（Rule 5 跳过 add）。"""
    index = _index(tmp_path)
    result = validate_plan(
        index,
        [
            _ch("NewModule/new_mod.c", "new_mod_init", "add"),
            _ch("User/hmi/alarm_view.c", "alarm_view_update"),
        ],
    )
    assert result.changes[0].status == "warning"
    assert result.changes[1].status == "pass"
    assert result.level == "warning"
