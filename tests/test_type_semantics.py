"""Phase 7.7.4 Type Semantic 测试（Struct/Enum/Macro 数据模型级理解）。

覆盖（任务要求 12 项）：
1. struct field 提取
2. typedef struct
3. nested struct
4. function pointer field（is_function_pointer）
5. enum value（显式）
6. enum 无值情况（保守 null）
7. macro constant
8. macro function
9. 条件编译宏（flag）
10. malformed C 不崩溃
11. index roundtrip（type_semantics 持久化）
12. 与 indirect_calls 联动（字段 registered）

原则验证：
- 事实优先：字段/enum value/macro value 全部来自源码（无 LLM）
- 不污染 symbols：type_semantics 独立，symbols 无 is_function_pointer/macro kind
- 增量：content_hash stale 复用
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from agentx.semantic.extractor import extract_file_semantics
from agentx.semantic.type_extractor import build_type_semantics

_SRC = r"""
typedef struct {
    uint32_t id;
    void (*callback)(int event, void *arg);
    uint8_t data[8];
} hmi_action_t;

typedef struct {
    struct {
        uint16_t x;
    } inner;
    int (*on_scan)(void);
} key_t;

typedef enum {
    MOTOR_OK = 0,
    MOTOR_ERROR = 1,
    MOTOR_STALL,
} motor_state_t;

typedef enum {
    MODE_A,
    MODE_B,
} mode_t;

#define LCD_WIDTH  800
#define LCD_HEIGHT (480 + 40)
#define PIN_MASK(n) (1u << (n))
#define FEATURE_ENABLED

void motor_scan(void) { }
"""


def _semantics(src: str = _SRC, file: str = "types.c") -> list[Any]:
    return [extract_file_semantics(file, src)]


def _ts(src: str = _SRC, indirect: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    sem = extract_file_semantics("types.c", src)
    return build_type_semantics(Path("."), [sem], [], indirect or [], old=None)


# ---------- 1-4. Struct 语义 ----------


def test_struct_field_extraction() -> None:
    ts = _ts()
    st = next(s for s in ts["structs"] if s["name"] == "hmi_action_t")
    fields = {f["name"]: f for f in st["fields"]}
    assert fields["id"]["type"] == "uint32_t"
    assert fields["id"]["line"] > 0
    assert fields["data"]["type"] == "uint8_t[8]"
    assert fields["data"]["is_function_pointer"] is False
    assert isinstance(st["content_hash"], str)  # 来源文件局部 hash（增量用）


def test_typedef_struct_name() -> None:
    ts = _ts()
    names = {s["name"] for s in ts["structs"]}
    assert "hmi_action_t" in names  # typedef 别名
    assert "key_t" in names


def test_nested_struct_fields() -> None:
    ts = _ts()
    st = next(s for s in ts["structs"] if s["name"] == "key_t")
    fields = {f["name"]: f for f in st["fields"]}
    assert fields["inner"]["type"].startswith("struct")
    assert fields["on_scan"]["is_function_pointer"] is True


def test_function_pointer_field_marked() -> None:
    ts = _ts()
    st = next(s for s in ts["structs"] if s["name"] == "hmi_action_t")
    fields = {f["name"]: f for f in st["fields"]}
    assert fields["callback"]["is_function_pointer"] is True
    assert "(*" in fields["callback"]["type"] or "(*" in fields["callback"]["type"]


# ---------- 5-6. Enum 语义 ----------


def test_enum_explicit_values() -> None:
    ts = _ts()
    en = next(e for e in ts["enums"] if e["name"] == "motor_state_t")
    members = {m["name"]: m for m in en["members"]}
    assert members["MOTOR_OK"]["value"] == "0"
    assert members["MOTOR_ERROR"]["value"] == "1"
    assert members["MOTOR_STALL"]["value"] == "2"  # 隐式递增（规则计算，非猜）


def test_enum_no_value_conservative() -> None:
    """无显式值 → 隐式递增（C 标准规则）；无法确定的表达式保留 value_expr 不猜。"""
    src = "typedef enum { A = EXPR_CONST, B } t;\n"
    ts = _ts(src)
    en = ts["enums"][0]
    by_name = {m["name"]: m for m in en["members"]}
    assert by_name["A"]["value"] is None  # 表达式：不猜 value
    assert by_name["A"]["value_expr"] == "EXPR_CONST"
    assert by_name["B"]["value"] is None  # 前值表达式无法确定 → 后续隐式成员保守 null


# ---------- 7-9. Macro 语义 ----------


def test_macro_constant_kind() -> None:
    ts = _ts()
    macros = {m["name"]: m for m in ts["macros"]}
    assert macros["LCD_WIDTH"]["kind"] == "constant"
    assert macros["LCD_WIDTH"]["value"] == "800"  # 源码原文
    assert macros["LCD_HEIGHT"]["kind"] == "constant"
    assert macros["LCD_HEIGHT"]["value_expr"] == "(480 + 40)"
    assert "value" not in macros["LCD_HEIGHT"]  # 表达式：原文保留，不执行


def test_macro_function_kind() -> None:
    ts = _ts()
    macros = {m["name"]: m for m in ts["macros"]}
    assert macros["PIN_MASK"]["kind"] == "function"
    assert "(1u << (n))" in macros["PIN_MASK"]["value_expr"]


def test_macro_conditional_flag_kind() -> None:
    ts = _ts()
    macros = {m["name"]: m for m in ts["macros"]}
    assert macros["FEATURE_ENABLED"]["kind"] == "flag"
    assert "value" not in macros["FEATURE_ENABLED"]


# ---------- 10. malformed C 不崩溃 ----------


def test_malformed_c_no_crash() -> None:
    sem = extract_file_semantics("bad.c", "struct { int x;\nvoid broken( {\n#define X (1\n")
    # 不崩溃；type_semantics 组装安全
    ts = build_type_semantics(Path("."), [sem], [], [], old=None)
    assert isinstance(ts["structs"], list)
    assert isinstance(ts["enums"], list)
    assert isinstance(ts["macros"], list)


# ---------- 11. index roundtrip ----------


def test_type_semantics_roundtrip(tmp_path: Path) -> None:
    from agentx.index.index import load_index, save_index
    from agentx.plan.service import enrich_index
    from agentx.understanding.graph import ProjectGraph

    (tmp_path / "types.c").write_text(_SRC, encoding="utf-8")

    def _graph(root):
        return ProjectGraph(
            source="codegraph",
            files=[{"path": "types.c", "language": "c"}],
            symbols=[
                {"name": "motor_scan", "type": "function", "file": "types.c", "start_line": 1},
            ],
            call_graph=[],
            include_map={},
            build_info={},
            errors=[],
        )

    import agentx.plan.service as ps

    old_fn = ps.analyze_project
    ps.analyze_project = _graph
    try:
        index, _ = enrich_index(tmp_path)
    finally:
        ps.analyze_project = old_fn
    ts = index.type_semantics
    assert ts.get("structs") and ts.get("enums") and ts.get("macros")
    assert any(s["name"] == "hmi_action_t" for s in ts["structs"])
    # 持久化 roundtrip
    save_index(tmp_path, index)
    loaded = load_index(tmp_path)
    assert loaded is not None and loaded.type_semantics
    assert loaded.type_semantics["structs"][0]["name"] == "hmi_action_t"
    # JSON 序列化安全（含中文/特殊字符路径）
    text = json.dumps(loaded.type_semantics, ensure_ascii=False)
    assert "hmi_action_t" in text


def test_type_semantics_not_polluting_symbols() -> None:
    """type_semantics 独立：symbols 无 is_function_pointer / macro kind。"""
    from agentx.semantic.merge import _macro_symbol, _struct_symbol

    sem_obj = extract_file_semantics("types.c", _SRC)
    sym = _struct_symbol(sem_obj.structs[0])
    assert "is_function_pointer" not in str(sym.get("members", []))
    for m in sem_obj.macros:
        if m.kind != "constant":
            continue
        ms = _macro_symbol(m)
        assert "kind" not in ms  # symbols 不携带 macro kind


# ---------- 12. 与 indirect_calls 联动 ----------


def test_function_pointer_field_registered_callbacks() -> None:
    """函数指针字段 ↔ indirect_calls：绑定到该字段的函数名出现在 registered。"""
    indirect = [
        {
            "callee": "motor_scan",
            "via": "field_assign",
            "file": "types.c",
            "line": 3,
            "caller_hint": "app_init",
            "confidence": "high",
            "field": "callback",
        },
        {
            "callee": "motor_scan",
            "via": "table_assign",
            "file": "types.c",
            "line": 10,
            "caller_hint": None,
            "confidence": "high",
            "field": "",
        },
    ]
    ts = _ts(indirect=indirect)
    st = next(s for s in ts["structs"] if s["name"] == "hmi_action_t")
    callback = next(f for f in st["fields"] if f["name"] == "callback")
    assert callback["is_function_pointer"] is True
    assert callback["registered"] == ["motor_scan"]  # 字段级联动（field 匹配）


def test_registered_no_match_field() -> None:
    """非函数指针字段：field 不匹配 → 无 registered。"""
    indirect = [
        {
            "callee": "motor_scan",
            "via": "field_assign",
            "file": "types.c",
            "line": 3,
            "caller_hint": "app_init",
            "confidence": "high",
            "field": "other",
        }
    ]
    ts = _ts(indirect=indirect)
    st = next(s for s in ts["structs"] if s["name"] == "hmi_action_t")
    id_f = next(f for f in st["fields"] if f["name"] == "id")
    assert id_f["registered"] == []


# ---------- 增量：content_hash stale 复用 ----------


def test_incremental_reuse_same_hash(tmp_path: Path) -> None:
    """文件未变 → 条目复用（content_hash 相同），不重建。"""
    p = tmp_path / "types.c"
    p.write_text(_SRC, encoding="utf-8")
    sem = extract_file_semantics("types.c", _SRC)
    ts1 = build_type_semantics(tmp_path, [sem], [], [], old=None)
    marker = ts1["structs"][0]["content_hash"]
    ts2 = build_type_semantics(tmp_path, [sem], [], [], old=ts1)
    assert ts2["structs"][0]["content_hash"] == marker
    assert ts2["structs"][0]["name"] == "hmi_action_t"


def test_incremental_rebuild_changed_hash(tmp_path: Path) -> None:
    """文件变化 → 重算（content_hash 不同）。"""
    p = tmp_path / "types.c"
    p.write_text(_SRC, encoding="utf-8")
    sem1 = extract_file_semantics("types.c", _SRC)
    ts1 = build_type_semantics(tmp_path, [sem1], [], [], old=None)
    # 修改文件（新增字段）
    changed = _SRC + "typedef struct { uint8_t added; } extra_t;\n"
    p.write_text(changed, encoding="utf-8")
    sem2 = extract_file_semantics("types.c", changed)
    ts2 = build_type_semantics(tmp_path, [sem2], [], [], old=ts1)
    assert ts2["structs"][0]["content_hash"] != ts1["structs"][0]["content_hash"]
    names = {s["name"] for s in ts2["structs"]}
    assert "extra_t" in names  # 新增 struct 被收录


# ---------- struct_usage：谁读/谁写 ----------


def test_struct_usage_read_write_functions() -> None:
    src = r"""
typedef struct { uint32_t voltage; uint16_t current; } param_t;
void set_voltage(param_t *p) { p->voltage = 4800; }
uint32_t get_voltage(param_t *p) { return p->voltage; }
void show(param_t *p) { uint16_t c = p->current; }
"""
    ts = _ts(src)
    usage = ts["struct_usage"]
    assert usage["voltage"]["write_by"] == ["set_voltage"]
    assert usage["voltage"]["read_by"] == ["get_voltage"]
    assert usage["current"]["read_by"] == ["show"]
    assert usage["current"]["write_by"] == []
