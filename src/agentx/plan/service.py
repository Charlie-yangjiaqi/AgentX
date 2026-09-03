"""Plan 服务：建/更新 Project Index + 深读 + 输出实施计划。

Plan 是唯一允许深读项目、建立/更新 Index 的核心入口。
Plan 不修改业务代码。
"""

from __future__ import annotations

import contextlib
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

from agentx.app.application import Application
from agentx.index.index import (
    IndexStatus,
    ProjectIndex,
    create_index,
    index_path,
    index_status,
    load_index,
    refresh_index,
    save_index,
)
from agentx.providers.messages import ChatMessage
from agentx.state.models import AgentXModel
from agentx.understanding.graph import ProjectGraph, analyze_project

PLAN_FILENAME = "plan.json"


class PlanAnalysis(AgentXModel):
    affected_files: list[str] = []
    dependency_chain: list[dict[str, Any]] = []  # [{from, to, impact: direct|indirect}]
    risk: str = ""


class PlanStep(AgentXModel):
    step: int = 0
    file: str = ""
    change: str = ""
    reason: str = ""


class PlanValidation(AgentXModel):
    commands: list[str] = []
    expected_result: str = ""


class PlanChange(AgentXModel):
    """Phase 7.9：结构化修改点（Evidence Validation 的稳定输入）。

    - file: 目标文件（Index 相对路径）
    - symbol: 目标符号（函数/struct/字段/宏名）；纯文件级修改留空
    - operation: modify | add | delete | move
    - reason: 修改依据（引用 Index 证据；验证层只作展示，不参与判定）
    """

    file: str = ""
    symbol: str = ""
    operation: str = "modify"
    reason: str = ""


class ExecutionContext(AgentXModel):
    goal: str = ""
    allowed_files: list[str] = []
    forbidden_files: list[str] = []
    change_strategy: str = ""
    validation_commands: list[str] = []


class PlanOutput(AgentXModel):
    """Plan 输出（Phase 6 结构：analysis / implementation_steps / validation / execution_context）。

    旧字段（summary/steps/files_involved/risks/verification）保留兼容，
    normalize_plan 负责新旧映射。
    """

    summary: str = ""
    analysis: PlanAnalysis = PlanAnalysis()
    implementation_steps: list[PlanStep] = []
    validation: PlanValidation = PlanValidation()
    execution_context: ExecutionContext = ExecutionContext()
    # Phase 7.9：结构化修改点（Evidence Validation 输入；空时由 normalize 从旧字段派生）
    changes: list[PlanChange] = []
    # 兼容旧字段
    steps: list[dict[str, Any]] = []
    files_involved: list[str] = []
    risks: list[str] = []
    verification: str | None = None


def normalize_plan(plan: PlanOutput | None, goal: str = "") -> PlanOutput | None:
    """新旧结构映射：旧格式（steps/files_involved/risks/verification）→ Phase 6 结构。

    - files_involved → analysis.affected_files
    - risks → analysis.risk
    - verification → validation.commands
    - execution_context 由上述字段 + goal 生成
    """
    if plan is None:
        return None
    # Phase 7.9：changes 缺失 → 从 implementation_steps 降级派生（旧格式兼容；
    # symbol 留空 → 验证退化为文件级）
    if not plan.changes and plan.implementation_steps:
        plan.changes = [
            PlanChange(
                file=step.file,
                symbol="",
                operation="modify",
                reason=step.change,
            )
            for step in plan.implementation_steps
        ]
    if plan.analysis.affected_files or plan.validation.commands or plan.execution_context.goal:
        return plan  # 已是新结构
    if plan.files_involved:
        plan.analysis.affected_files = list(plan.files_involved)
    if plan.risks:
        plan.analysis.risk = "; ".join(plan.risks)
    if plan.verification:
        plan.validation.commands = [plan.verification]
        plan.validation.expected_result = "验证命令执行成功（exit=0）"
    if not plan.execution_context.goal:
        plan.execution_context.goal = goal
    if not plan.execution_context.allowed_files:
        plan.execution_context.allowed_files = list(plan.analysis.affected_files)
    if not plan.execution_context.validation_commands:
        plan.execution_context.validation_commands = list(plan.validation.commands)
    return plan


def plan_path(project_root: Path) -> Path:
    return project_root.resolve() / ".agentx" / PLAN_FILENAME


def load_plan(project_root: Path) -> PlanOutput | None:
    p = plan_path(project_root)
    if not p.exists():
        return None
    try:
        plan = PlanOutput.model_validate_json(p.read_text(encoding="utf-8-sig"))
    except Exception:
        return None
    return normalize_plan(plan)


def save_plan(project_root: Path, plan: PlanOutput) -> Path:
    p = plan_path(project_root)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(plan.model_dump_json(indent=2), encoding="utf-8")
    return p


def parse_plan(content: str) -> PlanOutput | None:
    """从 Plan 最终消息解析 JSON（容错：取第一个 JSON 对象）。"""
    start = content.find("{")
    end = content.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        data: dict[str, Any] = json.loads(content[start : end + 1])
    except json.JSONDecodeError:
        return None
    try:
        plan = PlanOutput.model_validate(data)
    except Exception:
        return None
    return normalize_plan(plan)


def ensure_index(
    project_root: Path,
    force_rebuild: bool = False,
    scope_selections: dict[str, Any] | None = None,
) -> tuple[IndexStatus, str, ProjectIndex | None]:
    """Plan 入口第一步：检查并建立/更新 Index 骨架。

    Scope 前置条件（Phase 7.8 引导层）：任何 create_index 前必须通过
    check_scope_init —— 无 scope 配置且有建议时，不建 Index，返回
    (MISSING, "scope_required", None)；调用方需携带 scope_selections
    确认（或由上层向导先生成配置）后重试。

    Build Target 前置条件（Phase 7.10）：Keil 多 Target 且未确认分析目标时
    返回 (MISSING, "build_target_required", None)（不自动猜）；scope_selections
    可携带 build_target 持久化选择。

    返回 (处理前状态, 说明, 当前 Index)。规则：
    - scope/build_target gate 拦截 → Index 为 None（未建立）
    - MISSING / CORRUPTED → 创建
    - STALE → 刷新（保留已有认知）
    - VALID → 直接复用
    - force_rebuild=True → 显式重建（用户意图，优先级最高）
    """
    root = project_root.resolve()
    from agentx.scope.initializer import (
        apply_scope_selections,
        check_build_target_init,
        check_scope_init,
    )

    gate = check_scope_init(root)
    if gate is not None and scope_selections is None:
        return IndexStatus.MISSING, "scope_required", None
    if gate is not None:
        apply_scope_selections(root, scope_selections)

    btg = check_build_target_init(root)
    if btg is not None and scope_selections and scope_selections.get("build_target"):
        # 用户已确认目标 Target → 持久化到 scope config，解除门禁
        apply_scope_selections(root, scope_selections)
        btg = check_build_target_init(root)
    if btg is not None:
        return IndexStatus.MISSING, "build_target_required", None

    status, reason = index_status(root)
    if force_rebuild:
        index = create_index(root)
        save_index(root, index)
        return status, f"显式重建（用户请求）：{reason}", index
    if status == IndexStatus.VALID:
        valid_index = load_index(root)
        assert valid_index is not None
        return status, reason, valid_index
    if status == IndexStatus.STALE:
        stale_index = load_index(root)
        index = refresh_index(root, stale_index)
        save_index(root, index)
        return status, reason, index
    # MISSING / CORRUPTED
    index = create_index(root)
    save_index(root, index)
    return status, reason, index


def is_skeleton_index(index: ProjectIndex | None) -> bool:
    """骨架 Index（ensure_index 壳）：只有 files，没有 CodeGraph/semantic/module 认知。

    判定：从未经过 enrich_index —— codegraph_source 为空 且 capabilities 为空 且
    无符号/调用/模块。enrich_index 即便 CodeGraph 降级 filescan 也会写入
    source + capabilities（semantic disabled + reason），不算骨架。

    调用方（understand 等只走 ensure_index 的流程）必须先 enrich 补全，
    否则用户得到“files 有但 symbols/modules 全空”的伪完整 Index（Phase 7.9 bug）。
    """
    if index is None:
        return False
    return bool(
        index.codegraph_source is None
        and not index.capabilities
        and not index.symbols
        and not index.call_graph
        and not index.modules
    )


def enrich_index(project_root: Path) -> tuple[ProjectIndex, ProjectGraph]:
    """Project Understanding Layer：CodeGraph + Build Info + File Analysis 融合进 Index。

    返回 (更新后的 Index, 项目图)。CodeGraph 不可用时降级文件扫描。
    Build Reality：参与构建的文件 compile_status=compiled；工程明确排除的
    =excluded；有构建配置但未收录的=not_compiled；无构建配置=unknown。
    """
    import hashlib

    root = project_root.resolve()
    graph = analyze_project(root)

    build_info = graph.build_info
    # 构建配置文件的当前 hash：无 git 时用于检测 L4 级变化
    from agentx.index.fingerprint import CONFIG_FILES

    config_hashes: dict[str, str] = {}
    for name in sorted(CONFIG_FILES):
        p = root / name
        if p.is_file():
            with contextlib.suppress(OSError):
                config_hashes[name] = hashlib.sha256(p.read_bytes()).hexdigest()[:16]
    for p in root.iterdir():
        if p.is_file() and p.suffix.lower() in {".uvprojx", ".uvproj", ".ewp", ".ioc"}:
            with contextlib.suppress(OSError):
                config_hashes[p.name] = hashlib.sha256(p.read_bytes()).hexdigest()[:16]
    build_info = {**build_info, "config_hashes": config_hashes}

    compiled: set[str] = {
        str(e.get("file", "")).replace("\\", "/").split("/")[-1]
        for e in build_info.get("compiled_files", [])
    }
    excluded: set[str] = {
        str(e.get("file", "")).replace("\\", "/").split("/")[-1]
        for e in build_info.get("excluded_files", [])
    }
    has_build = bool(build_info.get("has_build_config"))
    build_source = build_info.get("build_source")

    def _content_hash(rel_path: str) -> str | None:
        try:
            data = (root / rel_path).read_bytes()
            return hashlib.sha256(data).hexdigest()[:16]
        except OSError:
            return None

    def _compile_status(base: str) -> str:
        if base in compiled:
            return "compiled"
        if base in excluded:
            return "excluded"
        return "not_compiled" if has_build else "unknown"

    # Phase 7.10 Build Scope：以 Keil Active Target source list 为主 Index 工程边界。
    # 文件分类优先级：ignored > third_party > build-project > non_build。
    # build 解析失败/多 Target 未确认 → build_scope 记录 unresolved（不伪装），
    # 文件分类退回 scope 目录规则（兼容旧行为）。
    from agentx.scope.build_scope import (
        build_scope_summary,
        classify_build_scope,
        resolve_keil_build,
    )

    build_view = resolve_keil_build(root)

    # 候选文件全集 = CodeGraph/扫描文件 ∪ 磁盘源码（后续按 Build Scope 分类）
    from agentx.index.fingerprint import relevant_files as _rf
    from agentx.index.index import index_exclude_name

    graph_paths = {str(f.get("path", "")) for f in graph.files if f.get("path")}
    all_files = set(_rf(root, extra_excludes={index_exclude_name(root)}))
    candidates = sorted(graph_paths | all_files)

    # ignored 已被 classify 丢弃；classified 只含 project/third_party/non_build
    classified = classify_build_scope(root, candidates, build_view, graph.include_map)
    scope_of_path = {p: v for p, v in classified.items()}

    files: list[dict[str, Any]] = []
    for path in sorted(scope_of_path):
        base = path.split("/")[-1]
        in_graph = path in graph_paths
        sc = scope_of_path[path]
        files.append(
            {
                "path": path,
                "status": "active" if in_graph else "orphaned",
                "compiled": base in compiled,
                "compile_status": _compile_status(base),
                "build_source": build_source,
                "content_hash": _content_hash(path),
                "referenced": in_graph,
                "scope_type": sc["scope_type"],
                "scope_name": sc.get("scope_name"),
            }
        )

    build_scope_note = build_scope_summary(build_view, classified, 0)
    build_info = {**build_info, "build_scope": build_scope_note}

    old = load_index(root)
    # Phase 7.6：Tree-sitter 语义补充（signature/struct members/enum values/macro）
    # 能力状态显式化：semantic 不可用 → capabilities 标记 + errors 记录，不静默生成假成功 Index
    # Phase 7.10：semantic 只对 Build Scope 内（project）文件提取——禁止先对全项目
    # 建 semantic 再过滤（否则主 Index 被非编译代码污染）。CodeGraph symbols/call_graph
    # 是原始事实保留，第三方/non_build 不跑文件级 semantic。
    capabilities: dict[str, Any] = {"module": {"enabled": True}}
    semantic_errors: list[str] = []
    scope_of_path = {str(f["path"]): f for f in files}
    project_paths = {
        str(f["path"]) for f in files if str(f.get("scope_type", "project")) == "project"
    }
    if graph.source == "codegraph":
        indirect_calls: list[dict[str, Any]] = []
        try:
            from agentx.semantic.extractor import _PARSER_SOURCE
            from agentx.semantic.merge import merge_semantics

            # 只对主边界（project）文件做文件级语义；CodeGraph 全量符号仍保留
            source_paths = sorted(project_paths)
            symbols, semantic_errors, indirect_calls, semantics = merge_semantics(
                graph.symbols, root, source_paths
            )
            # Phase 7.7.4：类型语义组装（独立理解事实层，不污染 symbols）
            from agentx.semantic.type_extractor import build_type_semantics

            type_semantics = build_type_semantics(
                root,
                semantics,
                symbols,
                indirect_calls,
                old=(old.type_semantics if old is not None else None),
            )
            capabilities["semantic"] = {
                "enabled": True,
                "reason": None,
                "parser": _PARSER_SOURCE,
            }
        except Exception as e:  # semantic 失败不阻断 Index Build，但必须显式标记
            symbols = graph.symbols
            indirect_calls = []
            type_semantics = {}
            from agentx.semantic.runtime import SemanticUnavailableError

            if isinstance(e, SemanticUnavailableError):
                semantic_errors = [f"Semantic 不可用: {e}"]
            else:
                semantic_errors = [f"Semantic 提取失败({type(e).__name__}): {e}"]
            capabilities["semantic"] = {
                "enabled": False,
                "reason": semantic_errors[0],
                "parser": None,
            }
    else:
        symbols = graph.symbols
        indirect_calls = []
        type_semantics = {}
        capabilities["semantic"] = {
            "enabled": False,
            "reason": "CodeGraph 不可用（filescan 模式），语义提取未运行",
            "parser": None,
        }
    # Phase 7.10：build_scope 能力状态（resolved/ambiguity/unknown 显式化）
    capabilities["build_scope"] = {
        "enabled": build_view.resolved,
        "reason": (
            None
            if build_view.resolved
            else (
                "多 Target 未确认（build_target_required）"
                if build_view.ambiguity
                else "无 Keil 工程或 Build 解析失败（build_scope_unknown）"
            )
        ),
        "target": build_view.target,
        "targets": build_view.targets,
    }
    # Phase 7.8：符号标注 scope_type（semantic 数据标记，供 Memory 判断业务/第三方）
    for s in symbols:
        f = str(s.get("file", ""))
        meta = scope_of_path.get(f)
        if meta is not None:
            s["scope_type"] = meta["scope_type"]
            s["scope_name"] = meta.get("scope_name")
        else:
            s["scope_type"] = "project"
    index = create_index(
        root,
        files=files,
        build_info=build_info,
        symbols=symbols,
        call_graph=graph.call_graph,
        include_map=graph.include_map,
        codegraph_source=graph.source,
        errors=graph.errors + semantic_errors,
    )
    index.capabilities = capabilities
    # Phase 7.7.3：函数注册/绑定事实（独立于 CodeGraph call_graph，不承诺调用）
    index.indirect_calls = indirect_calls
    # Phase 7.7.4：类型语义（数据模型级理解事实，独立于 symbols）
    index.type_semantics = type_semantics
    if old is not None and old.plan_summary:
        index.plan_summary = old.plan_summary
    # Phase 7.7：Module Knowledge Layer（确定性模块发现 + 模块级关系，纯 Index 证据）
    try:
        from agentx.module.discover import discover_modules
        from agentx.module.infer import infer_module_relations

        modules = discover_modules(index)
        modules, dependencies = infer_module_relations(modules, index)
        index.modules = modules
        index.dependencies = dependencies
    except Exception as e:  # 模块层失败不阻断 Index Build
        index.errors.append(f"Module discovery 失败({type(e).__name__}): {e}")
    save_index(root, index)
    return index, graph


def _index_preview(index: ProjectIndex, max_files: int = 60) -> str:
    """把 Index 转成给 Plan/Review/Verify 的最小上下文。"""
    files = [f.path for f in index.files[:max_files]]
    orphaned = [f.path for f in index.files if f.status == "orphaned"]
    lines = [
        f"文件数: {index.file_count}（预览 {len(files)}）",
        "文件:",
        *[f"  {f}" for f in files],
    ]
    if orphaned:
        lines.append(f"未参与构建 (orphaned): {', '.join(orphaned[:10])}")
    if index.symbols:
        lines.append("符号:")
        lines.extend(
            f"  {s.get('name')} ({s.get('kind', '')}) @ {s.get('file', '')}"
            for s in index.symbols[:50]
        )
    if index.build_info:
        bi = index.build_info
        # Build Scope（Phase 7.10）：project/third_party/non_build 边界摘要
        bs = bi.get("build_scope") or {}
        if bs.get("resolved"):
            lines.append(
                f"Build Scope: target={bs.get('target')} build_files={bs.get('build_files')} "
                f"project={bs.get('project')} non_build={bs.get('non_build')} "
                f"third_party={bs.get('third_party')}"
            )
        elif bs:
            state = "multi-target" if bs.get("ambiguity") else "build parse failed"
            lines.append(f"Build Scope: UNRESOLVED（{state}）")
        # 只给摘要（target/统计/defines），不注入 compiled_files/excluded_files 全量
        summary = {
            "system": bi.get("system"),
            "build_source": bi.get("build_source"),
            "target": bi.get("target"),
            "cpu": bi.get("cpu"),
            "compiled_count": len(bi.get("compiled_files") or []),
            "excluded_count": len(bi.get("excluded_files") or []),
            "defines": bi.get("defines"),
            "has_build_config": bi.get("has_build_config"),
        }
        lines.append(f"构建: {json.dumps(summary, ensure_ascii=False)}")
    if index.call_graph:
        lines.append(f"调用关系: {json.dumps(index.call_graph[:20], ensure_ascii=False)}")
    if index.modules:
        mods = [
            f"{m['name']}({m.get('type', '?')})={m.get('build_status', '?')}"
            f"/{len(m.get('files', []))}f"
            for m in index.modules[:10]
        ]
        lines.append(f"模块: {', '.join(mods)}")
    if index.include_map:
        incs = [f"{k} → {','.join(v[:5])}" for k, v in list(index.include_map.items())[:10]]
        lines.append(f"包含关系: {incs}")
    if index.tests:
        lines.append(f"测试: {json.dumps(index.tests, ensure_ascii=False)}")
    return "\n".join(lines)


class PlanService:
    """Plan 服务：Index 管理 + Plan 生成。"""

    def __init__(self, application: Application) -> None:
        self.app = application

    async def plan(
        self,
        goal: str,
        progress: Callable[[str], None] | None = None,
        origin: str = "unknown",
        force_rebuild: bool = False,
        on_event: Callable[[str, str, str], None] | None = None,
        scope_selections: dict[str, Any] | None = None,
        decision_choice: str | None = None,
        accept_blocked: bool = False,
    ) -> dict[str, Any]:
        """执行 Plan：检查 Index → Understanding → Decision Guard → 生成 Plan。

        on_event：结构化 workflow 事件回调 (stage, status, message)；None 时行为不变。
        force_rebuild：显式重建 Index（用户意图，优先于 VALID 复用）。
        scope_selections：首次 Scope 初始化确认（无配置且有建议时；不传则返回
        scope_required，不建立 Index——Scope 是 Index 构建的前置条件）。
        accept_blocked：人工确认兜底——跳过 Evidence Validation 的 BLOCK 拦截
        （输出方案并标记 validation.level=block，不静默）。

        Phase 7.8 Decision Boundary：Analyze → Candidate → Gate → User Confirm → Plan。
        - 无 decision_choice 且 Gate 触发 → 返回 decision_required（候选+原因，不调 LLM）
        - 带 decision_choice（candidate_id）→ 校验版本 → 仅围绕所选目标生成方案

        Phase 7.9 Evidence Validation：Plan → 验证 → PASS 输出 / WARNING 输出+标记 /
        BLOCK 回 LLM 修正一次（带失败原因）→ 仍 BLOCK → plan_blocked（人工确认）。
        """
        from agentx.core.orchestrator import _env_hint
        from agentx.core.progress import ProgressReporter

        def emit(stage: str, status: str, message: str = "") -> None:
            if on_event is not None:
                on_event(stage, status, message)

        root = self.app.project_root
        # Capability 检查（第一版可见性）：模型不支持 reasoning_effort 时提前 warning，
        # 不等 API 400。Provider Layer 拥有能力知识，此处仅透出。
        warnings: list[str] = []
        from agentx.providers.openai import check_reasoning_effort_compat

        plan_agent = self.app.orchestrator.agents.get("plan")
        plan_model: str = ""
        if plan_agent is not None:
            definition = plan_agent.definition
            if definition is not None:
                plan_model = definition.model or ""
        reasoning_effort = (self.app.config.generation or {}).get("reasoning_effort")
        compat_warning = check_reasoning_effort_compat(plan_model, reasoning_effort)
        if compat_warning:
            warnings.append(compat_warning)
            if progress is not None:
                progress(f"[WARN] {compat_warning}")
            emit("index_check", "pending", compat_warning)

        emit("index_check", "running", "checking fingerprint")
        if progress is not None:
            progress("[1/5] 检查 Project Index / 指纹")
        before_status, before_reason, _ = ensure_index(
            root, force_rebuild=force_rebuild, scope_selections=scope_selections
        )
        if _ is None:
            from agentx.scope.initializer import (
                check_build_target_init,
                check_scope_init,
            )

            if before_reason == "build_target_required":
                # Phase 7.10：Keil 多 Target 未确认 → build_target_required（不自动猜）
                gate = check_build_target_init(root) or {}
                emit("index_check", "completed", "build_target_required")
                return {
                    "index_scope": "build_target_required",
                    "status": "build_target_required",
                    "reason": gate.get("reason", "keil_multi_target_unselected"),
                    "message": gate.get("message", "Need user confirm current Keil target"),
                    "build_targets": gate.get("build_targets", []),
                    "build_files": gate.get("build_files", 0),
                    "project_file": gate.get("project_file"),
                    "index_before": {"status": str(before_status), "reason": before_reason},
                    "index_after": {"status": "build_target_required", "reason": "Index 未建立"},
                }
            # Scope 前置条件未满足：不建立 Index（gate 由 ensure_index 内部判定）
            gate = check_scope_init(root) or {}
            emit("index_check", "completed", "scope_required")
            return {
                "index_scope": "scope_required",
                "status": "scope_required",
                "reason": gate.get("reason", "first_project_index_without_scope"),
                "message": gate.get("message", "Need user confirmation before index build"),
                "suggestions": gate.get("suggestions", {}),
                "index_before": {"status": str(before_status), "reason": before_reason},
                "index_after": {"status": "scope_required", "reason": "Index 未建立"},
            }
        emit("index_check", "completed", f"{before_status} {before_reason}")
        from agentx.runtime.context import decide_index_action

        decision = decide_index_action(before_status.value, force_rebuild=force_rebuild)
        emit("index_decision", "completed", f"{decision['action']}: {decision['reason']}")
        # 外部变化感知：STALE 且由本调用触发同步 → 生成变更报告（agentx_execution 静默）
        if before_status == IndexStatus.STALE:
            from agentx.index.sync import sync_index

            emit("index_sync", "running", "syncing project knowledge")
            sync_result = sync_index(
                root, origin=origin, progress=progress, scope_selections=scope_selections
            )
            if sync_result.get("action") == "scope_required":
                from agentx.scope.initializer import check_scope_init

                gate = check_scope_init(root) or {}
                emit("index_sync", "completed", "scope_required")
                return {
                    "index_scope": "scope_required",
                    "status": "scope_required",
                    "reason": gate.get("reason", "first_project_index_without_scope"),
                    "message": gate.get("message", "Need user confirmation before index build"),
                    "suggestions": gate.get("suggestions", {}),
                    "index_before": {"status": str(before_status), "reason": before_reason},
                    "index_after": {"status": "scope_required", "reason": "Index 未重建"},
                }
            emit(
                "index_sync",
                "completed",
                f"{sync_result['level']}: {sync_result['message']}",
            )
            if progress is not None:
                progress(f"  外部变化已同步: {sync_result['message']}")

        if progress is not None:
            progress("[2/5] 项目分析（CodeGraph / 构建信息 / 文件分析）")
        emit("codegraph_analysis", "running", "analyzing symbols / build info")
        # Project Understanding Layer：CodeGraph + Build + File Analysis 融合
        index, graph = enrich_index(root)
        # Index Pipeline Reliability：semantic runtime 诊断 + Quality Gate + Scope Report
        from agentx.quality import compute_quality, compute_scope_report
        from agentx.scope.ignore import ignored_dirs
        from agentx.semantic.runtime import format_runtime_status, semantic_runtime_status

        rt = semantic_runtime_status()
        if progress is not None:
            progress(f"[semantic runtime] {format_runtime_status(rt)}")
        quality = compute_quality(index)
        scope_report = compute_scope_report(index)
        ignored = ignored_dirs(root)
        if progress is not None:
            progress(
                f"Index Quality: {quality['grade']} (semantic={quality['semantic']}, "
                f"funcs={quality['functions_with_signature']}/{quality['functions']}, "
                f"macros={quality['macros']}, modules={quality['modules']})"
            )
            progress(
                f"Scope: project files={scope_report['project_files']} "
                f"third_party files={scope_report['third_party_files']} "
                f"ignored={len(ignored)}"
            )
        emit(
            "index_quality",
            "completed",
            f"Quality: {quality['grade']} semantic={quality['semantic']} "
            f"funcs={quality['functions_with_signature']}/{quality['functions']} "
            f"structs={quality['structs_with_members']}/{quality['structs']} "
            f"enums={quality['enums_with_values']}/{quality['enums']} "
            f"macros={quality['macros']} modules={quality['modules']} "
            f"scope_p={scope_report['project_files']}/tp={scope_report['third_party_files']}",
        )
        emit(
            "codegraph_analysis",
            "completed",
            f"{graph.source}，{index.file_count} 文件，{len(index.symbols)} 符号",
        )
        if progress is not None:
            progress(f"[3/5] Index 已更新（来源: {graph.source}，文件: {index.file_count}）")

        # 工程理解：首次建立自动探索一次；过期且命中相关区域才按需刷新
        from agentx.understanding.understand import ensure_understanding

        emit("understanding", "running", "ensuring core path understanding")
        understanding_status = await ensure_understanding(
            self.app, root, goal=goal, progress=progress
        )
        index = load_index(root) or index
        emit("understanding", "completed", understanding_status["message"])
        if progress is not None and understanding_status["status"] != "reused":
            progress(f"[3.5/5] 工程理解: {understanding_status['message']}")

        # Phase 7.7.2：Module Responsibilities（理解层按需增强——事实层不依赖 LLM）
        from agentx.module.responsibility import generate_module_responsibilities

        emit("understanding", "running", "ensuring module responsibilities")
        resp_status = await generate_module_responsibilities(self.app, root, progress=progress)
        if progress is not None and resp_status["status"] != "reused":
            progress(f"[3.6/5] {resp_status['message']}")

        reporter = ProgressReporter(self.app.event_bus, progress) if progress else None
        if reporter is not None:
            reporter.start()
        try:
            runtime = self.app.orchestrator.agents.get("plan")
            if runtime is None:
                raise RuntimeError("Plan agent 未配置")
            ctx = self.app.orchestrator._ctx(self.app._dummy_task())
            from agentx.understanding.impact import build_impact_data, format_impact_data
            from agentx.understanding.query import format_query_result, query_index
            from agentx.understanding.understand import format_understanding

            query_result = query_index(index, goal)
            emit(
                "query_context",
                "completed",
                f"命中 {len(query_result['files'])} 文件 / {len(query_result['symbols'])} 符号",
            )

            # Phase 7.8 Decision Boundary：Candidate → Gate → User Confirm
            from agentx.decision.analyzer import analyze_candidates
            from agentx.decision.gate import evaluate_gate
            from agentx.module.responsibility import load_responsibilities

            decision_anchor: str | None = None
            resp_entries = load_responsibilities(root)
            candidates = analyze_candidates(index, query_result, goal, resp_entries)
            verdict = evaluate_gate(candidates, index)
            if verdict.confirm:
                emit("decision_gate", "completed", "decision_required")
                if progress is not None:
                    progress(f"[Decision Guard] {len(candidates)} 个候选，需用户确认")
                if decision_choice is None:
                    return {
                        "status": "decision_required",
                        "message": (
                            f"发现 {len(candidates)} 个可能修改位置，请选择修改目标"
                            "（decision_choice=candidate_id）"
                        ),
                        "decision_reasons": verdict.reasons,
                        "decision_fingerprint": index.project_fingerprint,
                        "candidates": candidates,
                        "options": [f"candidate_select:{c['id']}" for c in candidates]
                        + ["view_impact", "cancel"],
                        "index_before": {"status": str(before_status), "reason": before_reason},
                        "index_after": {"status": "VALID", "reason": "Index 可用（等待决策）"},
                    }
                # 用户已选择：校验候选 id + 版本（防选择错位）
                selected = next((c for c in candidates if c["id"] == decision_choice), None)
                if selected is None:
                    return {
                        "status": "decision_required",
                        "error": (
                            f"无效的 decision_choice: {decision_choice}（候选已更新，请重新选择）"
                        ),
                        "decision_fingerprint": index.project_fingerprint,
                        "candidates": candidates,
                        "options": [f"candidate_select:{c['id']}" for c in candidates]
                        + ["view_impact", "cancel"],
                    }
                if selected.get("index_fingerprint") != index.project_fingerprint:
                    return {
                        "status": "decision_required",
                        "error": "Index 已变化，候选已失效，请重新生成并选择",
                        "decision_fingerprint": index.project_fingerprint,
                        "candidates": candidates,
                    }
                decision_anchor = f"{selected['target']}（candidate_id={selected['id']}）"
                emit("decision_gate", "completed", f"用户选择: {selected['target']}")
            elif decision_choice is not None:
                # 有 choice 但 gate 放行：仍校验候选有效性
                selected = next((c for c in candidates if c["id"] == decision_choice), None)
                if selected is not None:
                    decision_anchor = f"{selected['target']}（candidate_id={selected['id']}）"
            elif candidates:
                decision_anchor = f"{candidates[0]['target']}（唯一候选，gate 放行）"

            if query_result["files"] or query_result["symbols"]:
                context_note = (
                    "以下是与任务相关的项目认知子图（Index Query 结果，不是全量）：\n"
                    f"{format_query_result(query_result)}"
                )
            else:
                context_note = f"任务未命中 Index 认知，给出全量项目概览：\n{_index_preview(index)}"
            understanding_note = ""
            if index.project_understanding:
                understanding_note = (
                    "\n工程理解（Core Path Understanding）：\n"
                    f"{format_understanding(index.project_understanding)}"
                )
            impact_note = ""
            impact_data = build_impact_data(index, query_result)
            if impact_data["symbols"] or impact_data["files"]:
                impact_note = f"\n{format_impact_data(impact_data)}"
            module_note = ""
            if index.modules:
                from agentx.query.module_query import (
                    format_module_view,
                    module_of_file,
                    module_of_symbol,
                )

                module_note = f"\n{format_module_view(index, query_result)}"
                # Phase 7.7.2：职责视图（high 直接 / medium [推断] / low+null 不进入）
                from agentx.module.responsibility import (
                    format_responsibilities_for_planning,
                    load_responsibilities,
                )

                hit_names: list[str] = []
                for f in query_result.get("files", []):
                    m = module_of_file(index, str(f.get("path", "")))
                    if m and m["name"] not in hit_names:
                        hit_names.append(m["name"])
                for s in query_result.get("symbols", []):
                    m = module_of_symbol(index, str(s.get("name", "")))
                    if m and m["name"] not in hit_names:
                        hit_names.append(m["name"])
                resp_note = format_responsibilities_for_planning(
                    index.modules, load_responsibilities(root), hit_names
                )
                if resp_note:
                    module_note += "\n" + resp_note
            messages = [
                ChatMessage(role="user", content=f"任务目标：{goal}"),
                ChatMessage(role="user", content=_env_hint()),
                ChatMessage(
                    role="user",
                    content=(
                        f"Project Index 状态: {before_status}（{before_reason}）\n"
                        f"项目认知来源: {graph.source}，文件总数: {index.file_count}\n"
                        f"{context_note}{understanding_note}{impact_note}{module_note}\n"
                        "需要更多细节时用工具读取实际文件。"
                    ),
                ),
            ]
            # Phase 7.8：用户已确定的修改目标锚点（禁止 LLM 重新选择）
            if decision_anchor is not None:
                messages.insert(
                    1,
                    ChatMessage(
                        role="user",
                        content=(
                            "【修改目标已由用户确定（Decision Guard）】\n"
                            f"目标: {decision_anchor}\n"
                            "硬性约束：实施方案必须围绕该目标展开；"
                            "禁止建议其他修改位置，禁止重新评估修改目标。"
                        ),
                    ),
                )
            if progress is not None:
                progress("[4/5] Planner 深度分析项目")
            emit("planning", "running", "planner analyzing project")
            result = await runtime.run(messages, ctx)
        finally:
            if reporter is not None:
                reporter.close()

        emit("planning", "completed", "implementation plan generated")
        if progress is not None:
            progress("[5/5] 生成实施方案")
        plan = parse_plan(result.content or "")
        if plan is None:
            plan = PlanOutput(summary=result.content or "")

        # Phase 7.9：Evidence Validation（零 LLM，不能让 AI 验证自己）
        from agentx.module.responsibility import load_responsibilities
        from agentx.validation.validator import format_validation, validate_plan

        resp_entries = load_responsibilities(root)
        validation = validate_plan(
            index, [c.model_dump() for c in plan.changes], resp_entries
        )
        emit("evidence_validation", "completed", validation.summary)
        if progress is not None:
            progress(f"[5.5/5] {validation.summary}")
        if validation.level == "block" and not accept_blocked:
            # 一次修正：带验证失败原因回 LLM（不是自由重写，只修正目标与依据）
            fix_feedback = (
                "以下修改点缺少事实证据，请基于已有 index 证据重新调整：\n"
                f"{format_validation(validation)}\n"
                "要求：\n"
                "1. 删除或修正不存在于工程的文件/符号（Rule 1）\n"
                "2. 函数/字段修改必须引用调用关系、注册关系或定义位置（Rule 2/3）\n"
                "3. 新增接口需说明 consumer（Rule 4）\n"
                "4. 跨模块修改需给出可达的传播链（Rule 5）\n"
                "只输出修正后的最终 JSON（格式与之前一致）。"
            )
            emit("evidence_validation", "running", "one fix attempt with failure reasons")
            result2 = await runtime.run(
                messages
                + [
                    ChatMessage(role="assistant", content=result.content or ""),
                    ChatMessage(role="user", content=fix_feedback),
                ],
                ctx,
            )
            plan2 = parse_plan(result2.content or "")
            if plan2 is None:
                plan2 = PlanOutput(summary=result2.content or "")
            validation2 = validate_plan(
                index, [c.model_dump() for c in plan2.changes], resp_entries
            )
            if validation2.level != "block":
                plan, result, validation = plan2, result2, validation2
            else:
                # 仍 BLOCK → 人工确认（不输出方案，附验证详情）
                save_plan(root, plan2)
                after_status, after_reason = index_status(root)
                emit("evidence_validation", "blocked", validation2.summary)
                emit("completed", "completed", "plan blocked by evidence validation")
                return {
                    "status": "plan_blocked",
                    "message": (
                        "修改方案无法通过证据验证，需人工确认。"
                        "可使用 decision_action=accept_blocked 强制接受"
                        "（方案已保存，validation 含全部证据详情）"
                    ),
                    "index_before": {"status": str(before_status), "reason": before_reason},
                    "index_after": {"status": str(after_status), "reason": after_reason},
                    "index_fingerprint": index.project_fingerprint,
                    "plan": plan2.model_dump(),
                    "agent_summary": result2.content or "",
                    "validation": validation2.as_dict(),
                    "warnings": warnings,
                }
            emit("evidence_validation", "completed", f"fixed: {validation.summary}")

        save_plan(root, plan)
        index.plan_summary = plan.summary
        save_index(root, index)
        # 处理完再查一次，反映真实当前状态（before 是进入时状态，可能 MISSIING→已创建）
        after_status, after_reason = index_status(root)
        emit("completed", "completed", "plan done")
        return {
            "index_before": {"status": str(before_status), "reason": before_reason},
            "index_after": {"status": str(after_status), "reason": after_reason},
            "index_created": before_status != after_status,
            "index_status": str(after_status),
            "index_reason": after_reason,
            "index_fingerprint": index.project_fingerprint,
            "index_dir": str(index_path(root).parent),
            "codegraph_source": graph.source,
            "plan": plan.model_dump(),
            "agent_summary": result.content or "",
            "validation": validation.as_dict(),
            "warnings": warnings,
        }
