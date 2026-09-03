"""Phase 7.9 嵌入式 C 工程语义提取测试（GD32/STM32 风格）。

覆盖（任务 3/4 验收）：
- User/hmi 普通业务函数（完整签名提取）
- struct 参数 / typedef 参数
- enum 返回
- 宏较多文件
- 条件编译
- K&R 复杂定义降级收录（不丢失）
"""

from __future__ import annotations

import json

from agentx.semantic.extractor import extract_file_semantics

_EMBEDDED_SRC = r"""
#include "gd32f30x.h"
#define KEY_SCAN_INTERVAL 10u
#define GPIO_PIN_MASK(n) (1u << (n))
#define BOARD_LED_RED    GPIO_PIN_7
typedef unsigned char uint8_t;
typedef unsigned int uint32_t;
typedef enum { MODE_AUTO = 0, MODE_MANUAL, MODE_COUNT } sys_mode_t;
typedef struct {
    uint8_t x;
    uint8_t y;
    uint8_t pressed;
} key_state_t;
static key_state_t g_key;
uint8_t key_scan(key_state_t *state, uint8_t mode)
{
    return state->pressed;
}
sys_mode_t sys_get_mode(void)
{
    return MODE_AUTO;
}
static void gpio_set_pin(uint32_t pin) { }
#if defined(USE_HAL_DRIVER)
void hal_driver_init(void) { }
#endif
"""


def test_embedded_business_functions_full_signatures() -> None:
    """普通业务函数：完整签名（返回类型 + 参数名 + 参数类型）。"""
    sem = extract_file_semantics("app.c", _EMBEDDED_SRC)
    assert sem.errors == []
    fns = {f.name: f for f in sem.functions}
    assert "key_scan" in fns
    sig = fns["key_scan"].signature
    assert sig is not None
    assert sig.return_type == "uint8_t"
    assert [(p.name, p.type) for p in sig.parameters] == [
        ("state", "key_state_t *"),
        ("mode", "uint8_t"),
    ]


def test_embedded_struct_param() -> None:
    """struct 指针参数：类型完整保留（typedef 别名 + 指针）。"""
    sem = extract_file_semantics("app.c", _EMBEDDED_SRC)
    sig = next(f for f in sem.functions if f.name == "key_scan").signature
    assert sig is not None
    assert sig.parameters[0].name == "state"
    assert sig.parameters[0].type == "key_state_t *"


def test_embedded_typedef_param_and_enum_return() -> None:
    """typedef 标量参数 + enum 返回类型。"""
    sem = extract_file_semantics("app.c", _EMBEDDED_SRC)
    fns = {f.name: f for f in sem.functions}
    pin = fns["gpio_set_pin"].signature
    assert pin is not None
    assert pin.return_type == "void"
    assert [(p.name, p.type) for p in pin.parameters] == [("pin", "uint32_t")]
    mode = fns["sys_get_mode"].signature
    assert mode is not None
    assert mode.return_type == "sys_mode_t"  # enum 返回类型（typedef 别名）


def test_embedded_conditional_compilation_extracted() -> None:
    """条件编译（#if defined）内的函数仍被提取。"""
    sem = extract_file_semantics("app.c", _EMBEDDED_SRC)
    names = {f.name for f in sem.functions}
    assert "hal_driver_init" in names
    sig = next(f for f in sem.functions if f.name == "hal_driver_init").signature
    assert sig is not None and sig.return_type == "void"


def test_embedded_macro_heavy_file() -> None:
    """宏较多文件：object-like 宏提取，函数式宏跳过，enum/struct 正常。"""
    sem = extract_file_semantics("app.c", _EMBEDDED_SRC)
    macros = {m.name: m for m in sem.macros}
    assert macros["KEY_SCAN_INTERVAL"].value == "10u"
    assert macros["BOARD_LED_RED"].value == "GPIO_PIN_7"
    assert macros["GPIO_PIN_MASK"].kind == "function"  # 函数式宏：kind=function 收录
    enums = {e.name: e for e in sem.enums}
    assert enums["sys_mode_t"]
    members = {m.name: m.value for m in enums["sys_mode_t"].members}
    assert members == {"MODE_AUTO": "0", "MODE_MANUAL": "1", "MODE_COUNT": "2"}
    structs = {s.name: s for s in sem.structs}
    assert structs["key_state_t"]
    assert [(m.name, m.type) for m in structs["key_state_t"].members] == [
        ("x", "uint8_t"),
        ("y", "uint8_t"),
        ("pressed", "uint8_t"),
    ]


def test_kr_style_definition_degraded_not_lost() -> None:
    """K&R 复杂定义：函数不丢失，签名降级（参数未完全提取也可接受）。"""
    src = r"""
int sum(a, b)
int a;
int b;
{
    return a + b;
}
"""
    sem = extract_file_semantics("kr.c", src)
    assert any(f.name == "sum" for f in sem.functions)


def test_variable_declaration_not_function() -> None:
    """严格性：普通变量声明不污染函数列表。"""
    src = "int plain_var;\nvoid normal(int x) { }\n"
    sem = extract_file_semantics("v.c", src)
    assert {f.name for f in sem.functions} == {"normal"}


def test_empty_param_list_and_void() -> None:
    """void 参数 / 空参数列表。"""
    src = "void a(void) { }\nvoid b() { }\n"
    sem = extract_file_semantics("e.c", src)
    fns = {f.name: f.signature for f in sem.functions}
    assert fns["a"] is not None and fns["a"].parameters == []
    assert fns["b"] is not None and fns["b"].parameters == []


def test_parser_failure_file_isolated() -> None:
    """parser 失败文件：结构化错误，不影响同批其他文件（worker 链路）。"""
    from agentx.semantic.worker import run_jobs_isolated

    jobs = [
        ("bad.c", "void broken( {\n    return;\n"),
        ("good.c", "int ok(void) { return 1; }\n"),
    ]
    results, errors = run_jobs_isolated(jobs, timeout_seconds=30)
    assert results[1].functions and results[1].functions[0].name == "ok"
    bad_errs = [e for e in results[0].errors if e.startswith("{")]
    assert any(json.loads(e).get("type") == "semantic_partial" for e in bad_errs)
