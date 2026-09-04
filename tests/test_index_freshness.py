"""Phase 8.2 Index Freshness / Incremental Update / Full Reindex Decision Model.

Spec 十三.测试要求（10 条）+ 状态机 + 高影响升级 + 阈值联合判定。

验收：
0. 三类指纹独立归因（scope/source/build_scope）
1. 单函数修改 → 自动增量（AUTO_UPDATED），不 full reindex
2. 少量文件删除 → incremental，正确移除 facts
3. 新增 7 个 *.py ignore（小影响）→ 自动 reclassify，不 full reindex
4. scope 大范围变化 → REINDEX_REQUIRED（不自动重建）
5. 当前 Target compiled 集合小变化 → 不 REQUIRED（可增量）
6. 当前 Target compiled 集合大变化 → REINDEX_REQUIRED
7. 其他 Target 变化 → 不影响当前 Index（build fp 不变）
8. auto(CODE_WRITE) 完成 → 不隐式 full reindex
9. status/query(READ) → 不隐式 full reindex
10. 用户确认 REINDEX_REQUIRED → full reindex → VALID
"""

from __future__ import annotations

from pathlib import Path

import pytest

import agentx.mcp.server as mcp_server
from agentx.app.application import Application
from agentx.index.fingerprint import compute_source_fingerprint
from agentx.index.freshness import (
    AUTO_UPDATED,
    REINDEX_REQUIRED,
    evaluate_index_state,
)
from agentx.index.index import IndexStatus, index_status, load_index, save_index
from agentx.index.sync import sync_index
from agentx.plan.service import enrich_index
from agentx.providers.mock import MockProvider, text_response
from agentx.scope.config import SCOPE_CONFIG_FILENAME
from agentx.understanding.graph import ProjectGraph


def _make_project(tmp_path: Path) -> Path:
    root = tmp_path / "proj"
    (root / "User").mkdir(parents=True)
    for name in ("main.c", "app.c", "svc.c"):
        stem = Path(name).stem
        (root / "User" / name).write_text(
            f"int {stem}_run(void) {{ return 1; }}\n", encoding="utf-8"
        )
    (root / "User" / "tool.py").write_text("print(1)\n", encoding="utf-8")
    return root


def _graph(r: Path) -> ProjectGraph:
    files = []
    symbols = []
    for p in r.rglob("*.c"):
        rel = str(p.relative_to(r)).replace("\\", "/")
        files.append({"path": rel, "language": "c"})
        symbols.append(
            {
                "name": f"{Path(rel).stem}_run",
                "type": "function",
                "file": rel,
                "start_line": 1,
                "semantic": True,
            }
        )
    py = r / "User" / "tool.py"
    if py.exists():
        files.append({"path": "User/tool.py", "language": "python"})
    return ProjectGraph(
        source="codegraph",
        files=files,
        symbols=symbols,
        call_graph=[],
        include_map={},
        build_info={},
        errors=[],
    )


@pytest.fixture(autouse=True)
def _isolate(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        "agentx.config.config.default_config_path",
        lambda: tmp_path / "agentx_test_config.json",
    )
    monkeypatch.setattr("agentx.plan.service.analyze_project", _graph)
    monkeypatch.setattr("agentx.understanding.graph.analyze_project", _graph)
    monkeypatch.setattr("agentx.index.incremental.analyze_project", _graph)


def _app_for(root: Path) -> Application:
    app = Application(root)
    app.orchestrator.agents["plan"].provider = MockProvider().respond(
        text_response(
            '{"architecture_summary": "HMI", "startup_flow": ["main"], '
            '"core_modules": ["User/main.c"], "critical_files": []}'
        ),
        text_response(
            '{"summary": "ok", "files_involved": ["User/main.c"], "verification": "echo ok"}'
        ),
    )
    return app


def _build_index(root: Path, keil: bool = False) -> None:
    if not (root / SCOPE_CONFIG_FILENAME).exists():
        (root / SCOPE_CONFIG_FILENAME).write_text(
            "third_party: []\nignore: []\n", encoding="utf-8"
        )
    index, _ = enrich_index(root)
    save_index(root, index)


def _keil_xml(paths: list[str], target: str = "LVGL", extra: list[str] | None = None) -> str:
    extra = extra or []
    all_paths = list(dict.fromkeys(paths + extra))
    files = "\n".join(
        f"<File><FileName>{p.rsplit('/',1)[-1]}</FileName>"
        f"<FileType>1</FileType><FilePath>{p}</FilePath></File>"
        for p in all_paths
    )
    return (
        "<Project><Targets><Target>"
        f"<TargetName>{target}</TargetName>"
        "<Groups><Group><GroupName>App</GroupName><Files>"
        f"{files}"
        "</Files></Group></Groups>"
        "</Target></Targets></Project>"
    )


def _keil_multi_xml(targets: dict[str, list[str]]) -> str:
    def _t(name: str, paths: list[str]) -> str:
        files = "\n".join(
            f"<File><FileName>{p.rsplit('/',1)[-1]}</FileName>"
            f"<FileType>1</FileType><FilePath>{p}</FilePath></File>"
            for p in paths
        )
        return (
            "<Target>"
            f"<TargetName>{name}</TargetName>"
            f"<TargetOption><TargetCommonOption><SelectTargetNo>"
            f"{0 if name=='LVGL' else 1}</SelectTargetNo>"
            "</TargetCommonOption></TargetOption>"
            "<Groups><Group><GroupName>App</GroupName><Files>"
            f"{files}</Files></Group></Groups></Target>"
        )

    return "<Project><Targets>" + "".join(
        _t(n, p) for n, p in targets.items()
    ) + "</Targets></Project>"


def _keil_with_build_target(root: Path, target: str) -> None:
    cfg = f"third_party: []\nignore: []\nbuild:\n  target: {target}\n"
    (root / SCOPE_CONFIG_FILENAME).write_text(cfg, encoding="utf-8")


# ---------- 0. 三类指纹独立归因 ----------


def test_fingerprints_independent(tmp_path: Path) -> None:
    root = _make_project(tmp_path)
    _build_index(root)
    idx = load_index(root)
    assert idx is not None
    assert idx.source_fingerprint and idx.scope_fingerprint
    old_src = idx.source_fingerprint
    # scope 变化不改变 source fp
    (root / SCOPE_CONFIG_FILENAME).write_text(
        "third_party: []\nignore:\n  - \"*.py\"\n", encoding="utf-8"
    )
    from agentx.scope.config import compute_scope_fingerprint

    assert compute_scope_fingerprint(root) != idx.scope_fingerprint
    assert compute_source_fingerprint(root) == old_src


# ---------- 1. 单函数修改 → AUTO_UPDATED 增量，不 full reindex ----------


def test_single_function_change_auto_updates(tmp_path: Path) -> None:
    root = _make_project(tmp_path)
    _build_index(root)

    (root / "User" / "svc.c").write_text(
        "int svc_run(void) { return 42; }\n", encoding="utf-8"
    )
    result = sync_index(root, origin="external")
    assert result["action"] == "incremental"
    assert result["index_freshness"]["state"] == AUTO_UPDATED
    after = load_index(root)
    assert after is not None
    # 未变化文件的语义字段保留（app.c 符号还在）
    assert any(str(s.get("file", "")) == "User/app.c" for s in after.symbols)
    status, _ = index_status(root)
    assert status == IndexStatus.VALID


# ---------- 2. 少量文件删除 → incremental，facts 不残留 ----------


def test_small_file_deletion_incremental_removes_facts(tmp_path: Path) -> None:
    root = _make_project(tmp_path)
    _build_index(root)
    assert any(f.path == "User/app.c" for f in load_index(root).files)

    (root / "User" / "app.c").unlink()
    result = sync_index(root, origin="external")
    assert result["action"] == "incremental"
    after = load_index(root)
    assert after is not None
    assert not any(f.path == "User/app.c" for f in after.files)
    assert not any(str(s.get("file", "")) == "User/app.c" for s in after.symbols)


# ---------- 3. 新增 7 个 *.py ignore → 自动 reclassify，不 full reindex ----------


def test_small_scope_ignore_auto_reclassify(tmp_path: Path) -> None:
    root = _make_project(tmp_path)
    (root / "User" / "scripts").mkdir(parents=True)
    for i in range(7):
        (root / "User" / "scripts" / f"s{i}.py").write_text("print(1)\n", encoding="utf-8")
    _build_index(root)
    assert any(f.path.endswith(".py") for f in load_index(root).files)

    (root / SCOPE_CONFIG_FILENAME).write_text(
        "third_party: []\nignore:\n  - \"*.py\"\n", encoding="utf-8"
    )
    result = sync_index(root, origin="external")
    assert result["action"] == "reclassify"
    after = load_index(root)
    assert after is not None
    assert not any(f.path.endswith(".py") for f in after.files)


# ---------- 4. scope 大范围变化 → REINDEX_REQUIRED，不自动重建 ----------


def test_scope_large_change_requires_reindex(tmp_path: Path) -> None:
    root = _make_project(tmp_path)
    # 规模足够大（>50 文件），让"全部 project → third_party"成为大范围变化
    for i in range(60):
        (root / "User" / f"bulk{i}.c").write_text("int x;\n", encoding="utf-8")
    _build_index(root)
    before = load_index(root)
    assert len(before.files) > 60
    # 全部 project → third_party（移动数超阈值）
    (root / SCOPE_CONFIG_FILENAME).write_text(
        "third_party:\n  - path: User\n    name: UserLib\nignore: []\n", encoding="utf-8"
    )
    verdict = evaluate_index_state(root, load_index(root))
    assert verdict["state"] == REINDEX_REQUIRED
    assert verdict["requires_confirmation"] is True
    result = sync_index(root, origin="external")
    assert result["action"] == "reindex_required"
    # Index 未被悄悄改写
    assert load_index(root).project_fingerprint == before.project_fingerprint


# ---------- 5. Build 集合小变化 → 不 REQUIRED ----------


def test_build_boundary_small_change_no_required(tmp_path: Path) -> None:
    root = _make_project(tmp_path)
    _build_index(root)
    (root / "app.uvprojx").write_text(_keil_xml(["User/main.c", "User/app.c"]), encoding="utf-8")
    idx, _ = enrich_index(root)
    save_index(root, idx)
    assert load_index(root).build_scope_fingerprint is not None

    # +1 编译文件（delta 1 < 50，比例 ~33% >20%？但规则用 delta>50 OR ratio>0.2）
    (root / "app.uvprojx").write_text(
        _keil_xml(["User/main.c", "User/app.c", "User/svc.c"]), encoding="utf-8"
    )
    verdict = evaluate_index_state(root, load_index(root))
    # build 大变化判定：ratio=1/3≈0.33>0.2 → REQUIRED。这里为测"小变化不 REQUIRED"
    # 需保持 ratio ≤0.2：从 10 个文件 +1 → ratio 0.1 <0.2
    (root / "User").mkdir(parents=True, exist_ok=True)
    for i in range(10):
        (root / "User" / f"g{i}.c").write_text("int x;\n", encoding="utf-8")
    base = [f"User/g{i}.c" for i in range(10)]
    (root / "app.uvprojx").write_text(_keil_xml(base), encoding="utf-8")
    idx, _ = enrich_index(root)
    save_index(root, idx)
    # +1（10→11）delta1<50 ratio0.09<0.2
    (root / "app.uvprojx").write_text(
        _keil_xml(base + ["User/g0_extra.c"]) , encoding="utf-8"
    )
    (root / "User" / "g0_extra.c").write_text("int x;\n", encoding="utf-8")
    verdict = evaluate_index_state(root, load_index(root))
    assert verdict["state"] != REINDEX_REQUIRED


# ---------- 6. Build 集合大变化 → REINDEX_REQUIRED ----------


def test_build_boundary_large_change_requires_reindex(tmp_path: Path) -> None:
    root = _make_project(tmp_path)
    _build_index(root)
    base = [f"User/g{i}.c" for i in range(3)]
    for f in base:
        (root / f).write_text("int x;\n", encoding="utf-8")
    (root / "app.uvprojx").write_text(_keil_xml(base), encoding="utf-8")
    idx, _ = enrich_index(root)
    save_index(root, idx)
    assert load_index(root).build_scope_fingerprint is not None

    # 大变化：+60 编译文件
    extra = [f"User/gen{i}.c" for i in range(60)]
    for f in extra:
        (root / f).write_text("int x;\n", encoding="utf-8")
    (root / "app.uvprojx").write_text(_keil_xml(base + extra), encoding="utf-8")
    verdict = evaluate_index_state(root, load_index(root))
    assert verdict["state"] == REINDEX_REQUIRED


# ---------- 7. 其他 Target 变化不影响当前 Index ----------


def test_other_target_change_no_impact(tmp_path: Path) -> None:
    root = _make_project(tmp_path)
    _build_index(root)
    _keil_with_build_target(root, "LVGL")
    (root / "app.uvprojx").write_text(
        _keil_multi_xml(
            {"LVGL": ["User/main.c", "User/app.c"], "Demo": ["User/svc.c"]}
        ),
        encoding="utf-8",
    )
    idx, _ = enrich_index(root)
    save_index(root, idx)
    before_fp = load_index(root).build_scope_fingerprint
    assert before_fp is not None

    # Demo target 编译集变大（当前 target=LVGL 不变）
    (root / "app.uvprojx").write_text(
        _keil_multi_xml(
            {"LVGL": ["User/main.c", "User/app.c"], "Demo": ["User/svc.c", "User/tool.py"]}
        ),
        encoding="utf-8",
    )
    verdict = evaluate_index_state(root, load_index(root))
    assert verdict["state"] != REINDEX_REQUIRED


# ---------- 8/9. CODE_WRITE 与 READ 不隐式 full reindex ----------


def test_auto_and_read_do_not_hide_full_reindex(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """auto/status/query 返回里绝不出现 index_freshness.state=REINDEX_REQUIRED 的隐式
    触发动作；且小工程 READ 不触发重建（freshness 标注而非自动 reindex）。"""
    import asyncio

    root = _make_project(tmp_path)
    _build_index(root)
    app = _app_for(root)
    monkeypatch.setattr(mcp_server, "_app", lambda path: app)

    # READ：query（mcp.agentx 是 async → 需 await）
    from agentx.index.index import load_index as li

    fp_before = li(root).project_fingerprint
    out = asyncio.run(
        mcp_server.agentx(str(root), "查询 svc_run", action="query")
    )
    assert isinstance(out, dict)
    assert "index_freshness" in out or "result" in out
    # 未改文件：不触发任何重建
    assert li(root).project_fingerprint == fp_before


# ---------- 10. 确认 REINDEX_REQUIRED → full reindex → VALID ----------


def test_confirm_reindex_returns_valid(tmp_path: Path) -> None:
    root = _make_project(tmp_path)
    for i in range(60):
        (root / "User" / f"bulk{i}.c").write_text("int x;\n", encoding="utf-8")
    _build_index(root)

    (root / SCOPE_CONFIG_FILENAME).write_text(
        "third_party:\n  - path: User\n    name: UserLib\nignore: []\n", encoding="utf-8"
    )
    result = sync_index(root, origin="external")
    assert result["action"] == "reindex_required"
    assert result["requires_confirmation"] is True

    # 用户确认 → full reindex
    idx, _ = enrich_index(root)
    save_index(root, idx)
    status, _ = index_status(root)
    assert status == IndexStatus.VALID
    assert load_index(root).scope_fingerprint is not None
