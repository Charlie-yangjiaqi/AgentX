"""Phase 7.9 Index 构建完整性：骨架 Index 必须 enrich 补全，不得伪完整。

问题：agentx understand / MCP understand 在 Index MISSING 时只走 ensure_index
建壳（files，无 CodeGraph/semantic/module 认知），保存后 fingerprint 一致 → 被当
VALID 复用 → 用户得到 files 有、但 symbols/modules/call_graph/codegraph_source
全空的伪完整 Index。

修复：is_skeleton_index 识别骨架；understand 流程 bootstrap 后 enrich 补全；
CodeGraph 知识库不可读 → 明确降级 filescan（source+errors 记录），不返回
“codegraph 但空知识”的误导结果。
"""

from __future__ import annotations

from pathlib import Path

import pytest

import agentx.mcp.server as mcp_server
from agentx.app.application import Application
from agentx.config.config import load_config
from agentx.index.index import IndexStatus, create_index, index_status, load_index, save_index
from agentx.plan.service import enrich_index, ensure_index, is_skeleton_index
from agentx.providers.mock import MockProvider, text_response
from agentx.understanding.graph import ProjectGraph


def _make_project(tmp_path: Path) -> Path:
    root = tmp_path / "工程"
    (root / "User").mkdir(parents=True)
    for i in range(4):
        (root / "User" / f"app{i}.c").write_text(
            f"int app{i}_fn(void) {{ return {i}; }}\n", encoding="utf-8"
        )
    return root


def _codegraph_graph(root: Path) -> ProjectGraph:
    files = [{"path": f"User/app{i}.c", "language": "c"} for i in range(4)]
    symbols = [
        {"name": f"app{i}_fn", "type": "function", "file": f"User/app{i}.c", "start_line": 1}
        for i in range(4)
    ]
    return ProjectGraph(
        source="codegraph",
        files=files,
        symbols=symbols,
        call_graph=[{"caller": "app1_fn", "callee": "app0_fn"}],
        include_map={},
        build_info={},
        errors=[],
    )


def _filescan_graph(root: Path) -> ProjectGraph:
    """模拟 CodeGraph 不可用 → 已明确降级的 filescan 图（source=filescan + error）。"""
    files = [{"path": f"User/app{i}.c", "language": "c"} for i in range(4)]
    g = ProjectGraph(source="filescan", files=files, include_map={}, errors=[])
    g.errors.append("CodeGraph 不可用，已降级为文件扫描")
    return g


@pytest.fixture(autouse=True)
def _no_global_config(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        "agentx.config.config.default_config_path",
        lambda: tmp_path / "agentx_test_config.json",
    )


# ---------- 骨架识别 ----------


def test_is_skeleton_index_true_for_bare_skeleton(tmp_path: Path) -> None:
    root = _make_project(tmp_path)
    skeleton = create_index(root)  # ensure_index 的建壳路径
    save_index(root, skeleton)
    loaded = load_index(root)
    assert loaded is not None
    assert is_skeleton_index(loaded)
    # 骨架 fingerprint 有效 → 会被 index_status 判 VALID（这就是伪完整误判的来源）
    status, _ = index_status(root)
    assert status == IndexStatus.VALID


def test_is_skeleton_index_false_after_enrich(tmp_path: Path) -> None:
    """enrich（即便 CodeGraph 降级 filescan）后不再算骨架。"""
    root = _make_project(tmp_path)
    create_index(root)
    save_index(root, create_index(root))
    enriched, graph = enrich_index(root)
    assert not is_skeleton_index(enriched)
    assert graph.source in ("codegraph", "filescan")


# ---------- understand bootstrap 必须 enrich ----------


def _app_for(root: Path) -> Application:
    app = Application(root, config=load_config())
    app.orchestrator.agents["plan"].provider = MockProvider().respond(
        text_response(
            '{"architecture_summary": "HMI 固件", "startup_flow": ["main"], '
            '"core_modules": ["User/app0.c"], "critical_files": []}'
        )
    )
    return app


@pytest.mark.asyncio
async def test_understand_enriches_skeleton_index(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """MISSING 项目先 ensure_index 建壳 → ensure_understanding 必须 enrich 补全，
    不能把骨架当 VALID 完整 Index 用。"""
    from agentx.understanding.understand import ensure_understanding

    root = _make_project(tmp_path)
    monkeypatch.setattr("agentx.plan.service.analyze_project", _codegraph_graph)
    monkeypatch.setattr("agentx.understanding.graph.analyze_project", _codegraph_graph)
    app = _app_for(root)
    monkeypatch.setattr(mcp_server, "_app", lambda path: app)

    # 场景复现：ensure_index 建壳（骨架保存，fingerprint 有效）
    ensure_index(root, scope_selections={"ignore": ["docs/**"], "third_party": []})
    before = load_index(root)
    assert before is not None
    assert is_skeleton_index(before)  # 复现：只有 files

    # understand 流程 → 骨架被 enrich 补全
    out = await ensure_understanding(app, root, goal="分析", force=True)
    assert out.get("status") in ("created", "refreshed", "reused")
    after = load_index(root)
    assert after is not None
    assert not is_skeleton_index(after)
    assert after.codegraph_source == "codegraph"
    assert after.capabilities.get("semantic", {}).get("enabled") is True
    assert len(after.symbols) > 0
    assert len(after.call_graph) > 0
    assert len(after.modules) > 0


@pytest.mark.asyncio
async def test_understand_codegraph_down_explicit_degraded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CodeGraph 不可用：understand 仍建 Index，但明确 degraded
    （source=filescan + capabilities.semantic disabled + errors 记录），
    不得产出“codegraph_source 空、符号全空”的误导骨架。"""
    from agentx.understanding.understand import ensure_understanding

    root = _make_project(tmp_path)
    monkeypatch.setattr("agentx.plan.service.analyze_project", _filescan_graph)
    monkeypatch.setattr("agentx.understanding.graph.analyze_project", _filescan_graph)
    app = _app_for(root)
    monkeypatch.setattr(mcp_server, "_app", lambda path: app)

    ensure_index(root, scope_selections={"ignore": ["docs/**"], "third_party": []})
    before = load_index(root)
    assert before is not None and is_skeleton_index(before)

    out = await ensure_understanding(app, root, goal="分析", force=True)
    assert out.get("status") in ("created", "refreshed", "reused", "degraded")
    after = load_index(root)
    assert after is not None
    assert not is_skeleton_index(after)
    # filescan 降级是显式的：source 有值 + semantic 明确 disabled + errors 有原因
    assert after.codegraph_source == "filescan"
    assert after.capabilities.get("semantic", {}).get("enabled") is False
    assert any("CodeGraph" in e for e in after.errors)


def test_ensure_index_missing_then_enrich_via_sync(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """sync MISSING 路径：_ensure_index 建壳 + enrich 补全 → 非骨架。"""
    from agentx.index.sync import sync_index

    root = _make_project(tmp_path)
    monkeypatch.setattr("agentx.plan.service.analyze_project", _codegraph_graph)
    # scope gate：直接确认（第三方无）
    result = sync_index(root, scope_selections={"ignore": [], "third_party": []})
    assert result.get("action") == "created"
    index = load_index(root)
    assert index is not None
    assert not is_skeleton_index(index)
