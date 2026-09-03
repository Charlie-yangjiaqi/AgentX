"""Phase 7.9.2 MCP 体验：统一 bootstrap + 长任务后台化 测试。

Bootstrap（问题1）：
1. MISSING + sync → scope_required（不 skip）
2. scope 确认后 sync → 创建 Index
3. MISSING + understand → scope_required
4. 已有 Index → 原流程不变

Timeout（问题2）：
1. 模拟长时间 build → 返回 running + job_id（不阻塞 RPC）
2. 不产生假失败状态（job 最终 completed）
3. status(job_id) 可查询最终状态
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest

import agentx.mcp.server as mcp_server
from agentx.app.application import Application
from agentx.config.config import load_config
from agentx.index.index import load_index
from agentx.providers.mock import MockProvider, text_response
from agentx.understanding.graph import ProjectGraph


def _make_project(tmp_path: Path, n_files: int = 4, with_third_party: bool = True) -> Path:
    """构造工程：User 业务文件 + 可选 Middlewares（第三方触发 scope 建议）。"""
    root = tmp_path / "工程"
    (root / "User").mkdir(parents=True)
    if with_third_party:
        (root / "Middlewares" / "LVGL").mkdir(parents=True)
    for i in range(n_files):
        (root / "User" / f"app{i}.c").write_text(
            f"int app{i}_fn(void) {{ return {i}; }}\n", encoding="utf-8"
        )
    if with_third_party:
        (root / "Middlewares" / "LVGL" / "lv_core.c").write_text(
            "void lv_init(void) { }\n", encoding="utf-8"
        )
    return root


def _graph(root: Path) -> ProjectGraph:
    files = [{"path": f"User/app{i}.c", "language": "c"} for i in range(4)]
    symbols = [
        {"name": f"app{i}_fn", "type": "function", "file": f"User/app{i}.c", "start_line": 1}
        for i in range(4)
    ]
    return ProjectGraph(
        source="codegraph",
        files=files,
        symbols=symbols,
        call_graph=[],
        include_map={},
        build_info={},
        errors=[],
    )


def _app_for(root: Path) -> Application:
    app = Application(root, config=load_config())
    app.orchestrator.agents["plan"].provider = MockProvider().respond(
        text_response(
            '{"architecture_summary": "HMI 固件", "startup_flow": ["main"], '
            '"core_modules": ["User/app0.c"], "critical_files": []}'
        ),
        text_response("分析完成"),
        text_response(
            '{"summary": "ok", "steps": [{"action": "fix", "file": "User/app0.c", "change": "x"}], '
            '"files_involved": ["User/app0.c"], "risks": [], "verification": "echo ok"}'
        ),
    )
    return app


@pytest.fixture(autouse=True)
def _no_global_config(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """隔离：每个测试用独立 config 路径（避免真实 ~/.agentx 配置干扰 scope）。"""
    monkeypatch.setattr(
        "agentx.config.config.default_config_path",
        lambda: tmp_path / "agentx_test_config.json",
    )


# ---------- Bootstrap 1/2：sync MISSING → scope_required → 确认 → 创建 ----------


@pytest.mark.asyncio
async def test_sync_missing_returns_scope_required(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """MISSING + sync：不再 skip——统一 bootstrap 返回 scope_required（带建议）。"""
    root = _make_project(tmp_path)
    monkeypatch.setattr("agentx.plan.service.analyze_project", _graph)

    out = await mcp_server.agentx(str(root), "同步索引", action="sync")
    result = out["result"]
    assert result.get("action") == "scope_required"
    assert result.get("scope_status") == "scope_required"
    assert result.get("suggestions", {}).get("third_party")
    # 未绕过 scope gate：Index 未创建
    assert load_index(root) is None


@pytest.mark.asyncio
async def test_sync_with_scope_confirmation_creates_index(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """scope 确认后 sync → 创建 Index（含认知数据）。"""
    root = _make_project(tmp_path)
    monkeypatch.setattr("agentx.plan.service.analyze_project", _graph)

    selections = {"ignore": ["docs/**"], "third_party": ["Middlewares/**"]}
    out = await mcp_server.agentx(str(root), "同步索引", action="sync", scope_selections=selections)
    result = out["result"]
    assert result.get("action") == "created"
    index = load_index(root)
    assert index is not None
    assert any(f.path == "User/app0.c" for f in index.files)


# ---------- Bootstrap 3：understand MISSING → scope_required ----------


@pytest.mark.asyncio
async def test_understand_missing_returns_scope_required(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """MISSING + understand：不再 skipped——统一 bootstrap scope_required。"""
    root = _make_project(tmp_path)
    monkeypatch.setattr("agentx.plan.service.analyze_project", _graph)
    monkeypatch.setattr("agentx.understanding.graph.analyze_project", _graph)

    out = await mcp_server.agentx(str(root), "分析项目", action="understand")
    result = out["result"]
    assert result.get("status") == "scope_required"
    assert result.get("suggestions", {}).get("ignore") is not None
    assert load_index(root) is None


@pytest.mark.asyncio
async def test_understand_with_scope_creates_and_refreshes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """understand + scope 确认 → 建 Index + 理解刷新（不 skipped）。"""
    root = _make_project(tmp_path)
    monkeypatch.setattr("agentx.plan.service.analyze_project", _graph)
    monkeypatch.setattr("agentx.understanding.graph.analyze_project", _graph)
    # 预置 app（ensure_understanding 用 plan agent 探索）
    app = _app_for(root)
    monkeypatch.setattr(mcp_server, "_app", lambda path: app)

    selections = {"ignore": ["docs/**"], "third_party": ["Middlewares/**"]}
    out = await mcp_server.agentx(
        str(root), "分析项目", action="understand", scope_selections=selections
    )
    result = out["result"]
    assert result.get("status") != "scope_required"
    assert load_index(root) is not None
    u = result.get("understanding", {})
    assert u.get("status") in ("created", "refreshed", "reused")


# ---------- Bootstrap 4：已有 Index → 原流程不变 ----------


@pytest.mark.asyncio
async def test_sync_existing_index_unchanged_flow(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """已有 Index：sync 走增量维护（非 created/scope_required）。"""
    root = _make_project(tmp_path)
    monkeypatch.setattr("agentx.plan.service.analyze_project", _graph)
    # 先建 Index
    from agentx.plan.service import enrich_index

    enrich_index(root)
    before = load_index(root)
    assert before is not None

    out = await mcp_server.agentx(str(root), "同步", action="sync")
    result = out["result"]
    assert result.get("action") != "skip"  # VALID 时也不会 skip
    after = load_index(root)
    assert after is not None
    assert len(after.files) == len(before.files)


@pytest.mark.asyncio
async def test_status_without_job_keeps_read_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """status 纯读取：无 Index 时不自动创建。"""
    root = _make_project(tmp_path)
    out = await mcp_server.agentx(str(root), "", action="status")
    assert out["result"]["index_status"] in ("MISSING", "STALE", "VALID", "CORRUPTED")
    assert load_index(root) is None


# ---------- Timeout：长任务后台化 ----------


@pytest.mark.asyncio
async def test_long_build_returns_running_job(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """模拟长时间 build（真实 enrich > 窗口）：立即返回 running + job_id。"""
    root = _make_project(tmp_path, with_third_party=False)  # 无 scope 干扰 → 直接构建
    monkeypatch.setattr("agentx.plan.service.analyze_project", _graph)
    # 强制 job 化（小测试工程）：阈值降为 0 + 同步窗口极小（真实 enrich ~0.5s > 窗口）
    monkeypatch.setattr(mcp_server, "_JOB_SOURCE_THRESHOLD", 0)
    monkeypatch.setattr(mcp_server, "_SYNC_WINDOW_SECONDS", 0.01)

    t0 = time.time()
    out = await mcp_server.agentx(str(root), "同步", action="sync")
    elapsed = time.time() - t0
    assert elapsed < 2.0  # 未等 build 完成
    assert out["status"] == "running"
    assert out["job_id"]
    assert out["phase"] == "building_index"


@pytest.mark.asyncio
async def test_job_completes_without_false_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """后台任务最终 completed（不假失败）；status 可查询最终状态。"""
    from agentx.mcp.jobs import job_manager

    root = _make_project(tmp_path, with_third_party=False)
    monkeypatch.setattr("agentx.plan.service.analyze_project", _graph)
    monkeypatch.setattr(mcp_server, "_JOB_SOURCE_THRESHOLD", 0)
    monkeypatch.setattr(mcp_server, "_SYNC_WINDOW_SECONDS", 0.01)

    out = await mcp_server.agentx(str(root), "同步", action="sync")
    assert out["status"] == "running"
    job_id = out["job_id"]

    # 等待后台完成（测试内直接 await 任务）
    job = job_manager().get(job_id)
    assert job is not None and job.task is not None
    await asyncio.wait_for(job.task, timeout=30)

    # 轮询语义：action=status + job_id 查询最终状态
    poll = await mcp_server.agentx(str(root), "", action="status", job_id=job_id)
    view = poll["job"]
    assert view["status"] == "completed"
    assert view["phase"] == "completed"
    assert view["result"] is not None  # 最终响应可用（非假失败）
    assert view.get("error") is None
    # Index 确实建成
    assert load_index(root) is not None


@pytest.mark.asyncio
async def test_job_scope_required_then_resume(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """后台任务遇 scope gate → scope_required 挂起；带确认续跑 → completed。

    首次调用的返回可能是 running（同步窗口内未完成）或 scope_required
    （任务极快命中 gate）——两者都是合法的 job 化结果，取决于调度时序。
    测试不依赖窗口竞速：直接 await 后台任务收敛到 scope_required 再续跑。
    """
    from agentx.mcp.jobs import job_manager

    root = _make_project(tmp_path)  # 含 Middlewares → scope 建议
    monkeypatch.setattr("agentx.plan.service.analyze_project", _graph)
    monkeypatch.setattr(mcp_server, "_JOB_SOURCE_THRESHOLD", 0)
    monkeypatch.setattr(mcp_server, "_SYNC_WINDOW_SECONDS", 0.01)

    out = await mcp_server.agentx(str(root), "同步", action="sync")
    assert out["status"] in ("running", "scope_required")
    job_id = out["job_id"]

    # 等待后台任务命中 scope gate（无论首次返回是否已完成都收敛到此状态）
    job = job_manager().get(job_id)
    assert job is not None and job.task is not None
    await asyncio.wait_for(job.task, timeout=30)
    assert job.status == "scope_required"  # 挂起等待确认（不失败）

    # 用户带 scope_selections + job_id 续跑
    selections = {"ignore": ["docs/**"], "third_party": ["Middlewares/**"]}
    resumed = await mcp_server.agentx(
        str(root), "同步", action="sync", job_id=job_id, scope_selections=selections
    )
    assert resumed["status"] in ("running", "completed")
    job2 = job_manager().get(job_id)
    assert job2 is not None and job2.task is not None
    await asyncio.wait_for(job2.task, timeout=30)
    assert job2.status == "completed"
    assert load_index(root) is not None
