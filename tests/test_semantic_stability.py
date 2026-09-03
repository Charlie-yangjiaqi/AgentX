"""Phase 7.8.1 Tree-sitter Stability Hardening 测试。

Case 1: 正常 C 文件 → semantic=true（提取能力不下降）
Case 2: 超大文件 → 不 crash、semantic_skip 记录、Index 正常生成
Case 3: 连续批量解析（每文件独立 Parser，无状态污染）
Case 4: worker 崩溃隔离（子进程 abort → 该文件失败，其他文件继续）
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from agentx.semantic.extractor import extract_file_semantics
from agentx.semantic.merge import merge_semantics
from agentx.semantic.worker import run_jobs_isolated

_BIG_SRC = "#define FONT_%d %d\nint data_%d[4096] = { %s };\n"


def _big_source(size_mb: float) -> str:
    """构造 size_mb 大小的 C 源文件（字体/图片数组风格）。"""
    chunk = "0x00, " * 256
    parts = []
    total = 0
    i = 0
    while total < size_mb * 1024 * 1024:
        parts.append(_BIG_SRC % (i, i, i, chunk))
        total += len(parts[-1].encode("utf-8"))
        i += 1
    return "\n".join(parts)


# ---------- Case 1: 正常文件（7.6 能力不下降） ----------


def test_normal_file_semantic() -> None:
    src = """#ifndef K_H
#define K_H
#define KEY0_PIN (1 << 0)
typedef enum { KEY_NONE = 0, KEY_PRESS } key_event_t;
typedef struct { int x; int y; } point_t;
uint8_t key_scan(uint8_t mode);
#endif
"""
    sem = extract_file_semantics("key.h", src)
    assert sem.errors == []
    assert any(f.signature.text == "uint8_t key_scan(uint8_t mode)" for f in sem.functions)
    assert any(s.name == "point_t" and len(s.members) == 2 for s in sem.structs)
    assert any(e.name == "key_event_t" for e in sem.enums)
    assert any(m.name == "KEY0_PIN" for m in sem.macros)


# ---------- Case 2: 超大文件保护 ----------


def test_oversized_file_skipped_with_record(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENTX_SEMANTIC_MAX_FILE_SIZE_MB", "0.1")  # 100KB 限制
    big = _big_source(0.5)  # ~512KB > 100KB
    sem = extract_file_semantics("font_big.c", big)
    # 不 crash、无提取
    assert sem.functions == []
    assert sem.macros == []
    # errors 有结构化记录（stage/recoverable 诊断字段）
    assert len(sem.errors) == 1
    entry = json.loads(sem.errors[0])
    assert entry["type"] == "semantic_skip"
    assert entry["stage"] == "read"
    assert entry["reason"] == "source_too_large"
    assert entry["recoverable"] is False
    assert entry["file"] == "font_big.c"
    assert entry["size"] > entry["limit_bytes"]


def test_tree_sitter_version_gate_blocks_parse(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """tree-sitter>=0.26 版本门禁：不进入 native parse（防 SIGSEGV），结构化错误。"""
    import agentx.semantic.extractor as ext

    monkeypatch.setattr(ext, "_TREE_SITTER_INCOMPATIBLE", True)
    monkeypatch.setattr(ext, "_TREE_SITTER_VERSION", "0.26.0")
    sem = extract_file_semantics("x.c", "int foo(int a) { return a; }")
    # 不提取（拦截 parse），不 crash
    assert sem.functions == []
    assert len(sem.errors) == 1
    entry = json.loads(sem.errors[0])
    assert entry["type"] == "semantic_skip"
    assert entry["stage"] == "parser"
    assert entry["reason"] == "tree_sitter_version_incompatible"
    assert entry["recoverable"] is True  # 修复环境后重建即可恢复
    assert entry["version"] == "0.26.0"


def test_tree_sitter_version_gate_off_when_compatible(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """兼容版本（0.25.x）：门禁关闭，正常提取。"""
    import agentx.semantic.extractor as ext

    monkeypatch.setattr(ext, "_TREE_SITTER_INCOMPATIBLE", False)
    sem = extract_file_semantics("x.c", "int foo(int a) { return a; }")
    assert sem.functions and sem.functions[0].name == "foo"
    assert sem.functions[0].signature is not None


def test_worker_error_structured_success_false(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    """worker 返回 success:false（Python 异常）→ 结构化 semantic_worker_error。"""
    from agentx.semantic import worker as worker_mod

    err_serve = tmp_path / "err_serve.py"
    err_serve.write_text(
        "import sys, json\n"
        "for line in sys.stdin:\n"
        "    line = line.strip()\n"
        "    if not line:\n"
        "        continue\n"
        "    print(json.dumps({'success': False, 'error': 'BoomError: x',"
        " 'file': 'bad.c'}, ensure_ascii=False), flush=True)\n",
        encoding="utf-8",
    )

    env = dict(os.environ)
    env["PYTHONPATH"] = str(Path(__file__).resolve().parent.parent / "src")

    def _fake_start(self: Any) -> None:
        import subprocess as sp
        import threading as th

        self._generation += 1
        gen = self._generation
        self._proc = sp.Popen(
            [sys.executable, str(err_serve)],
            stdin=sp.PIPE,
            stdout=sp.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
        )
        assert self._proc.stdout is not None
        self._reader = th.Thread(target=self._read_loop, args=(gen,), daemon=True)
        self._reader.start()

    monkeypatch.setattr(worker_mod.SemanticWorkerSession, "_start", _fake_start)

    results, errors = run_jobs_isolated(
        [("bad.c", "int x(void) { return 1; }")], timeout_seconds=30
    )
    assert errors == []  # success:false 是受控错误（不是 crash），进入 FileSemantics.errors
    assert results[0].errors
    entry = json.loads(results[0].errors[0])
    assert entry["type"] == "semantic_worker_error"
    assert entry["file"] == "bad.c"
    assert entry["stage"] == "extract"
    assert entry["recoverable"] is True


def test_oversized_file_index_still_generated(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    """大文件跳过不阻塞 Index（enrich 链路正常）。"""
    from agentx.plan.service import enrich_index
    from agentx.understanding.graph import ProjectGraph

    (tmp_path / "main.c").write_text("int main(void) { return 0; }\n", encoding="utf-8")
    monkeypatch.setenv("AGENTX_SEMANTIC_MAX_FILE_SIZE_MB", "0.1")
    big = _big_source(0.5)
    (tmp_path / "font_big.c").write_text(big, encoding="utf-8")

    def _graph(root):
        return ProjectGraph(
            source="codegraph",
            files=[
                {"path": "main.c", "language": "c"},
                {"path": "font_big.c", "language": "c"},
            ],
            symbols=[
                {"name": "main", "type": "function", "file": "main.c", "start_line": 1},
                {"name": "font_data", "type": "variable", "file": "font_big.c", "start_line": 1},
            ],
            call_graph=[],
            include_map={},
            build_info={},
            errors=[],
        )

    monkeypatch.setattr("agentx.plan.service.analyze_project", _graph)
    index, _ = enrich_index(tmp_path)
    # Index 正常生成
    assert index.file_count > 0
    # 大文件符号保留（CodeGraph 原始 symbol）
    assert any(s["name"] == "font_data" for s in index.symbols)
    # semantic_skip 进入 errors
    assert any("semantic_skip" in e for e in index.errors)


# ---------- Case 3: 连续批量解析（1000 文件，每文件独立 Parser） ----------


def test_batch_1000_files_no_state_pollution() -> None:
    n = 1000
    sources = [
        f"int func_{i}(int a, int b) {{ return a + b; }}\n#define M_{i} {i}\n" for i in range(n)
    ]
    results = []
    for i, src in enumerate(sources):
        results.append(extract_file_semantics(f"f_{i}.c", src))
    # 全部解析完成、无崩溃
    assert len(results) == n
    # 抽样验证无状态污染（提取正确性）
    for i in (0, 1, 499, 999):
        sem = results[i]
        assert len(sem.functions) == 1
        assert sem.functions[0].name == f"func_{i}"
        assert sem.functions[0].signature.text == f"int func_{i}(int a, int b)"
        assert sem.macros[0].name == f"M_{i}"
    # 无错误
    assert all(not r.errors for r in results)


# ---------- Case 4: worker 崩溃隔离（serve 模式） ----------


def test_worker_crash_isolated(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    """worker 进程 abort（模拟 native SIGSEGV）→ 该文件失败，其他文件继续。"""
    from agentx.semantic import worker as worker_mod

    crash_serve = tmp_path / "crash_serve.py"
    crash_serve.write_text(
        "import sys, json\n"
        "from agentx.semantic.worker import _handle_request\n"
        "for line in sys.stdin:\n"
        "    line = line.strip()\n"
        "    if not line:\n"
        "        continue\n"
        "    if 'boom.c' in line:\n"
        "        import os; os.abort()\n"
        "    out = _handle_request(line)\n"
        "    print(json.dumps(out, ensure_ascii=False), flush=True)\n",
        encoding="utf-8",
    )

    env = dict(os.environ)
    env["PYTHONPATH"] = str(Path(__file__).resolve().parent.parent / "src")

    def _fake_start(self: Any) -> None:
        import subprocess as sp
        import threading as th

        self._generation += 1
        gen = self._generation
        self._proc = sp.Popen(
            [sys.executable, str(crash_serve)],
            stdin=sp.PIPE,
            stdout=sp.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
        )
        assert self._proc.stdout is not None
        self._reader = th.Thread(target=self._read_loop, args=(gen,), daemon=True)
        self._reader.start()

    monkeypatch.setattr(worker_mod.SemanticWorkerSession, "_start", _fake_start)

    jobs = [
        ("boom.c", "int bad(void) { return 1; }"),
        ("good1.c", "int ok1(void) { return 1; }"),
        ("good2.c", "int ok2(void) { return 2; }"),
    ]
    results, errors = run_jobs_isolated(jobs, timeout_seconds=30)
    # 主进程继续、其他文件恢复
    assert len(results) == 3
    assert results[1].functions and results[1].functions[0].name == "ok1"
    assert results[2].functions and results[2].functions[0].name == "ok2"
    # 坏文件记录结构化 crash 错误
    crash = [e for e in errors if "semantic_worker_crash" in e]
    assert crash
    entry = json.loads(crash[0])
    assert entry["file"] == "boom.c"
    assert entry["reason"] == "native_process_exit"
    # Phase 7.9：诊断字段（stage/recoverable）
    assert entry["stage"] == "parser"
    assert entry["recoverable"] is True


def test_worker_consecutive_crashes_queue_isolated(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """连续两个文件 crash：队列分代隔离，后续正常文件不误报（stale eof 丢弃）。"""
    from agentx.semantic import worker as worker_mod

    crash_serve = tmp_path / "crash2_serve.py"
    crash_serve.write_text(
        "import sys, json\n"
        "from agentx.semantic.worker import _handle_request\n"
        "for line in sys.stdin:\n"
        "    line = line.strip()\n"
        "    if not line:\n"
        "        continue\n"
        "    if 'boom1.c' in line or 'boom2.c' in line:\n"
        "        import os; os.abort()\n"
        "    out = _handle_request(line)\n"
        "    print(json.dumps(out, ensure_ascii=False), flush=True)\n",
        encoding="utf-8",
    )

    env = dict(os.environ)
    env["PYTHONPATH"] = str(Path(__file__).resolve().parent.parent / "src")

    def _fake_start(self: Any) -> None:
        import subprocess as sp
        import threading as th

        self._generation += 1
        gen = self._generation
        self._proc = sp.Popen(
            [sys.executable, str(crash_serve)],
            stdin=sp.PIPE,
            stdout=sp.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
        )
        assert self._proc.stdout is not None
        self._reader = th.Thread(target=self._read_loop, args=(gen,), daemon=True)
        self._reader.start()

    monkeypatch.setattr(worker_mod.SemanticWorkerSession, "_start", _fake_start)

    jobs = [
        ("boom1.c", "int b1(void) { return 1; }"),
        ("boom2.c", "int b2(void) { return 1; }"),
        ("good.c", "int ok(void) { return 1; }"),
    ]
    results, errors = run_jobs_isolated(jobs, timeout_seconds=30)
    assert len(results) == 3
    assert results[2].functions and results[2].functions[0].name == "ok"
    crashes = [json.loads(e) for e in errors if "semantic_worker_crash" in e]
    assert [c["file"] for c in crashes] == ["boom1.c", "boom2.c"]


def test_worker_retry_recovers_after_crash(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    """崩溃文件重试（retry queue）：首次 crash，新 worker 重试成功 → 无错误记录。"""
    from agentx.semantic import worker as worker_mod

    counter_file = tmp_path / "counter.txt"
    retry_serve = tmp_path / "retry_serve.py"
    retry_serve.write_text(
        "import sys, json, pathlib\n"
        "from agentx.semantic.worker import _handle_request\n"
        f"counter = pathlib.Path({json.dumps(str(counter_file))})\n"
        "for line in sys.stdin:\n"
        "    line = line.strip()\n"
        "    if not line:\n"
        "        continue\n"
        "    job = json.loads(line)\n"
        "    n = 0\n"
        "    if counter.exists():\n"
        "        n = int(counter.read_text())\n"
        "    counter.write_text(str(n + 1))\n"
        "    if job['file'] == 'flaky.c' and n == 0:\n"
        "        import os; os.abort()\n"
        "    out = _handle_request(line)\n"
        "    print(json.dumps(out, ensure_ascii=False), flush=True)\n",
        encoding="utf-8",
    )

    env = dict(os.environ)
    env["PYTHONPATH"] = str(Path(__file__).resolve().parent.parent / "src")

    def _fake_start(self: Any) -> None:
        import subprocess as sp
        import threading as th

        self._generation += 1
        gen = self._generation
        self._proc = sp.Popen(
            [sys.executable, str(retry_serve)],
            stdin=sp.PIPE,
            stdout=sp.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
        )
        assert self._proc.stdout is not None
        self._reader = th.Thread(target=self._read_loop, args=(gen,), daemon=True)
        self._reader.start()

    monkeypatch.setattr(worker_mod.SemanticWorkerSession, "_start", _fake_start)

    jobs = [
        ("flaky.c", "int flaky(void) { return 1; }"),
        ("steady.c", "int steady(void) { return 2; }"),
    ]
    results, errors = run_jobs_isolated(jobs, timeout_seconds=30)
    assert errors == []  # 重试成功 → 无错误
    assert results[0].functions and results[0].functions[0].name == "flaky"
    assert results[1].functions and results[1].functions[0].name == "steady"


def test_worker_module_entry_smoke() -> None:
    """worker_main 子进程入口：正常输入返回结果。"""
    proc = subprocess.run(
        [sys.executable, "-m", "agentx.semantic.worker"],
        input=json.dumps({"jobs": [{"file": "x.c", "source": "int foo(void) { return 1; }"}]}),
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
    )
    assert proc.returncode == 0
    data = json.loads(proc.stdout)
    assert data["results"][0]["functions"][0]["name"] == "foo"


def test_merge_worker_mode_flag() -> None:
    """worker_mode 配置：默认启用（隔离是默认行为），可关闭。"""
    from agentx.config.config import resolve_semantic_config

    cfg = resolve_semantic_config()
    assert cfg["worker_mode"] is True  # Phase 7.8.2：默认启用隔离
    assert cfg["max_file_size_mb"] == 5.0
    assert cfg["worker_timeout_seconds"] == 30.0

    os.environ["AGENTX_SEMANTIC_WORKER_MODE"] = "0"
    try:
        assert resolve_semantic_config()["worker_mode"] is False
    finally:
        os.environ.pop("AGENTX_SEMANTIC_WORKER_MODE", None)


def test_merge_semantics_worker_mode(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    """worker_mode=true 时 merge 全链路走隔离解析。"""
    from agentx.config.config import load_config, save_config

    (tmp_path / "a.c").write_text("int alpha(void) { return 1; }\n", encoding="utf-8")
    (tmp_path / "b.c").write_text("int beta(void) { return 2; }\n", encoding="utf-8")
    cfg = load_config()
    cfg.semantic = {"worker_mode": True}
    save_config(cfg, tmp_path / "agentx_config.json")
    monkeypatch.setattr(
        "agentx.config.config.default_config_path", lambda: tmp_path / "agentx_config.json"
    )
    # merge 内部走 run_jobs_isolated（subprocess）；验证结果正确
    symbols = [
        {"name": "alpha", "type": "function", "file": "a.c", "start_line": 1},
    ]
    merged, errors, _indirect, _sem = merge_semantics(symbols, tmp_path, ["a.c", "b.c"])
    assert errors == []
    alpha = next(s for s in merged if s["name"] == "alpha")
    assert alpha["signature"]["text"] == "int alpha(void)"
    beta = next(s for s in merged if s["name"] == "beta")
    assert beta["signature"]["text"] == "int beta(void)"


# ---------- 语法错误误报过滤（Phase 7.8.3） ----------


def test_has_error_without_real_node_not_reported() -> None:
    """has_error 保守标志：仅当存在真实 ERROR/missing 节点时才记录语法错误。"""
    from agentx.semantic.extractor import _has_real_error, _make_parser

    # 干净 C 文件：无真实错误节点
    clean = "int foo(int a) { return a; }\n"
    tree = _make_parser().parse(clean.encode("utf-8"))
    assert not tree.root_node.has_error
    assert _has_real_error(tree.root_node) is False

    # 真实语法残缺：存在 ERROR/missing
    broken = "void broken( {\n    return;\n"
    tree2 = _make_parser().parse(broken.encode("utf-8"))
    assert _has_real_error(tree2.root_node) is True

    # 外部场景（350A lcd.h）：preproc 平衡 + has_error 置位但无真实错误节点
    # → 不记录语法错误，提取完整（26 函数 / 32 宏）
    header = (
        "#ifndef __X_H\n"
        "#define __X_H\n"
        "#define M1(x)        do{ x ? 1 : 2; }while(0)   /* 注释 */\n"
        "#define M2           0x01    /* 中文注释 */\n"
        "int foo(int a);\n"
        "int bar(void);\n"
        "#endif\n"
    )
    sem = extract_file_semantics("x.h", header)
    assert sem.errors == []
    assert len(sem.functions) == 2


def test_real_error_still_reported() -> None:
    """真实语法残缺（函数体缺大括号）→ 仍记录语法错误（结构化）。"""
    src = "void broken( {\n    return;\n"
    sem = extract_file_semantics("broken.c", src)
    assert any(
        json.loads(e).get("type") == "semantic_partial"
        and json.loads(e).get("reason") == "syntax_error"
        for e in sem.errors
        if e.startswith("{")
    )


def test_tree_sitter_version_pinned() -> None:
    """回归保护：tree-sitter 锁定 0.25.x（0.26.0 binding 有 native 内存 bug）。"""
    pyproject = Path(__file__).resolve().parent.parent / "pyproject.toml"
    text = pyproject.read_text(encoding="utf-8")
    m = re.search(r'"tree-sitter([^"]*)"', text)
    assert m, "pyproject.toml 缺少 tree-sitter 依赖声明"
    spec = m.group(1)
    assert ">=0.25.2" in spec and "<0.26" in spec, f"tree-sitter 应锁定 >=0.25.2,<0.26: {spec}"


# ---------- Case 5: MCP 场景（status → plan，server 不退出） ----------


@pytest.mark.asyncio
async def test_mcp_plan_with_worker_default(
    tmp_path, monkeypatch: pytest.MonkeyPatch, gate_bypass: None
) -> None:
    """MCP plan 全链路（worker 默认启用）：server 不退出、Index 生成、semantic 通过隔离完成。"""
    import agentx.mcp.server as mcp_server
    from agentx.app.application import Application
    from agentx.index.index import load_index
    from agentx.providers.mock import MockProvider, text_response
    from agentx.understanding.graph import ProjectGraph

    (tmp_path / "User").mkdir()
    (tmp_path / "User" / "main.c").write_text(
        '#include "main.h"\nint main(void) { return run(); }\n', encoding="utf-8"
    )
    (tmp_path / "User" / "app.c").write_text(
        '#include "main.h"\nint run(void) { return key_scan(); }\n', encoding="utf-8"
    )
    (tmp_path / "User" / "main.h").write_text(
        "#ifndef M_H\n#define M_H\nint run(void);\nint key_scan(void);\n#endif\n",
        encoding="utf-8",
    )

    def _graph(root):
        return ProjectGraph(
            source="codegraph",
            files=[
                {"path": "User/main.c", "language": "c", "scope_type": "project"},
                {"path": "User/app.c", "language": "c", "scope_type": "project"},
                {"path": "User/main.h", "language": "c", "scope_type": "project"},
            ],
            symbols=[
                {"name": "main", "type": "function", "file": "User/main.c", "start_line": 2},
                {"name": "run", "type": "function", "file": "User/app.c", "start_line": 2},
                {"name": "key_scan", "type": "function", "file": "User/main.h", "start_line": 4},
            ],
            call_graph=[
                {"caller": "main", "callee": "run", "confidence": "high", "file": "User/main.c"}
            ],
            include_map={"User/main.c": ["User/main.h"], "User/app.c": ["User/main.h"]},
            build_info={},
            errors=[],
        )

    monkeypatch.setattr("agentx.plan.service.analyze_project", _graph)

    plan_json = (
        '{"summary": "ok", "steps": [{"action": "fix", "file": "User/app.c", "change": "x"}], '
        '"files_involved": ["User/app.c"], "risks": [], "verification": "echo ok"}'
    )
    app = Application(tmp_path)
    app.orchestrator.agents["plan"].provider = MockProvider().respond(
        text_response("分析完成"), text_response(plan_json)
    )
    monkeypatch.setattr(mcp_server, "_app", lambda path: app)

    result = await mcp_server.agentx(str(tmp_path), "修复 run 函数", action="plan")
    # server 不退出（正常返回结构）
    assert result["result"]["index_after"]["status"] in ("VALID", "STALE", "MISSING")
    # Index 生成且 semantic 通过 worker 隔离完成
    index = load_index(tmp_path)
    assert index is not None
    assert (index.capabilities or {}).get("semantic", {}).get("enabled") is True
    run_sym = next(s for s in index.symbols if s["name"] == "run")
    assert run_sym.get("signature", {}).get("text") == "int run(void)"
