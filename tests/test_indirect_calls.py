"""Phase 7.7.3 Qualified/Indirect CallGraph 测试。

覆盖（设计验收）：
1. 三种函数地址逃逸发现：.field = Func / table[i] = Func / register_xxx(Func)
2. 非函数符号过滤（右值标识符必须是 index.symbols 中 type=function）
3. worker 序列化：FileSemantics.bindings roundtrip
4. enrich_index 集成：indirect_calls 进入 index（独立于 call_graph）
5. impact 消费：registered_by 标注（via/file/line/caller_hint）+ 回调注册提示
6. 语义：indirect_calls 是注册事实，不承诺真实调用（与 call_graph 分离）
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from agentx.semantic.extractor import extract_file_semantics
from agentx.semantic.merge import _filter_indirect_calls


def _src() -> str:
    return r"""
typedef struct {
    void (*handler)(int);
    int (*cb)(void);
} ops_t;

static ops_t g_ops;

void nav_home(int x) { }

void on_dismiss(void) { return; }

int read_key(void) { return 0; }

static void (*handlers[4])(int);

void init(void)
{
    g_ops.handler = nav_home;
    g_ops.cb = &on_dismiss;
    handlers[0] = nav_home;
    handlers[1] = &on_dismiss;
    register_key_handler(read_key);
    attach_event_cb(nav_home, &on_dismiss);
}
"""


# ---------- 1. 三种 via 提取（语法层 bindings） ----------


def test_bindings_three_via_patterns() -> None:
    sem = extract_file_semantics("ops.c", _src())
    b = sem.bindings
    by_via: dict[str, list[dict[str, Any]]] = {}
    for x in b:
        by_via.setdefault(str(x["via"]), []).append(x)
    names_field = {x["name"] for x in by_via.get("field_assign", [])}
    names_table = {x["name"] for x in by_via.get("table_assign", [])}
    names_reg = {x["name"] for x in by_via.get("register_call", [])}
    assert "nav_home" in names_field
    assert "on_dismiss" in names_field
    assert "nav_home" in names_table and "on_dismiss" in names_table
    assert "read_key" in names_reg
    assert "nav_home" in names_reg and "on_dismiss" in names_reg  # 多个参数都被提取
    # caller_hint：绑定发生在 init() 内
    assert all(x.get("caller_hint") == "init" for x in b)
    # line 定位
    assert any(x.get("line") for x in b)


def test_bindings_caller_hint_none_at_top_level() -> None:
    # 顶层初始化（declaration initializer）不在提取范围（仅 assignment_expression）
    # 但函数体内赋值应提取
    src2 = "void a(void) { }\nvoid b(void) { fp = a; }\n"
    sem2 = extract_file_semantics("t2.c", src2)
    assert any(x["name"] == "a" and x["caller_hint"] == "b" for x in sem2.bindings)


def test_bindings_ignore_non_function_assignments() -> None:
    """普通赋值（x = 5、str = "abc"）不产生绑定。"""
    src = "int x;\nvoid f(void) { x = 5; int y = x; }\n"
    sem = extract_file_semantics("v.c", src)
    assert sem.bindings == []


# ---------- 2. merge 过滤：indirect_calls 只含函数符号 ----------


def test_filter_indirect_calls_function_only() -> None:
    symbols = [
        {"name": "nav_home", "type": "function", "file": "ops.c"},
        {"name": "read_key", "type": "function", "file": "ops.c"},
        {"name": "g_ops", "type": "variable", "file": "ops.c"},
    ]
    bindings = [
        {
            "name": "nav_home",
            "via": "field_assign",
            "file": "ops.c",
            "line": 12,
            "caller_hint": "init",
        },
        {
            "name": "read_key",
            "via": "register_call",
            "file": "ops.c",
            "line": 14,
            "caller_hint": "init",
        },
        {
            "name": "g_ops",
            "via": "field_assign",
            "file": "ops.c",
            "line": 15,
            "caller_hint": "init",
        },
        {
            "name": "not_a_symbol",
            "via": "register_call",
            "file": "ops.c",
            "line": 16,
            "caller_hint": "init",
        },
    ]
    out = _filter_indirect_calls(bindings, symbols)
    assert {e["callee"] for e in out} == {"nav_home", "read_key"}
    e = next(x for x in out if x["callee"] == "nav_home")
    assert e["via"] == "field_assign"
    assert e["caller_hint"] == "init"
    assert e["confidence"] == "high"
    assert e["line"] == 12


def test_filter_indirect_calls_dedupe() -> None:
    symbols = [{"name": "f", "type": "function", "file": "x.c"}]
    bindings = [
        {"name": "f", "via": "field_assign", "file": "x.c", "line": 1, "caller_hint": None},
        {"name": "f", "via": "field_assign", "file": "x.c", "line": 1, "caller_hint": None},
        {"name": "f", "via": "field_assign", "file": "x.c", "line": 2, "caller_hint": None},
    ]
    out = _filter_indirect_calls(bindings, symbols)
    assert len(out) == 2  # 同 file+line 去重


# ---------- 3. worker 序列化 roundtrip ----------


def test_worker_bindings_roundtrip() -> None:
    from agentx.semantic.worker import _from_dict, _to_dict

    sem = extract_file_semantics("ops.c", _src())
    data = _to_dict(sem)
    assert data["bindings"] and data["bindings"][0]["name"]
    back = _from_dict(data)
    assert len(back.bindings) == len(sem.bindings)
    assert back.bindings[0]["via"] == sem.bindings[0]["via"]


def test_worker_module_entry_bindings() -> None:
    """worker 子进程链路：bindings 随 FileSemantics 返回。"""
    import subprocess
    import sys

    proc = subprocess.run(
        [sys.executable, "-m", "agentx.semantic.worker"],
        input=json.dumps(
            {
                "jobs": [
                    {
                        "file": "ops.c",
                        "source": "void a(void) {}\nvoid b(void) { x.cb = a; }\n",
                    }
                ]
            }
        ),
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
    )
    assert proc.returncode == 0
    data = json.loads(proc.stdout)
    b = data["results"][0]["bindings"]
    assert any(x["name"] == "a" and x["via"] == "field_assign" for x in b)


# ---------- 4. enrich_index 集成 ----------


def test_enrich_index_indirect_calls_persisted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """enrich_index：indirect_calls 进入 index 并持久化（独立于 call_graph）。"""
    from agentx.index.index import load_index
    from agentx.plan.service import enrich_index
    from agentx.understanding.graph import ProjectGraph

    (tmp_path / "ops.c").write_text(_src(), encoding="utf-8")

    def _graph(root):
        return ProjectGraph(
            source="codegraph",
            files=[{"path": "ops.c", "language": "c"}],
            symbols=[
                {"name": "nav_home", "type": "function", "file": "ops.c", "start_line": 1},
                {"name": "on_dismiss", "type": "function", "file": "ops.c", "start_line": 1},
                {"name": "read_key", "type": "function", "file": "ops.c", "start_line": 1},
                {"name": "init", "type": "function", "file": "ops.c", "start_line": 1},
                {"name": "g_ops", "type": "variable", "file": "ops.c", "start_line": 1},
            ],
            call_graph=[
                {"caller": "init", "callee": "nav_home", "confidence": "high", "file": "ops.c"}
            ],
            include_map={},
            build_info={},
            errors=[],
        )

    monkeypatch.setattr("agentx.plan.service.analyze_project", _graph)
    index, _ = enrich_index(tmp_path)
    indirect = index.indirect_calls
    assert indirect
    callees = {e["callee"] for e in indirect}
    # 函数符号进入（nav_home/on_dismiss/read_key），变量 g_ops 不进
    assert "nav_home" in callees
    assert "on_dismiss" in callees
    assert "read_key" in callees
    assert "g_ops" not in callees
    # 与 call_graph 分离：indirect 边不进 call_graph
    assert all(not e.get("indirect") for e in index.call_graph)
    # 持久化 roundtrip
    loaded = load_index(tmp_path)
    assert loaded is not None and loaded.indirect_calls
    assert len(loaded.indirect_calls) == len(indirect)


# ---------- 5. impact 消费：registered_by ----------


def _impact_index() -> Any:
    from agentx.index.index import ProjectIndex

    return ProjectIndex(
        project_fingerprint="x",
        index_version="1.6",
        generated_at=__import__("datetime").datetime.now(__import__("datetime").UTC),
        files=[
            __import__("agentx.index.index", fromlist=["IndexFileMeta"]).IndexFileMeta(path="ops.c")
        ],
        symbols=[
            {"name": "nav_home", "type": "function", "file": "ops.c", "start_line": 1},
            {"name": "init", "type": "function", "file": "ops.c", "start_line": 1},
        ],
        call_graph=[],
        indirect_calls=[
            {
                "callee": "nav_home",
                "via": "field_assign",
                "file": "ops.c",
                "line": 12,
                "caller_hint": "init",
                "confidence": "high",
            },
            {
                "callee": "nav_home",
                "via": "table_assign",
                "file": "ops.c",
                "line": 14,
                "caller_hint": "init",
                "confidence": "high",
            },
        ],
        build_info={},
        errors=[],
    )


def test_impact_registered_by_marker() -> None:
    """命中符号被注册为回调 → registered_by 标注（via/file/line/caller_hint）。"""
    from agentx.understanding.impact import build_impact_data

    index = _impact_index()
    query = {"files": [], "symbols": [{"name": "nav_home", "file": "ops.c"}]}
    data = build_impact_data(index, query)
    sym = next(s for s in data["symbols"] if s["name"] == "nav_home")
    regs = sym["registered_by"]
    assert len(regs) == 2
    assert {"via", "file", "line", "caller_hint"} <= set(regs[0].keys())
    assert regs[0]["via"] == "field_assign"
    assert regs[0]["caller_hint"] == "init"
    # 非命中符号（无注册）不在证据卡：不产生注册噪声
    assert all(s["name"] != "init" or s["registered_by"] == [] for s in data["symbols"])


def test_impact_format_registered_note() -> None:
    """format_impact_data：回调注册提示（静态调用图无法表达调用路径）。"""
    from agentx.understanding.impact import build_impact_data, format_impact_data

    index = _impact_index()
    query = {"files": [], "symbols": [{"name": "nav_home", "file": "ops.c"}]}
    data = build_impact_data(index, query)
    text = format_impact_data(data)
    assert "作为回调被注册 2 处" in text
    assert "静态调用图无法表达调用路径" in text
    assert "via=field_assign @ ops.c:12" in text
    assert "注册者=init" in text


def test_impact_format_without_registered_no_noise() -> None:
    """无回调注册的符号：不输出注册噪声。"""
    from agentx.understanding.impact import build_impact_data, format_impact_data

    index = _impact_index()
    query = {"files": [], "symbols": [{"name": "init", "file": "ops.c"}]}
    data = build_impact_data(index, query)
    text = format_impact_data(data)
    assert "作为回调被注册" not in text


# ---------- 6. 语义边界：indirect_calls ≠ call_graph ----------


def test_indirect_calls_semantics_registration_fact() -> None:
    """indirect_calls 语义 = 注册/绑定事实（不承诺真实调用路径）。"""
    from agentx.semantic.worker import _from_dict, _to_dict

    sem = extract_file_semantics("ops.c", _src())
    data = _to_dict(sem)
    back = _from_dict(data)
    for b in back.bindings:
        # 每条绑定只有：被注册函数 + 位置 + 注册者提示，没有 caller→callee 断言
        assert {"name", "via", "file", "line", "caller_hint"} <= set(b.keys())
        assert "caller" not in b  # 不伪造调用方
    # 文档化语义：合并后的 indirect_calls 同样无 caller
    symbols = [
        {"name": "nav_home", "type": "function", "file": "ops.c"},
        {"name": "read_key", "type": "function", "file": "ops.c"},
    ]
    out = _filter_indirect_calls(back.bindings, symbols)
    for e in out:
        assert "caller" not in e
        assert e["confidence"] == "high"
