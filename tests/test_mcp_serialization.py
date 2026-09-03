"""MCP response 序列化防护层测试（Phase 7.9.1）。

覆盖（任务 5）：
1. 非法 surrogate 字符进入 response → 正常返回可序列化 JSON
2. 非 UTF-8 文件名（surrogateescape 路径）→ response 不崩溃
3. binary 文件路径进入 error/event → 自动替换非法字符
4. 正常中文路径 → 保持原样
5. auto 大项目结果 → serialization 通过（json.dumps 模拟 SDK 序列化层）
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from agentx.mcp.sanitize import sanitize_str, sanitize_value

SURROGATE = "\udc80"  # Windows surrogateescape 常见非法字符


def _serialize_sim(data: Any) -> str:
    """模拟 MCP SDK 输出层（pydantic 序列化的底层 utf-8 编码）。

    json.dumps 对 surrogate 抛 UnicodeEncodeError（与 PydanticSerializationError
    同一底层异常）；清洗后必须能正常编码。
    """
    return json.dumps(data, ensure_ascii=False)


# ---------- 单元：sanitize_str / sanitize_value ----------


def test_sanitize_surrogate_replaced_with_question_mark() -> None:
    """ "\\udc80abc" → "?abc"（任务要求示例）。"""
    assert sanitize_str(SURROGATE + "abc") == "?abc"


def test_sanitize_high_surrogate_replaced() -> None:
    """高位代理（\\ud800-\\udbff）同样替换。"""
    assert sanitize_str("\ud800x") == "?x"


def test_sanitize_chinese_preserved() -> None:
    """正常中文路径/文本保持原样。"""
    s = "D:\\工程\\主目录\\User\\hmi_按键.c"
    assert sanitize_str(s) == s


def test_sanitize_ascii_preserved() -> None:
    assert sanitize_str("int foo(void) { return 1; }") == "int foo(void) { return 1; }"


def test_sanitize_mixed_surrogate_and_chinese() -> None:
    """非法字符替换、中文保留。"""
    s = "路径\\dc" + SURROGATE + "中文名.c"
    out = sanitize_str(s)
    assert "?" in out
    assert "中文名.c" in out
    _serialize_sim(out)  # 可序列化


def test_sanitize_bytes_decoded_with_replace() -> None:
    """bytes（二进制内容）→ replace 解码 + 清洗。"""
    raw = b"bad\xff\xfe" + "路径".encode() + SURROGATE.encode("utf-8", "replace")
    out = sanitize_value(raw)
    assert isinstance(out, str)
    _serialize_sim(out)


def test_sanitize_value_recursive_dict_list() -> None:
    """递归清洗：dict/list/tuple/set 内所有字符串。"""
    data = {
        "path": SURROGATE + "abc",
        "nested": {"error": "x" + SURROGATE, "ok": "正常路径.c"},
        "list": [SURROGATE + "e", {"f": SURROGATE}],
        "num": 42,
        "flag": True,
        "none": None,
    }
    out = sanitize_value(data)
    assert out["path"] == "?abc"
    assert out["nested"]["error"] == "x?"
    assert out["nested"]["ok"] == "正常路径.c"
    assert out["list"][0] == "?e"
    assert out["list"][1]["f"] == "?"
    assert out["num"] == 42 and out["flag"] is True and out["none"] is None
    _serialize_sim(out)


def test_sanitize_value_non_string_objects_preserved() -> None:
    """非 str/bytes 对象原样保留（不破坏结构）。"""
    marker = object()
    out = sanitize_value({"obj": marker})
    assert out["obj"] is marker


# ---------- 集成：MCP agentx 返回入口 ----------


async def _invoke_agentx(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, payload: Any) -> Any:
    """直调 server.agentx（monkeypatch 掉内部 action 返回污染数据）。"""
    import agentx.mcp.server as mcp_server

    async def _fake_handler(
        app: Any,
        task: str,
        origin: str = "unknown",
        force_rebuild: bool = False,
        events: Any = None,
        scope_selections: Any = None,
        decision_choice: str | None = None,
        accept_blocked: bool = False,
    ) -> dict[str, Any]:
        return payload

    monkeypatch.setattr(mcp_server, "_ACTIONS", {"plan": _fake_handler})
    return await mcp_server.agentx(str(tmp_path), "测试任务", action="plan")


@pytest.mark.asyncio
async def test_mcp_response_surrogate_in_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """非法 surrogate 进入 result payload → response 正常序列化。"""
    payload = {
        "status": "ok",
        "project": SURROGATE + "path",
        "summary": "修复 " + SURROGATE + " 问题",
    }
    out = await _invoke_agentx(monkeypatch, tmp_path, payload)
    assert "?" in out["result"]["project"]
    _serialize_sim(out)  # 不抛 UnicodeEncodeError


@pytest.mark.asyncio
async def test_mcp_response_non_utf8_filename_in_events(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """非 UTF-8 文件名进入 events（workflow 事件）→ response 不崩溃。"""
    payload = {
        "status": "ok",
        "index_fingerprint": "abc123",
        "events": [{"stage": "index_sync", "message": "同步 " + SURROGATE + "file.c"}],
    }
    out = await _invoke_agentx(monkeypatch, tmp_path, payload)
    text = _serialize_sim(out)
    assert "?" in text
    assert "\udc80" not in text


@pytest.mark.asyncio
async def test_mcp_response_binary_path_in_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """binary 文件路径进入 error/llm_error → 自动替换非法字符。"""
    payload = {
        "error": "解析失败: " + SURROGATE + SURROGATE + ".bin",
        "llm_error": {"category": "parse", "detail": SURROGATE + "raw"},
    }
    out = await _invoke_agentx(monkeypatch, tmp_path, payload)
    text = _serialize_sim(out)
    assert "\udc80" not in text
    assert "??.bin" in text


@pytest.mark.asyncio
async def test_mcp_response_chinese_path_preserved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """正常中文路径 → 保持原样（不清洗合法文本）。"""
    payload = {
        "status": "ok",
        "project": "D:\\工程\\主目录\\User\\按键处理.c",
        "index_dir": "D:\\工程\\.agents",
    }
    out = await _invoke_agentx(monkeypatch, tmp_path, payload)
    text = _serialize_sim(out)
    assert "按键处理.c" in text
    assert "工程" in text


@pytest.mark.asyncio
async def test_mcp_response_unknown_action_error_sanitized(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """未知 action error 分支同样清洗（异常字符串不破坏 response）。"""
    import agentx.mcp.server as mcp_server

    out = await mcp_server.agentx(str(tmp_path), "t", action="nope")
    _serialize_sim(out)
    assert "未知 action" in out["error"]


# ---------- 集成：auto 全链路（真实工程 + mock LLM） ----------


@pytest.mark.asyncio
async def test_mcp_auto_full_result_serializable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, gate_bypass: None
) -> None:
    """auto 大项目结果：index 正常生成 + response 可序列化（无编码异常）。"""
    import agentx.mcp.server as mcp_server
    from agentx.app.application import Application
    from agentx.providers.mock import MockProvider, text_response
    from agentx.understanding.graph import ProjectGraph

    root = tmp_path / "工程"
    (root / "User").mkdir(parents=True)
    (root / "User" / "main.c").write_text(
        '#include "main.h"\nint main(void) { return run(); }\n', encoding="utf-8"
    )
    (root / "User" / "app.c").write_text(
        '#include "main.h"\nint run(void) { return key_scan(); }\n', encoding="utf-8"
    )
    (root / "User" / "main.h").write_text(
        "#ifndef M_H\n#define M_H\nint run(void);\nint key_scan(void);\n#endif\n",
        encoding="utf-8",
    )
    # 中文文件名（Windows 合法）：路径字段覆盖真实中文场景
    (root / "User" / "按键处理.c").write_text("void key_handle(void) { }\n", encoding="utf-8")

    def _graph(r: Path):
        return ProjectGraph(
            source="codegraph",
            files=[
                {"path": "User/main.c", "language": "c"},
                {"path": "User/app.c", "language": "c"},
                {"path": "User/main.h", "language": "c"},
                {"path": "User/按键处理.c", "language": "c"},
            ],
            symbols=[
                {"name": "main", "type": "function", "file": "User/main.c", "start_line": 1},
                {"name": "run", "type": "function", "file": "User/app.c", "start_line": 1},
                {"name": "key_scan", "type": "function", "file": "User/main.h", "start_line": 1},
                {
                    "name": "key_handle",
                    "type": "function",
                    "file": "User/按键处理.c",
                    "start_line": 1,
                },
            ],
            call_graph=[],
            include_map={"User/main.c": ["User/main.h"], "User/app.c": ["User/main.h"]},
            build_info={},
            errors=[],
        )

    monkeypatch.setattr("agentx.plan.service.analyze_project", _graph)

    plan_json = (
        '{"summary": "ok", "steps": [{"action": "fix", "file": "User/app.c", "change": "x"}], '
        '"files_involved": ["User/app.c"], "risks": [], "verification": "echo ok"}'
    )
    app = Application(root)
    app.orchestrator.agents["plan"].provider = MockProvider().respond(
        text_response("分析完成"), text_response(plan_json)
    )
    app.orchestrator.agents["reviewer"].provider = MockProvider().respond(
        text_response("review done"),
        text_response('{"verdict": "approved", "findings": [], "summary": "ok"}'),
    )
    app.orchestrator.agents["verifier"].provider = MockProvider().respond(
        text_response("verify done"),
        text_response('{"verdict": "passed", "evidence": [], "summary": "ok"}'),
    )
    monkeypatch.setattr(mcp_server, "_app", lambda path: app)

    out = await mcp_server.agentx(str(root), "修复 run 函数", action="auto")
    # index 正常生成
    assert out["result"]["phase"] in ("complete", "scope_required")
    # response 可序列化（SDK 输出层模拟：无 UnicodeEncodeError）
    text = _serialize_sim(out)
    assert "工程" in text  # 中文项目路径保留
    assert "\udc80" not in text
    # 中文文件名进入 Index（symbols）
    from agentx.index.index import load_index

    index = load_index(root)
    assert index is not None
    assert any("按键处理.c" in str(s.get("file", "")) for s in index.symbols)


def test_sanitize_str_never_raises() -> None:
    """清洗函数本身绝不抛异常（任意输入安全）。"""
    for bad in ("\udcff", "\ud800", "\x00", "\udc80" * 1000, "a" * 10**6):
        out = sanitize_str(bad)
        _serialize_sim(out)
