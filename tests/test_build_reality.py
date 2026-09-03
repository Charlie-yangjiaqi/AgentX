"""Phase 7.5：Keil Build Reality Integration。

Case 1: uvprojx 解析（target/cpu/groups/files/defines/active）
Case 2: Query 实体按键 → key.c compiled=true
Case 3: excluded 文件不推荐为正式实现
Case 4: 无 Keil 工程 → build_status=unknown（不编造）
+ build query（文件为什么没效果）、defines 只进证据不做推断
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from agentx.build import (
    build_query_from_info,
    build_status_from_info,
    parse_keil_project,
)
from agentx.index.index import ProjectIndex
from agentx.query.evidence import build_evidence_card
from agentx.query.feature import search_feature


def _uvprojx_xml() -> str:
    return """<Project>
<Targets>
<Target>
<TargetName>GD32F427_Release</TargetName>
<TargetOption>
<TargetCommonOption><Device>GD32F427</Device></TargetCommonOption>
<TargetArmAds><Cads><VariousControls><Define>USE_FREERTOS,GD32F427</Define></VariousControls></Cads></TargetArmAds>
</TargetOption>
<Groups>
<Group><GroupName>User</GroupName><Files>
<File><FileName>User/main.c</FileName></File>
<File><FileName>User/app_controller.c</FileName></File>
</Files></Group>
<Group><GroupName>Drivers/BSP/KEY</GroupName><Files>
<File><FileName>Drivers/BSP/KEY/key.c</FileName></File>
<File><FileName>Drivers/BSP/KEY/key.h</FileName></File>
<File><FileName>Drivers/BSP/KEY/key_test.c</FileName><IncludeInBuild>0</IncludeInBuild></File>
</Files></Group>
</Groups>
</Target>
<Target>
<TargetName>Bootloader</TargetName>
<Groups>
<Group><GroupName>Boot</GroupName><Files>
<File><FileName>Boot/boot.c</FileName></File>
</Files></Group>
</Groups>
</Target>
</Targets>
<SelectTargetNo>0</SelectTargetNo>
</Project>"""


def _uvprojx(tmp_path: Path) -> Path:
    p = tmp_path / "GD32F427.uvprojx"
    p.write_text(_uvprojx_xml(), encoding="utf-8")
    return p


def _index(build_info: dict[str, Any] | None = None) -> ProjectIndex:
    build_info = build_info or {
        "system": "keil",
        "build_source": "keil",
        "target": "GD32F427_Release",
        "cpu": None,
        "defines": ["USE_FREERTOS", "GD32F427"],
        "project_file": "GD32F427.uvprojx",
        "compiled_files": [
            {"file": "User/main.c", "compiled": True},
            {"file": "Drivers/BSP/KEY/key.c", "compiled": True},
        ],
        "excluded_files": [{"file": "Drivers/BSP/KEY/key_test.c", "compiled": False}],
        "has_build_config": True,
    }
    return ProjectIndex(
        project_fingerprint="fp",
        index_version="1.3",
        generated_at=datetime(2026, 8, 29, tzinfo=UTC),
        files=[
            {"path": "User/main.c", "status": "active", "compile_status": "compiled"},
            {"path": "Drivers/BSP/KEY/key.c", "status": "active", "compile_status": "compiled"},
            {
                "path": "Drivers/BSP/KEY/key_test.c",
                "status": "active",
                "compile_status": "excluded",
            },
        ],
        symbols=[
            {
                "name": "key_scan",
                "type": "function",
                "file": "Drivers/BSP/KEY/key.c",
                "start_line": 10,
                "end_line": 30,
            },
            {
                "name": "main",
                "type": "function",
                "file": "User/main.c",
                "start_line": 1,
                "end_line": 50,
            },
            {
                "name": "key_scan_test",
                "type": "function",
                "file": "Drivers/BSP/KEY/key_test.c",
                "start_line": 1,
                "end_line": 20,
            },
        ],
        call_graph=[
            {
                "caller": "main",
                "callee": "key_scan",
                "confidence": "high",
                "file": "User/main.c",
                "line": 5,
            },
        ],
        include_map={"key.c": ["key.h"]},
        build_info=build_info,
        file_count=3,
    )


# ---------- Case 1: uvprojx 解析 ----------


def test_parse_targets_cpu_defines_groups(tmp_path: Path) -> None:
    project = parse_keil_project(_uvprojx(tmp_path))
    assert project.target_name == "GD32F427_Release"  # SelectTargetNo=0 → 第一个
    assert project.target_cpu is None  # 无 Cpu 元素
    assert project.defines == ["USE_FREERTOS", "GD32F427"]
    targets = {t.name for t in project.targets}
    assert targets == {"GD32F427_Release", "Bootloader"}
    assert project.active_target is not None
    # groups
    groups = {g.name: [f.path for f in g.files] for g in project.active_target.groups}
    assert "Drivers/BSP/KEY" in groups
    assert "Drivers/BSP/KEY/key.c" in groups["Drivers/BSP/KEY"]
    # compiled / excluded
    compiled = {f.path for f in project.active_target.compiled_files}
    excluded = {f.path for f in project.active_target.excluded_files}
    assert "Drivers/BSP/KEY/key.c" in compiled
    assert "User/main.c" in compiled
    assert "Drivers/BSP/KEY/key_test.c" in excluded


def test_active_target_selected_by_number(tmp_path: Path) -> None:
    xml = _uvprojx_xml().replace(
        "<SelectTargetNo>0</SelectTargetNo>", "<SelectTargetNo>1</SelectTargetNo>"
    )
    p = tmp_path / "p.uvprojx"
    p.write_text(xml, encoding="utf-8")
    project = parse_keil_project(p)
    assert project.target_name == "Bootloader"


def test_active_target_user_parameter_wins(tmp_path: Path) -> None:
    project = parse_keil_project(_uvprojx(tmp_path), target_name="Bootloader")
    assert project.target_name == "Bootloader"


def test_parse_invalid_project_returns_empty(tmp_path: Path) -> None:
    p = tmp_path / "bad.uvprojx"
    p.write_text("not xml", encoding="utf-8")
    project = parse_keil_project(p)
    assert project.targets == []
    assert project.active_target is None
    assert project.target_name == ""


# ---------- Case 2: Query → compiled=true ----------


def test_feature_query_shows_build_membership() -> None:
    result = search_feature(_index(), "找实体按键")
    assert result["confidence"] == "high"
    assert result["build"]["compile_status"] == "compiled"
    assert result["build"]["target"] == "GD32F427_Release"
    assert "USE_FREERTOS" in result["build"]["defines"]
    # 证据卡 Build 块
    card = build_evidence_card(_index(), result)
    assert "compile_status: compiled" in card
    assert "GD32F427_Release" in card
    assert "defines: USE_FREERTOS, GD32F427" in card


# ---------- Case 3: excluded 不作正式实现推荐 ----------


def test_excluded_file_not_recommended() -> None:
    """key_test.c（excluded）不被推荐为正式实现。"""
    result = search_feature(_index(), "key test 测试按键")
    # 命中 excluded 文件 → 不推荐 answer
    paths = {f["path"] for f in result["files"]}
    assert "Drivers/BSP/KEY/key_test.c" in paths
    assert result["recommended_action"]["type"] == "read_source"
    assert "非正式固件路径" in result["summary"]


def test_excluded_flag_in_evidence_card() -> None:
    index = _index()
    index.build_info = {
        **index.build_info,
        "compiled_files": [],
        "excluded_files": [
            {"file": "Drivers/BSP/KEY/key_test.c", "compiled": False},
            {"file": "Drivers/BSP/KEY/key.c", "compiled": False},
            {"file": "User/main.c", "compiled": False},
        ],
    }
    for f in index.files:
        if f.path in ("Drivers/BSP/KEY/key.c", "User/main.c", "Drivers/BSP/KEY/key_test.c"):
            f.compile_status = "excluded"
    result = search_feature(index, "找实体按键")
    card = build_evidence_card(index, result)
    assert "compile_status: excluded" in card
    assert "not in production firmware" in card


# ---------- Case 4: 无 Keil → unknown ----------


def test_build_status_unknown_without_project() -> None:
    result = build_status_from_info({})
    assert result["build_status"] == "unknown"
    result2 = build_status_from_info({"system": "unknown"})
    assert result2["build_status"] == "unknown"


def test_build_status_valid_summary() -> None:
    result = build_status_from_info(_index().build_info)
    assert result["build_status"] == "valid"
    assert result["system"] == "keil"
    assert result["target"] == "GD32F427_Release"
    assert result["compiled_count"] == 2
    assert result["excluded_count"] == 1
    assert result["defines"] == ["USE_FREERTOS", "GD32F427"]
    assert result["project_file"] == "GD32F427.uvprojx"


# ---------- Build Query（文件为什么没效果） ----------


def test_build_query_compiled_file() -> None:
    result = build_query_from_info(_index().build_info, "Drivers/BSP/KEY/key.c")
    assert result["exists"] is True
    assert result["compiled"] is True
    assert result["excluded"] is False
    assert result["target"] == "GD32F427_Release"
    assert "USE_FREERTOS" in result["defines"]


def test_build_query_excluded_file() -> None:
    result = build_query_from_info(_index().build_info, "Drivers/BSP/KEY/key_test.c")
    assert result["exists"] is True
    assert result["compiled"] is False
    assert result["excluded"] is True
    assert result["status"] == "excluded"


def test_build_query_unknown_file_and_no_build() -> None:
    result = build_query_from_info(_index().build_info, "Drivers/XXX/ghost.c")
    assert result["exists"] is False
    assert result["status"] == "not_in_project"
    empty = build_query_from_info({}, "key.c")
    assert empty["build_status"] == "unknown"


# ---------- MCP ----------


@pytest.mark.asyncio
async def test_mcp_build_status_action(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import agentx.mcp.server as mcp_server
    from agentx.index.index import save_index
    from agentx.providers.mock import MockProvider, text_response
    from tests.helpers import EXPLORE_RESPONSE

    (tmp_path / "key.c").write_text("int key_scan(void) { return 1; }\n", encoding="utf-8")
    app = mcp_server._app(str(tmp_path))
    app.orchestrator.agents["plan"].provider = MockProvider().respond(
        text_response(EXPLORE_RESPONSE),
        text_response('{"summary": "s", "files_involved": ["key.c"], "verification": "echo ok"}'),
    )
    monkeypatch.setattr(mcp_server, "_app", lambda path: app)
    await mcp_server.agentx(str(tmp_path), "任务", action="plan")

    from agentx.index.index import load_index

    index = load_index(tmp_path)
    assert index is not None
    index.build_info = {
        "system": "keil",
        "build_source": "keil",
        "target": "GD32F427_Release",
        "defines": ["USE_FREERTOS"],
        "compiled_files": [{"file": "key.c", "compiled": True}],
        "excluded_files": [],
        "has_build_config": True,
        "project_file": "app.uvprojx",
    }
    for f in index.files:
        if f.path == "key.c":
            f.compile_status = "compiled"
    save_index(tmp_path, index)

    r = await mcp_server.agentx(str(tmp_path), "", action="build_status")
    assert r["result"]["build_status"] == "valid"
    assert r["result"]["target"] == "GD32F427_Release"
    assert r["result"]["compiled_count"] == 1

    # query type=build
    r2 = await mcp_server.agentx(str(tmp_path), "key.c", action="query", query_type="build")
    assert r2["result"]["compiled"] is True
    assert r2["result"]["target"] == "GD32F427_Release"
