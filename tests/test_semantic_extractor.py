"""Phase 7.6 Semantic Extractor 测试（standalone，不碰 Index）。

Case 1: Function Signature（返回类型/参数/文本/行号）
Case 2: Struct Members（类型/行号/指针/数组/嵌套）
Case 3: Enum（显式值/隐式值/表达式）
Case 4: Macro（value/表达式/行号）
Case 9: GD32/LVGL 真实嵌入式风格
"""

from __future__ import annotations

from dataclasses import asdict

from agentx.semantic import extract_file_semantics


def _extract(source: str, file: str = "test.c"):
    return extract_file_semantics(file, source)


# ---------- Case 1: Function ----------


def test_function_signature_basic() -> None:
    s = _extract("uint8_t key_scan(uint8_t mode)\n{\n    return mode;\n}\n")
    assert len(s.functions) == 1
    fn = s.functions[0]
    assert fn.name == "key_scan"
    assert fn.start_line == 1
    assert fn.end_line == 4
    assert fn.signature is not None
    assert fn.signature.return_type == "uint8_t"
    assert [asdict(p) for p in fn.signature.parameters] == [{"name": "mode", "type": "uint8_t"}]
    assert fn.signature.text == "uint8_t key_scan(uint8_t mode)"


def test_function_pointer_array_params() -> None:
    s = _extract("void handle(const char *name, uint8_t buf[16], void (*cb)(uint32_t));\n")
    assert len(s.functions) == 1
    params = s.functions[0].signature.parameters if s.functions[0].signature else []
    assert asdict(params[0]) == {"name": "name", "type": "const char *"}
    assert asdict(params[1]) == {"name": "buf", "type": "uint8_t[16]"}
    assert params[2].name == "cb"
    assert "(*cb)" in params[2].type  # 函数指针保留完整声明


def test_function_void_params_and_static() -> None:
    s = _extract("static uint16_t tick(void) { return 0; }\n")
    fn = s.functions[0]
    assert fn.signature is not None
    assert fn.signature.return_type == "uint16_t"  # static 不进返回类型
    assert fn.signature.parameters == []
    assert fn.signature.text == "uint16_t tick(void)"


def test_function_declaration_only() -> None:
    s = _extract("uint8_t key_scan(uint8_t mode);\n")
    assert len(s.functions) == 1
    assert s.functions[0].signature is not None
    assert s.functions[0].signature.return_type == "uint8_t"


def test_function_multiple_params() -> None:
    s = _extract("int add(int a, int b) { return a + b; }\n")
    params = s.functions[0].signature.parameters if s.functions[0].signature else []
    assert [asdict(p) for p in params] == [
        {"name": "a", "type": "int"},
        {"name": "b", "type": "int"},
    ]


# ---------- Case 2: Struct ----------


def test_struct_members() -> None:
    s = _extract(
        "typedef struct\n{\n    uint16_t x;\n    uint16_t y;\n    uint8_t state;\n} key_t;\n"
    )
    assert len(s.structs) == 1
    st = s.structs[0]
    assert st.name == "key_t"  # typedef 别名优先
    assert st.start_line == 1
    assert st.end_line == 6
    assert asdict(st.members[0]) == {
        "name": "x",
        "type": "uint16_t",
        "line": 3,
        "is_function_pointer": False,
    }
    assert asdict(st.members[2]) == {
        "name": "state",
        "type": "uint8_t",
        "line": 5,
        "is_function_pointer": False,
    }


def test_struct_pointer_array_nested() -> None:
    s = _extract(
        "struct lcd {\n"
        "    uint8_t *data;\n"
        "    uint8_t buf[16];\n"
        "    struct { uint8_t inner; } nested;\n"
        "};\n"
    )
    assert len(s.structs) == 1
    st = s.structs[0]
    assert st.name == "lcd"
    assert asdict(st.members[0]) == {
        "name": "data",
        "type": "uint8_t *",
        "line": 2,
        "is_function_pointer": False,
    }
    assert asdict(st.members[1]) == {
        "name": "buf",
        "type": "uint8_t[16]",
        "line": 3,
        "is_function_pointer": False,
    }
    assert st.members[2].name == "nested"
    assert st.members[2].type.startswith("struct")  # 嵌套 struct 记录声明关系


def test_struct_forward_declaration_skipped() -> None:
    s = _extract("struct lcd;\nstruct lcd { int w; };\n")
    assert len(s.structs) == 1  # 前向声明不收录
    assert [asdict(m) for m in s.structs[0].members] == [
        {"name": "w", "type": "int", "line": 2, "is_function_pointer": False}
    ]


# ---------- Case 3: Enum ----------


def test_enum_explicit_implicit_expression() -> None:
    s = _extract(
        "typedef enum\n"
        "{\n"
        "    KEY_NONE,\n"
        "    KEY_PRESS = 1,\n"
        "    KEY_LONG = KEY_PRESS + 1\n"
        "} key_state_t;\n"
    )
    assert len(s.enums) == 1
    en = s.enums[0]
    assert en.name == "key_state_t"
    assert asdict(en.members[0]) == {
        "name": "KEY_NONE",
        "value": "0",
        "value_expr": None,
        "line": 3,
    }
    assert asdict(en.members[1]) == {
        "name": "KEY_PRESS",
        "value": "1",
        "value_expr": None,
        "line": 4,
    }
    assert en.members[2].name == "KEY_LONG"
    assert en.members[2].value is None
    assert en.members[2].value_expr == "KEY_PRESS + 1"  # 表达式保留，不执行


def test_enum_hex_and_trailing_increment() -> None:
    s = _extract("enum flags { A = 0x10, B, C };\n")
    en = s.enums[0]
    assert en.members[0].value == "0x10"
    assert en.members[1].value == "17"  # 隐式递增 = 16+1
    assert en.members[2].value == "18"


# ---------- Case 4: Macro ----------


def test_macro_value_and_expr() -> None:
    s = _extract(
        "#define KEY0_PRES 1\n"
        "#define UART_BUF_SIZE (1024*2)\n"
        "#define KEY0_PIN GPIO_PIN_0\n"
        "#define FLAG_MASK (1U << 4)\n"
        '#define NAME "hello"\n'
    )
    macros = {m.name: m for m in s.macros}
    assert macros["KEY0_PRES"].value == "1"
    assert macros["KEY0_PRES"].line == 1
    assert macros["UART_BUF_SIZE"].value_expr == "(1024*2)"
    assert macros["UART_BUF_SIZE"].value is None
    assert macros["KEY0_PIN"].value == "GPIO_PIN_0"  # 单标识符 → value
    assert macros["FLAG_MASK"].value_expr == "(1U << 4)"
    assert macros["NAME"].value == '"hello"'


def test_macro_function_like_skipped() -> None:
    """函数式宏：Phase 7.7.4 起收录（kind=function），不进 symbols（type_semantics 用）。"""
    s = _extract("#define MAX(a, b) ((a) > (b) ? (a) : (b))\n")
    assert len(s.macros) == 1
    m = s.macros[0]
    assert m.name == "MAX"
    assert m.kind == "function"
    assert m.value_expr == "((a) > (b) ? (a) : (b))"


# ---------- 文件级错误处理 ----------


def test_parse_failure_isolated() -> None:
    good = _extract("int foo(void) { return 1; }\n")
    assert good.errors == []
    assert len(good.functions) == 1


def test_include_guard_header_full_semantics() -> None:
    """真实 .h 标准形态：#ifndef 守卫内 struct/enum/macro/函数全部提取。"""
    src = """#ifndef __KEY_H
#define __KEY_H

#define KEY0_PIN (1 << 0)

typedef enum {
    KEY_NONE = 0,
    KEY_PRESS,
    KEY_HOLD,
} key_event_t;

typedef struct {
    int x;
    int y;
} point_t;

uint8_t key_scan(uint8_t mode);

#endif
"""
    sem = _extract(src)
    assert sem.errors == []
    assert any(m.name == "KEY0_PIN" and m.value_expr == "(1 << 0)" for m in sem.macros)
    assert any(e.name == "key_event_t" and e.members[0].value == "0" for e in sem.enums)
    assert any(s.name == "point_t" for s in sem.structs)
    assert any(f.signature.text == "uint8_t key_scan(uint8_t mode)" for f in sem.functions)


def test_merge_bad_file_does_not_break_others(tmp_path) -> None:
    """Case 7：单个坏文件不影响其他文件，errors 记录，不炸 Index Build。"""
    from agentx.semantic.merge import merge_semantics

    (tmp_path / "good.c").write_text("int foo(void) { return 1; }\n", encoding="utf-8")
    (tmp_path / "bad.c").write_text("void broken( {\n", encoding="utf-8")  # 故意坏语法
    (tmp_path / "also_good.h").write_text("#define GOOD 1\n", encoding="utf-8")
    symbols, errors, _indirect, _sem = merge_semantics(
        [], tmp_path, ["good.c", "bad.c", "also_good.h"]
    )
    # good.c 的函数仍被提取
    assert any(s["name"] == "foo" and s.get("semantic") is True for s in symbols)
    # also_good.h 的宏仍被提取
    assert any(s["name"] == "GOOD" and s.get("value") == "1" for s in symbols)
    # bad.c 的错误被记录，但不影响整体
    assert any("bad.c" in e for e in errors)


# ---------- Case 9: GD32/LVGL 真实嵌入式风格 ----------


GD32_SAMPLE = """\
#include "gd32f4xx.h"
#include "lvgl.h"

#define KEY0_PIN GPIO_PIN_0
#define WKUP_PIN GPIO_PIN_1
#define KEY_SCAN_PERIOD 10

typedef struct
{
    uint16_t x;
    uint16_t y;
    uint8_t state;
    uint8_t key_buf[8];
    lv_obj_t *label;
} key_t;

typedef enum
{
    KEY_IDLE,
    KEY_DOWN = 1,
    KEY_HOLD = KEY_DOWN + 1
} key_event_t;

uint8_t key_scan(uint8_t mode);

uint8_t key_scan(uint8_t mode)
{
    static uint8_t last = 0;
    if (mode == 0) {
        return KEY_IDLE;
    }
    return last;
}

void key_init(key_t *k)
{
    k->x = 0;
    k->y = 0;
}
"""


def test_gd32_real_world_sample() -> None:
    s = extract_file_semantics("Drivers/BSP/KEY/key.c", GD32_SAMPLE)
    assert not s.errors

    # 函数：定义 + 声明 + 指针参数
    names = {f.name: f for f in s.functions}
    assert names["key_scan"].signature is not None
    assert names["key_scan"].signature.return_type == "uint8_t"
    assert names["key_scan"].signature.text == "uint8_t key_scan(uint8_t mode)"
    key_init = names["key_init"].signature
    assert key_init is not None
    assert [asdict(p) for p in key_init.parameters] == [{"name": "k", "type": "key_t *"}]

    # struct：成员 + 数组 + 指针
    assert s.structs[0].name == "key_t"
    member_map = {m.name: m for m in s.structs[0].members}
    assert asdict(member_map["x"]) == {
        "name": "x",
        "type": "uint16_t",
        "line": 10,
        "is_function_pointer": False,
    }
    assert asdict(member_map["key_buf"]) == {
        "name": "key_buf",
        "type": "uint8_t[8]",
        "line": 13,
        "is_function_pointer": False,
    }
    assert asdict(member_map["label"]) == {
        "name": "label",
        "type": "lv_obj_t *",
        "line": 14,
        "is_function_pointer": False,
    }

    # enum：隐式 + 显式 + 表达式
    en = s.enums[0]
    assert en.name == "key_event_t"
    assert en.members[0].value == "0"  # KEY_IDLE 隐式
    assert en.members[1].value == "1"  # KEY_DOWN 显式
    assert en.members[2].value_expr == "KEY_DOWN + 1"

    # macro：数字 / 单标识符
    macro_map = {m.name: m for m in s.macros}
    assert macro_map["KEY0_PIN"].value == "GPIO_PIN_0"
    assert macro_map["KEY_SCAN_PERIOD"].value == "10"
