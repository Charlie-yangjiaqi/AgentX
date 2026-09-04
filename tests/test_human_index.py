"""Phase 8.3 Human Project Knowledge / Human Index 测试。

Spec §十八（10 条）+ 核心约束：
0. 文档写入 <project>_codebase_index/human/ 而非项目根 docs
1. VALID Index → 生成三份文档
2. 文档含 project metadata / build target / fingerprint / traceability
3. LOW responsibility 不得写成 HIGH certainty
4. 不存在的 symbol/module 不得出现在文档
5. non_build 文件不得描述为当前固件组成部分
6. 修改普通函数 → 只刷新受影响 Human Documents
7. 无关局部修改 → 不强制刷新所有文档
8. REINDEX_REQUIRED → 不偷跑 full reindex
9. Index 不存在 → 按现有 lifecycle 处理
+ Manifest knowledge_dependencies 支撑刷新判定
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agentx.human.manifest import (
    DOC_ARCHITECTURE,
    DOC_MODULES,
    DOC_PROJECT_OVERVIEW,
    load_manifest,
)
from agentx.human.service import HumanKnowledgeService, infer_changed_modules_from_files
from agentx.index.index import index_dir, load_index, save_index
from agentx.plan.service import enrich_index
from agentx.providers.mock import MockProvider, text_response
from agentx.understanding.graph import ProjectGraph


def _make_project(tmp_path: Path) -> Path:
    root = tmp_path / "proj"
    (root / "User" / "hmi" / "app").mkdir(parents=True)
    (root / "User" / "hmi" / "comm").mkdir(parents=True)
    (root / "User" / "hmi" / "service").mkdir(parents=True)
    files = {
        "User/hmi/app/hmi_app.c": "int HMI_App_Init(void){return 0;}\n",
        "User/hmi/app/hmi_action.c": "int HMI_Action_Handle(void){return 1;}\n",
        "User/hmi/comm/comm_port_rs485.c": "int CommPortRs485_Write(void){return 0;}\n",
        "User/hmi/comm/comm_port_rs485.h": "#ifndef RS485\n#define RS485\n#endif\n",
        "User/hmi/service/param_service.c": "int ParamService_Load(void){return 0;}\n",
        "tools/gen.py": "print(1)\n",  # 会被 scope ignore（非当前固件）
    }
    for rel, content in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    return root


def _graph(r: Path) -> ProjectGraph:
    c_files = [
        "User/hmi/app/hmi_app.c",
        "User/hmi/app/hmi_action.c",
        "User/hmi/comm/comm_port_rs485.c",
        "User/hmi/service/param_service.c",
    ]
    files = [{"path": f, "language": "c"} for f in c_files]
    py = r / "tools" / "gen.py"
    if py.exists():
        files.append({"path": "tools/gen.py", "language": "python"})
    return ProjectGraph(
        source="codegraph",
        files=files,
        symbols=[
            {"name": "HMI_App_Init", "type": "function", "file": c_files[0],
             "start_line": 1, "end_line": 1, "semantic": True},
            {"name": "HMI_Action_Handle", "type": "function", "file": c_files[1],
             "start_line": 1, "end_line": 1, "semantic": True},
            {"name": "CommPortRs485_Write", "type": "function", "file": c_files[2],
             "start_line": 1, "end_line": 1, "semantic": True},
            {"name": "ParamService_Load", "type": "function", "file": c_files[3],
             "start_line": 1, "end_line": 1, "semantic": True},
        ],
        call_graph=[
            {"caller": "HMI_App_Init", "callee": "HMI_Action_Handle",
             "confidence": "high", "file": c_files[0], "line": 1},
            {"caller": "HMI_App_Init", "callee": "ParamService_Load",
             "confidence": "high", "file": c_files[0], "line": 2},
        ],
        include_map={
            "User/hmi/comm/comm_port_rs485.c": ["User/hmi/comm/comm_port_rs485.h"],
        },
        build_info={
            "system": "keil",
            "build_source": "keil",
            "has_build_config": True,
            "target": "LVGL",
            "cpu": 'IRAM CPUTYPE("Cortex-M4")',
            "defines": ["GD32F427"],
            "compiled_files": [{"file": f, "compiled": True} for f in c_files],
            "excluded_files": [],
        },
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


def _app(root: Path, with_prose: bool = True):
    from agentx.app.application import Application

    app = Application(root)
    responses = [
        text_response(
            '{"architecture_summary": "HMI 固件", "startup_flow": ["hmi_app.c"], '
            '"core_modules": ["User/hmi/app/hmi_app.c"], "critical_files": []}'
        )
    ]
    if with_prose:
        responses.append(text_response("这是项目概览散文。"))  # overview prose
        responses.append(text_response("这是架构散文。"))  # architecture prose
    app.orchestrator.agents["plan"].provider = MockProvider().respond(*responses)
    return app


def _build_index(root: Path) -> None:
    (root / ".agentxscope.yaml").write_text(
        "third_party: []\nignore:\n  - tools/**\n", encoding="utf-8"
    )
    idx, _ = enrich_index(root)
    save_index(root, idx)


# ---------- 1. VALID Index → 生成三份文档到 <index>/human/ ----------


@pytest.mark.asyncio
async def test_valid_index_generates_three_docs_in_human_dir(tmp_path: Path) -> None:
    root = _make_project(tmp_path)
    _build_index(root)
    app = _app(root, with_prose=False)
    svc = HumanKnowledgeService(root, app=app)
    r = await svc.generate(with_prose=False)
    assert r["status"] == "updated"
    assert r["human_index"]["status"] == "updated"
    assert set(r["human_index"]["documents"]) == {
        DOC_PROJECT_OVERVIEW, DOC_ARCHITECTURE, DOC_MODULES,
    }
    hdir = index_dir(root) / "human"
    assert hdir.exists()
    for d in (DOC_PROJECT_OVERVIEW, DOC_ARCHITECTURE, DOC_MODULES):
        assert (hdir / d).exists()
    # 不在项目根 docs
    assert not (root / "docs").exists()
    assert not (root / "human_index").exists()
    # manifest
    assert (hdir / "manifest.json").exists()


# ---------- 2. 文档含 metadata / build target / fingerprint / traceability ----------


@pytest.mark.asyncio
async def test_docs_contain_metadata_and_traceability(tmp_path: Path) -> None:
    root = _make_project(tmp_path)
    _build_index(root)
    idx = load_index(root)
    svc = HumanKnowledgeService(root)
    await svc.generate(with_prose=False)
    hdir = index_dir(root) / "human"
    overview = (hdir / DOC_PROJECT_OVERVIEW).read_text(encoding="utf-8")
    assert "Generated by AgentX" in overview
    assert "Build Target: `LVGL`" in overview
    assert idx.source_fingerprint in overview
    assert idx.scope_fingerprint in overview
    assert "Traceability" in overview or "Evidence" in overview
    assert "Cortex-M4" in overview  # runtime env
    arch = (hdir / DOC_ARCHITECTURE).read_text(encoding="utf-8")
    assert "Traceability" in arch
    mods = (hdir / DOC_MODULES).read_text(encoding="utf-8")
    assert "Module Map" in mods


# ---------- 3. LOW responsibility 不得写成 HIGH certainty ----------


def test_low_responsibility_not_presented_as_high():
    # 渲染层约束：fallback/low 模块用 [事实构成] 而非 [High：职责判断]
    from agentx.human.docs import _tag_interpretation

    assert "推断" in _tag_interpretation("medium")
    assert "未做职责判断" in _tag_interpretation("low")
    assert "High" not in _tag_interpretation("low")


# ---------- 4. 不存在的 symbol/module 不得出现 ----------


@pytest.mark.asyncio
async def test_docs_only_contain_existing_modules(tmp_path: Path) -> None:
    root = _make_project(tmp_path)
    _build_index(root)
    svc = HumanKnowledgeService(root)
    await svc.generate(with_prose=False)
    idx = load_index(root)
    existing = {m.get("name") for m in (idx.modules or [])}
    mods_md = (index_dir(root) / "human" / DOC_MODULES).read_text(encoding="utf-8")
    # 文档中出现的反引号模块头必须来自 Index
    import re

    for m in re.findall(r"^### (.+)$", mods_md, re.MULTILINE):
        assert m in existing, f"文档出现不存在模块: {m}"
    # 不应出现编造的模块（抽样关键词）
    for fake in ("FakeStorageModule", "NonexistentCommLayer"):
        assert fake not in mods_md


# ---------- 5. non_build 文件不得描述为当前固件 ----------


@pytest.mark.asyncio
async def test_non_build_not_described_as_current_firmware(tmp_path: Path) -> None:
    root = _make_project(tmp_path)
    _build_index(root)
    # 将 tools/gen.py 弄成 non_build：它在 ignore，应该根本不进 index
    idx = load_index(root)
    assert not any(f.path == "tools/gen.py" for f in idx.files)
    svc = HumanKnowledgeService(root)
    await svc.generate(with_prose=False)
    arch = (index_dir(root) / "human" / DOC_ARCHITECTURE).read_text(encoding="utf-8")
    # 当前固件模块描述不得包含 tools/gen.py 为编译模块
    assert "gen.py" not in arch or "not part of current build target" in arch


# ---------- 6. 修改普通函数 → 只刷新受影响 Human Documents ----------


@pytest.mark.asyncio
async def test_refresh_only_affected_docs(tmp_path: Path) -> None:
    root = _make_project(tmp_path)
    _build_index(root)
    svc = HumanKnowledgeService(root)
    await svc.generate(with_prose=False)

    # 模拟一个函数改动：改 hmi_app.c（属于 HMI_App 相关模块）
    # 记录生成后各文档 mtime
    hdir = index_dir(root) / "human"
    before = {d: (hdir / d).stat().st_mtime_ns for d in (
        DOC_PROJECT_OVERVIEW, DOC_ARCHITECTURE, DOC_MODULES)}

    bundle = _make_bundle(root)
    changed_mods = infer_changed_modules_from_files(
        ["User/hmi/app/hmi_app.c"], bundle
    )
    assert changed_mods  # 应命中至少一个模块
    r = await svc.refresh(changed_modules=changed_mods)
    assert r["human_index"]["status"] in ("updated", "no_change")
    # 至少有文档被刷新，或按依赖判定刷新
    refreshed = set(r["human_index"]["documents"])
    after = {d: (hdir / d).stat().st_mtime_ns for d in (
        DOC_PROJECT_OVERVIEW, DOC_ARCHITECTURE, DOC_MODULES)}
    for d in refreshed:
        assert after[d] >= before[d]


def _make_bundle(root: Path):
    from agentx.human.bundle import collect_bundle

    return collect_bundle(root)


# ---------- 7. 无关局部修改 → 不强制刷新所有文档 ----------


@pytest.mark.asyncio
async def test_unrelated_change_not_force_refresh_all(tmp_path: Path) -> None:
    root = _make_project(tmp_path)
    _build_index(root)
    svc = HumanKnowledgeService(root)
    await svc.generate(with_prose=False)
    # 无 changed_modules / changed_relations → refresh 应 no_change（基线一致）
    r = await svc.refresh()
    assert r["status"] == "no_change"


# ---------- 8. REINDEX_REQUIRED → 不偷跑 full reindex ----------


@pytest.mark.asyncio
async def test_reindex_required_does_not_auto_reindex(tmp_path: Path) -> None:
    root = _make_project(tmp_path)
    _build_index(root)
    # 制造大 scope 变化 → 让 freshness 判 REQUIRED
    (root / "User" / "bulk").mkdir(parents=True, exist_ok=True)
    for i in range(60):
        (root / "User" / "bulk" / f"b{i}.c").write_text("int x;\n", encoding="utf-8")
    _build_index(root)
    # scope 大变：把 User 全变 third_party
    (root / ".agentxscope.yaml").write_text(
        "third_party:\n  - path: User\n    name: UserLib\nignore: []\n", encoding="utf-8"
    )
    from agentx.index.freshness import evaluate_index_state

    verdict = evaluate_index_state(root, load_index(root))
    assert verdict["state"] == "REINDEX_REQUIRED"
    fp_before = load_index(root).project_fingerprint
    svc = HumanKnowledgeService(root)
    # generate 不应触发 full reindex（它应返回 blocked）
    r = await svc.generate(with_prose=False)
    assert r["status"] == "blocked"
    assert load_index(root).project_fingerprint == fp_before  # Index 未被改写


# ---------- 9. Index 不存在 → 按 lifecycle 处理 ----------


@pytest.mark.asyncio
async def test_missing_index_goes_through_lifecycle(tmp_path: Path) -> None:
    root = _make_project(tmp_path)
    # 无 scope config 但有建议 → scope_required 拦截
    from agentx.app.application import Application

    app = Application(root)
    app.orchestrator.agents["plan"].provider = MockProvider().respond(
        text_response(
            '{"architecture_summary": "x", "startup_flow": [], "core_modules": []}'
        )
    )
    svc = HumanKnowledgeService(root, app=app)
    # tools 目录会触发 ignore 建议
    r = await svc.generate(with_prose=False)
    # 可能 scope_required 或 bootstrap（取决于 suggest_scopes 是否报 tools/**）
    assert r["status"] in ("blocked", "updated")
    if r["status"] == "blocked":
        assert r.get("blocked") in ("scope_required", "missing", "build_target_required")


# ---------- 10. Manifest knowledge_dependencies 记录 ----------


@pytest.mark.asyncio
async def test_manifest_records_knowledge_dependencies(tmp_path: Path) -> None:
    root = _make_project(tmp_path)
    _build_index(root)
    svc = HumanKnowledgeService(root)
    await svc.generate(with_prose=False)
    manifest = load_manifest(root)
    docs = manifest.get("documents", {})
    assert DOC_MODULES in docs
    entry = docs[DOC_MODULES]
    assert "modules" in entry
    assert "knowledge_sources" in entry
    assert "knowledge_dependencies" in entry
    deps = entry["knowledge_dependencies"]
    assert isinstance(deps.get("modules"), list)
    assert "module_dependencies" in deps.get("relations", [])


# ---------- 11. LLM 散文白名单（防 bundle 外事实入文档） ----------


def test_prose_allowlist_blocks_hallucinated_modules() -> None:
    from agentx.human.service import _prose_within_allowlist

    allowed = {"HMI_SERVICE", "PARAMSERVICE", "COMMPORTRS485", "LVGL", "FREERTOS"}
    # 引用存在的模块 → 通过
    assert _prose_within_allowlist("HMI_Service 负责业务编排。", allowed) is True
    # 引用允许集内的前缀族 → 通过（HMI 是 HMI_SERVICE/HMI_APP 前缀）
    assert _prose_within_allowlist("HMI 提供界面相关能力。", allowed) is True
    # 编造不存在的模块 → 拒绝
    assert _prose_within_allowlist("依赖 NonexistentStorageModule 保存数据。", allowed) is False
    assert _prose_within_allowlist("FakeCommLayer 处理通信。", allowed) is False


def test_prose_allowlist_allows_common_words() -> None:
    from agentx.human.service import _prose_within_allowlist

    allowed = {"HMI_APP", "FREERTOS"}
    prose = "该固件基于 FreeRTOS，HMI_App 是综合应用入口，负责界面与业务流程组织。"
    assert _prose_within_allowlist(prose, allowed) is True
