"""AgentX MCP Server：Reasonix 的工程能力层（统一入口）。

对外只暴露一个工具 `agentx`，action 分发到内部服务：
- auto   ：完整闭环（Plan → 等 Reasonix 执行 → Review → Verify）
- plan   ：Index/Fingerprint/理解层 → 实施方案
- review ：Index + Plan + Diff → verdict + findings
- verify ：机器验证 → evidence + verdict
- status ：Index 状态 / 指纹 / 项目认知概览

Phase 6.5：返回统一包装 {result, runtime, events}——result 保持旧版兼容；
runtime 解释 Index 决策，events 展示 workflow 阶段。
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable, Coroutine
from pathlib import Path
from typing import Any, cast

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.context import Context

from agentx.app.application import Application
from agentx.config.config import load_config
from agentx.index.index import index_path
from agentx.mcp.jobs import JobFn, job_manager
from agentx.mcp.sanitize import sanitize_value
from agentx.plan.service import PlanService, load_plan
from agentx.review.service import ReviewService
from agentx.runtime.events import EventCollector, Heartbeat, ListenerFn
from agentx.verify.service import VerifyService

server = MCPServer(
    name="agentx",
    version="1.3.0",
    title="AgentX Engineering Layer",
    description=(
        "AgentX：Reasonix 的工程能力层（plan / review / verify）。"
        "AgentX 自动维护 Project Index（fingerprint 校验）："
        "已有 VALID Index 直接复用，不重新扫描；STALE 自动同步；"
        "仅 Index 缺失/损坏或显式 force_rebuild=true 时才重建。"
        "请勿无理由要求重建 Index。"
        "统一入口：action=auto|plan|review|verify|understand|sync|status。"
        "首次使用无 .agentxscope.yaml 的项目时，先带 scope_selections 确认范围"
        "（或按 scope_required 返回的 suggestions 确认），再建立 Index。"
        "返回 {result, runtime, events}：runtime 含 Index 状态与决策原因，"
        "events 为 workflow 阶段事件。"
    ),
)


def _app(project_path: str) -> Application:
    return Application(Path(project_path), config=load_config())


def _session_from_context(context: Any) -> Any:
    """从 SDK 注入的 Context 取 ServerSession；不可用返回 None（降级兼容）。"""
    if context is None:
        return None
    try:
        session = context.request_context.session
        return session if hasattr(session, "send_notification") else None
    except Exception:
        return None


def _progress_token_from_context(context: Any) -> str | int | None:
    """读取请求 _meta.progressToken（Reasonix 自动携带）；无则返回 None（降级）。"""
    if context is None:
        return None
    try:
        meta = context.request_context.meta
        if meta is None:
            return None
        token = meta.get("progress_token")
        return token if token is not None else None
    except Exception:
        return None


def _send_stream_event(
    session: Any,
    data: dict[str, Any],
    pending: list[asyncio.Task[None]] | None = None,
) -> None:
    """发送 notifications/message（logger=agentx）。

    fire-and-forget：真实网络 IO 会在下一个 await 点立即推送；
    pending 列表（可选）用于结束时 gather 兜底，保证最终送达。
    stream 失败静默，绝不影响任务。
    """
    from mcp.types import (
        LoggingMessageNotification,
        LoggingMessageNotificationParams,
    )

    async def _send() -> None:
        with contextlib.suppress(Exception):
            await session.send_notification(
                LoggingMessageNotification(
                    params=LoggingMessageNotificationParams(
                        level="info",
                        # 输出边界清洗：notification data 同样不允许非法编码
                        data=sanitize_value(data),
                        logger="agentx",
                    )
                )
            )

    try:
        task = asyncio.create_task(_send())
        if pending is not None:
            pending.append(task)
    except Exception:
        pass


def _make_stream_listener(session: Any, pending: list[asyncio.Task[None]]) -> ListenerFn:
    """workflow 事件 → 实时 notification（不进最终 events[] 的只走这里）。"""

    def _listener(event: dict[str, str]) -> None:
        _send_stream_event(session, {"type": "workflow_event", "event": event}, pending)

    return _listener


def _make_heartbeat_sender(
    session: Any, pending: list[asyncio.Task[None]], adapter: Any = None
) -> Any:
    """心跳 → 双通道：notifications/message + notifications/progress（若 adapter）。

    heartbeat 不进主事件列表；progress 不改变 stage 编号，只更新 message。
    """

    def _sender(beat: dict[str, object]) -> None:
        _send_stream_event(session, {"type": "workflow_heartbeat", "event": beat}, pending)
        if adapter is not None:
            adapter.on_beat(beat)

    return _sender


def _wrap(
    result: dict[str, Any],
    app: Application,
    workflow: str,
    events: EventCollector,
    force_rebuild: bool = False,
    before_state: str = "",
) -> dict[str, Any]:
    """统一返回包装：result（旧版兼容）+ runtime（Index 决策解释）+ events。

    decision 基于进入时状态（解释"为什么做这个决策"）；
    index_state 反映当前（完成后的）状态。
    """
    from agentx.index.index import index_status
    from agentx.runtime.context import build_runtime_context, decide_index_action

    state, _ = index_status(app.project_root)
    fingerprint = str(result.get("index_fingerprint") or result.get("fingerprint") or "")
    runtime = build_runtime_context(
        index_state=state.value,
        fingerprint=fingerprint,
        force_rebuild=force_rebuild,
        workflow_action=workflow,
        workflow_stage="completed",
    ).to_dict()
    if before_state:
        runtime["decision"] = decide_index_action(before_state, force_rebuild=force_rebuild)
    out: dict[str, Any] = {
        "result": result,
        "runtime": runtime,
        "events": events.events(),
    }
    # Phase 8.1：权限元数据注入（顶层 operation_class/changes_code/requires_decision_gate）
    out = _decorate(workflow, out)
    # 业务错误（如缺 Plan）同时暴露在顶层，兼容旧客户端
    if isinstance(result, dict) and result.get("error"):
        out["error"] = result["error"]
    return out


async def _action_plan(
    app: Application,
    task: str,
    origin: str = "unknown",
    force_rebuild: bool = False,
    events: EventCollector | None = None,
    scope_selections: dict[str, Any] | None = None,
    decision_choice: str | None = None,
    accept_blocked: bool = False,
) -> dict[str, Any]:
    return await PlanService(app).plan(
        task,
        origin=origin,
        force_rebuild=force_rebuild,
        on_event=events.emit if events is not None else None,
        scope_selections=scope_selections,
        decision_choice=decision_choice,
        accept_blocked=accept_blocked,
    )


async def _action_review(
    app: Application,
    task: str,
    origin: str = "unknown",
    force_rebuild: bool = False,
    events: EventCollector | None = None,
    scope_selections: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return await ReviewService(app).review(
        task, origin=origin, on_event=events.emit if events is not None else None
    )


async def _action_verify(
    app: Application,
    task: str,
    origin: str = "unknown",
    force_rebuild: bool = False,
    events: EventCollector | None = None,
    scope_selections: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return await VerifyService(app).verify(
        task, origin=origin, on_event=events.emit if events is not None else None
    )


async def _action_understand(
    app: Application,
    task: str,
    origin: str = "unknown",
    force_rebuild: bool = False,
    events: EventCollector | None = None,
    scope_selections: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """主动刷新工程理解（用户触发；task 为可选提示词）。"""
    from agentx.understanding.understand import ensure_understanding

    understanding = await ensure_understanding(
        app,
        app.project_root,
        goal=task or None,
        force=True,
        scope_selections=scope_selections,
    )
    # Phase 7.9.2：bootstrap 统一——scope 未确认时挂起（job 层识别并等待）
    if understanding.get("status") == "scope_required":
        return {
            "status": "scope_required",
            "reason": "first_project_index_without_scope",
            "message": understanding.get("message", "Need user confirmation before index build"),
            "suggestions": understanding.get("suggestions", {}),
        }
    # Phase 7.7.2：模块职责理解资产全量刷新（用户主动 understand）
    from agentx.module.responsibility import generate_module_responsibilities

    responsibilities = await generate_module_responsibilities(app, app.project_root, force=True)
    return {
        "understanding": understanding,
        "module_responsibilities": responsibilities,
    }


async def _action_sync(
    app: Application,
    task: str,
    origin: str = "unknown",
    force_rebuild: bool = False,
    events: EventCollector | None = None,
    scope_selections: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Index Sync：代码变化后分级维护 Index（git diff 优先）。"""
    from agentx.index.sync import sync_index

    return sync_index(app.project_root, origin=origin, scope_selections=scope_selections)


async def _action_scope_update(
    app: Application,
    task: str,
    origin: str = "unknown",
    force_rebuild: bool = False,
    events: EventCollector | None = None,
    scope_selections: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Phase 8.1 INDEX_WRITE：修改 .agentxscope.yaml（ignore/third_party/build_target）。

    全量替换语义：scope_selections 提供完整期望值（ignore/third_party/build_target）。
    只写 AgentX 自身配置文件，绝不触碰用户源码 → 不触发 CODE_WRITE 审批。
    """
    from agentx.scope.config import SCOPE_CONFIG_FILENAME, compute_scope_fingerprint
    from agentx.scope.initializer import apply_scope_selections, preview_scope_change

    root = app.project_root
    selections = scope_selections or {}
    preview = preview_scope_change(root, selections)
    target = apply_scope_selections(root, selections)
    return {
        "status": "updated",
        "scope_changed": True,
        "scope_config": str(target),
        "scope_fingerprint": compute_scope_fingerprint(root),
        "config_file": SCOPE_CONFIG_FILENAME,
        "preview": preview,
        "next": "调用 action=reindex 使新 scope 生效（Index 尚未重建）",
    }


async def _action_reindex(
    app: Application,
    task: str,
    origin: str = "unknown",
    force_rebuild: bool = False,
    events: EventCollector | None = None,
    scope_selections: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Phase 8.1 INDEX_WRITE：强制重建 Index（invalidate → CodeGraph → enrich → module）。

    使用磁盘上最新的 scope 配置（scope_fingerprint 比较，杜绝旧 scope 缓存）。
    只重建 AgentX 自身认知产物，不修改用户源码 → 不触发 CODE_WRITE 审批。
    """
    from agentx.index.sync import sync_index

    # sync_index 内部：scope_fingerprint 变化 → scope_rebuild；否则全量 enrich。
    # 传 scope_selections（scope_update 刚写入过时可不传，读取最新磁盘配置）。
    result = sync_index(
        root := app.project_root,
        origin=origin or "agentx_index_write",
        scope_selections=scope_selections,
    )
    from agentx.index.index import index_status, load_index

    status, reason = index_status(root)
    idx = load_index(root)
    from agentx.scope.config import compute_scope_fingerprint

    out: dict[str, Any] = {
        **result,
        "status": "completed",
        "index_status": status.value,
        "index_reason": reason,
        "fingerprint": idx.project_fingerprint if idx else None,
        "scope_fingerprint": idx.scope_fingerprint if idx else compute_scope_fingerprint(root),
    }
    # scope_summary：project/third_party/non_build/ignored
    if idx is not None:
        from collections import Counter

        counts = Counter(f.scope_type for f in idx.files)
        out["scope_summary"] = {
            "project": counts.get("project", 0),
            "third_party": counts.get("third_party", 0),
            "non_build": counts.get("non_build", 0),
            "ignored": counts.get("ignored", 0),
        }
        bs = (idx.build_info or {}).get("build_scope") or {}
        if bs:
            out["build_scope"] = bs
    return out


async def _action_status(
    app: Application,
    task: str,
    origin: str = "unknown",
    force_rebuild: bool = False,
    events: EventCollector | None = None,
    scope_selections: dict[str, Any] | None = None,
) -> dict[str, Any]:
    # 任务前置检查：STALE → Index Sync（外部变化生成报告）
    from agentx.index.sync import ensure_synced

    status, reason, sync_result = ensure_synced(app.project_root, origin=origin)
    plan = load_plan(app.project_root)
    out: dict[str, Any] = {
        "project": str(app.project_root),
        "index_dir": str(index_path(app.project_root).parent),
        "index_status": status,
        "index_reason": reason,
        "plan": plan.model_dump() if plan else None,
    }
    # Phase 7.10：Build Scope 边界摘要（target/project/non_build/third_party）
    from agentx.index.index import load_index

    idx = load_index(app.project_root)
    if idx is not None:
        bs = (idx.build_info or {}).get("build_scope") or {}
        if bs:
            out["build_scope"] = bs
    if sync_result is not None:
        out["sync"] = sync_result
    return out


async def _action_auto(
    app: Application,
    task: str,
    origin: str = "unknown",
    force_rebuild: bool = False,
    events: EventCollector | None = None,
    scope_selections: dict[str, Any] | None = None,
    decision_choice: str | None = None,
    accept_blocked: bool = False,
) -> dict[str, Any]:
    """完整闭环：Plan → 返回执行方案，等 Reasonix 执行后 Review → Verify。

    由于 MCP 调用是独立的，auto 分两步语义：
    1. 先执行 Plan 并返回方案；
    2. 同一调用内继续 Review + Verify（针对当前项目状态）。

    Phase 7.8：Plan 被 Decision Gate 拦截（decision_required）时，auto 同步
    返回候选（不继续 Review/Verify——未确认前不进入修改流程）。
    Phase 7.9：Plan 被 Evidence Validation 拦截（plan_blocked）时，auto 同步
    返回验证详情（不继续 Review/Verify——方案无据不进入流程）。
    """
    plan_result = await _action_plan(
        app,
        task,
        origin,
        force_rebuild,
        events,
        scope_selections,
        decision_choice,
        accept_blocked,
    )
    if plan_result.get("status") == "scope_required":
        return {"phase": "scope_required", "plan": plan_result}
    if plan_result.get("status") in ("decision_required", "plan_blocked"):
        return {"phase": plan_result["status"], "plan": plan_result}
    review_result = await _action_review(app, task, origin, force_rebuild, events)
    verify_result = await _action_verify(app, task, origin, force_rebuild, events)
    return {
        "phase": "complete",
        "plan": plan_result,
        "review": review_result,
        "verify": verify_result,
    }


async def _action_query(
    app: Application,
    task: str,
    origin: str = "unknown",
    force_rebuild: bool = False,
    events: EventCollector | None = None,
    *,
    query_type: str = "feature",
) -> dict[str, Any]:
    """Project Knowledge Query：feature / symbol / architecture（纯 Index 证据，不扫描工程）。"""
    from agentx.index.index import index_status, load_index
    from agentx.query.evidence import build_evidence_card, format_flow

    status, reason = index_status(app.project_root)
    index = load_index(app.project_root)
    if index is None:
        return {
            "error": "Project Index 不存在，请先调用 agentx.plan 建立项目认知。",
            "index_status": status,
        }

    if query_type == "symbol":
        from agentx.query.symbol import search_symbol

        result = search_symbol(index, task or "")
    elif query_type == "architecture":
        from agentx.query.architecture import search_architecture

        result = search_architecture(index, task or "")
    elif query_type == "build":
        # Build Query：这个文件是否进入固件（事实，不推断）
        from agentx.build import build_query_from_info

        result = build_query_from_info(index.build_info or {}, task or "")
        result["recommended_action"] = {
            "type": "read_source" if result.get("compiled") else "answer"
        }
        result["evidence_card"] = None
        result["query_type"] = query_type
        result["index_status"] = status
        result["index_reason"] = reason
        return result
    else:
        from agentx.query.feature import search_feature

        result = search_feature(index, task or "")

    result["query_type"] = query_type
    result["index_status"] = status
    result["index_reason"] = reason
    result["evidence_card"] = None
    if result["confidence"] != "low":
        if query_type == "architecture":
            result["evidence_card"] = format_flow(
                result.get("flow", []), result.get("evidence", [])
            )
        elif query_type == "symbol":
            from agentx.query.evidence import format_symbol_card

            result["evidence_card"] = format_symbol_card(result)
        else:
            result["evidence_card"] = build_evidence_card(index, result)
    return result


async def _action_search_feature(
    app: Application,
    task: str,
    origin: str = "unknown",
    force_rebuild: bool = False,
    events: EventCollector | None = None,
    scope_selections: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """兼容旧调用：工程探索（等价 query type=feature）。"""
    return await _action_query(app, task, origin, force_rebuild, events, query_type="feature")


async def _action_build_status(
    app: Application,
    task: str,
    origin: str = "unknown",
    force_rebuild: bool = False,
    events: EventCollector | None = None,
    scope_selections: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build Reality 汇总：target / compiled / excluded / defines（无工程时 unknown）。"""
    from agentx.build import build_status_from_info
    from agentx.index.index import load_index

    index = load_index(app.project_root)
    if index is None:
        return {"build_status": "unknown", "error": "Project Index 不存在，请先调用 agentx.plan。"}
    return build_status_from_info(index.build_info or {})


_ActionFn = Callable[..., Awaitable[dict[str, Any]]]

_ACTIONS: dict[str, _ActionFn] = {
    "auto": _action_auto,
    "plan": _action_plan,
    "review": _action_review,
    "verify": _action_verify,
    "understand": _action_understand,
    "sync": _action_sync,
    "scope_update": _action_scope_update,
    "reindex": _action_reindex,
    "status": _action_status,
    "query": _action_query,
    "search_feature": _action_search_feature,
    "build_status": _action_build_status,
}

# Phase 8.1 权限模型：每个 action 的写权限分类（宿主 AI 据此决定是否需要代码审批）。
# READ / INDEX_WRITE（低风险、可逆，宿主可直接执行）/ CODE_WRITE_PREVIEW（规划）/
# CODE_WRITE（自动闭环：修改源码）。
OPERATION_METADATA: dict[str, dict[str, Any]] = {
    "query": {"class": "READ", "changes_code": False, "requires_decision_gate": False},
    "search_feature": {"class": "READ", "changes_code": False, "requires_decision_gate": False},
    "build_status": {"class": "READ", "changes_code": False, "requires_decision_gate": False},
    "status": {"class": "READ", "changes_code": False, "requires_decision_gate": False},
    "understand": {"class": "INDEX_WRITE", "changes_code": False, "requires_decision_gate": False},
    "sync": {"class": "INDEX_WRITE", "changes_code": False, "requires_decision_gate": False},
    "scope_update": {
        "class": "INDEX_WRITE",
        "changes_code": False,
        "requires_decision_gate": False,
    },
    "reindex": {"class": "INDEX_WRITE", "changes_code": False, "requires_decision_gate": False},
    "review": {"class": "READ", "changes_code": False, "requires_decision_gate": False},
    "verify": {"class": "READ", "changes_code": False, "requires_decision_gate": False},
    "plan": {"class": "CODE_WRITE_PREVIEW", "changes_code": True, "requires_decision_gate": True},
    "auto": {"class": "CODE_WRITE", "changes_code": True, "requires_decision_gate": True},
}


def operation_metadata(action: str) -> dict[str, Any]:
    """action → 权限元数据（未知 action 保守按 code_write 处理）。"""
    return OPERATION_METADATA.get(action, {"class": "CODE_WRITE", "changes_code": True,
                                           "requires_decision_gate": True})


def _decorate(action: str, out: dict[str, Any]) -> dict[str, Any]:
    """把权限元数据注入返回结果（顶层），宿主 AI 无需猜权限。"""
    meta = dict(operation_metadata(action))
    meta["action"] = action
    out["operation_class"] = meta["class"]
    out["changes_code"] = meta["changes_code"]
    out["requires_decision_gate"] = meta["requires_decision_gate"]
    return out

# build 类 action：Index=MISSING 且项目规模 ≥ 阈值时后台化
# （避免大项目长任务卡 300s RPC；小项目走同步路径保留 progress 流）
# Phase 8.1：reindex 是大项目最重操作，同样后台化；scope_update 只写配置，保持同步。
_BUILD_ACTIONS = {"auto", "plan", "sync", "understand", "reindex"}
_JOB_SOURCE_THRESHOLD = 200  # 源文件数 ≥ 此值 → 后台任务
# 短同步窗口：后台任务在此时间内完成 → 同步返回结果（测试/小项目体验不变）
_SYNC_WINDOW_SECONDS = 5.0


def _count_source_files(project_path: str) -> int:
    """源文件数量估算（c/h/cpp/hpp 递归计数，排除索引目录；<1s）。"""
    import glob

    root = Path(project_path).resolve()
    index_name = f"{root.name}_codebase_index"
    patterns = ("**/*.c", "**/*.h", "**/*.cpp", "**/*.hpp", "**/*.cc", "**/*.cxx")
    count = 0
    for pat in patterns:
        for p in glob.iglob(str(root / pat), recursive=True):
            rel = p[len(str(root)) + 1 :]
            if not rel.startswith(".") and not rel.startswith(index_name):
                count += 1
    return count


def _make_job_runner(
    action: str,
    project_path: str,
    task: str,
    origin: str,
    force_rebuild: bool,
    query_type: str,
    decision_choice: str | None = None,
    accept_blocked: bool = False,
) -> JobFn:
    """后台任务 runner：复现 agentx() 的 action 语义（无 session 流，降级静默）。

    enrich/semantic 是同步阻塞链路（worker subprocess）——必须在线程池执行，
    否则阻塞事件循环使同步窗口超时失效（大项目仍卡住 agentx 返回）。
    """

    async def _runner(params: dict[str, Any]) -> dict[str, Any]:
        import asyncio as _asyncio
        import functools as _functools

        scope_selections = params.get("scope_selections")

        def _run_blocking() -> dict[str, Any]:
            from agentx.index.index import index_status

            app = _app(project_path)
            events = EventCollector()
            before_state, _ = index_status(Path(project_path))
            try:
                if action == "query":
                    result = _asyncio.run(
                        _action_query(
                            app, task, origin, force_rebuild, events, query_type=query_type
                        )
                    )
                elif action in ("plan", "auto"):
                    handler = _ACTIONS.get(action)
                    if handler is None:
                        return {"error": f"未知 action: {action}"}
                    awaitable = handler(
                        app,
                        task,
                        origin,
                        force_rebuild,
                        events,
                        scope_selections=scope_selections,
                        decision_choice=decision_choice,
                        accept_blocked=accept_blocked,
                    )
                    result = _asyncio.run(cast("Coroutine[Any, Any, dict[str, Any]]", awaitable))
                else:
                    handler = _ACTIONS.get(action)
                    if handler is None:
                        return {"error": f"未知 action: {action}"}
                    awaitable = handler(
                        app,
                        task,
                        origin,
                        force_rebuild,
                        events,
                        scope_selections=scope_selections,
                    )
                    result = _asyncio.run(cast("Coroutine[Any, Any, dict[str, Any]]", awaitable))
                return _wrap(
                    result,
                    app,
                    action,
                    events,
                    force_rebuild=force_rebuild,
                    before_state=before_state.value,
                )
            finally:
                app.store.close()

        return await _asyncio.to_thread(_functools.partial(_run_blocking))

    return _runner


def _job_status_view(job: Any) -> dict[str, Any]:
    """job 状态视图（top-level status 字段兼容 Reasonix 判断）。"""
    view: dict[str, Any] = job.to_dict()
    view["status"] = job.status  # running | scope_required | completed | failed
    return view


@server.tool(
    name="agentx",
    description=(
        "AgentX 统一入口（项目认知层 + 工程能力层）。"
        "AgentX maintains project knowledge: Index + CodeGraph + Understanding + Build Reality. "
        "Before exploring source files, query AgentX knowledge first to obtain project evidence. "
        "Direct filesystem search should only happen when AgentX evidence is insufficient."
        "Use query for: where is this feature implemented / how does this module work / "
        "find related code. "
        "Use plan for: modifying code / designing changes / impact analysis. "
        "Use review for: checking changes. "
        "action 支持："
        "query（Project Knowledge Query，query_type=feature|symbol|architecture，"
        "返回证据卡，不扫描工程）/ "
        "auto（默认：Plan→Review→Verify 完整闭环）/ "
        "plan（修改任务：项目认知+实施方案）/ "
        "review（Index+Plan+Diff 审查）/ "
        "verify（机器验证）/ "
        "understand（主动刷新工程理解）/ "
        "sync（Index 同步，scope 变化自动强制重建）/ "
        "scope_update（Phase 8.1 INDEX_WRITE：改 .agentxscope.yaml 的 ignore/"
        "third_party/build_target，写 AgentX 自身配置，不需代码审批）/ "
        "reindex（Phase 8.1 INDEX_WRITE：强制重建 Index，使用最新 scope，"
        "不需代码审批）/ "
        "status（Index 状态与项目认知概览）。"
        "【权限边界】每次返回顶层带 operation_class（READ/INDEX_WRITE/"
        "CODE_WRITE_PREVIEW/CODE_WRITE）+ changes_code + requires_decision_gate。"
        "修改用户源码请用 plan/auto（走 Decision Gate）；维护索引用 "
        "scope_update→reindex 或 sync/understand（无需代码审批）。"
        "AgentX 自动维护 Index：VALID 复用、STALE 同步、仅缺失/损坏/force_rebuild 重建。"
        "【Scope 首次初始化协议】首次使用（项目根目录无 .agentxscope.yaml 且检测到 "
        "ignore/third_party 建议）时，plan/auto/sync/understand 会返回 "
        "status=scope_required + suggestions（ignore/third_party 路径列表），"
        "此时不会建立 Index。你必须携带 scope_selections 参数重新调用确认范围，"
        '例如 ignore 填 ["docs/**", "tools/**"]、third_party 填 ["Middlewares/LVGL"]；'
        "确认后才会生成 .agentxscope.yaml 并继续建立 Index。"
        "已存在 .agentxscope.yaml 的项目直接执行，无需 scope_selections。"
        "返回 {result, runtime, events}：result 为业务结果，"
        "runtime 含 index_state/fingerprint/decision，events 为 workflow 阶段事件。"
        "运行期间通过 MCP notification（notifications/message + notifications/progress）"
        "实时推送工作流事件与心跳；不支持时忽略，最终 result 不受影响。"
        "失败返回 error 字段，不会崩溃。"
    ),
)
async def agentx(
    project_path: str,
    task: str,
    action: str = "auto",
    origin: str = "unknown",
    force_rebuild: bool = False,
    query_type: str = "feature",
    scope_selections: dict[str, Any] | None = None,
    job_id: str | None = None,
    decision_choice: str | None = None,
    decision_action: str = "candidate_select",
    context: Context | None = None,
) -> dict[str, Any]:
    # context：SDK 按类型注解（Context）注入的请求上下文（拿 ServerSession 做实时流）；
    # 直调（测试）不传时为 None → 流式自动降级，任务不受影响。
    # Phase 7.9.2 体验：job_id 提供 → 状态查询 / scope 确认续跑
    if job_id is not None:
        mgr = job_manager()
        job = mgr.get(job_id)
        if job is None:
            return _decorate(action, {"error": f"未知 job_id: {job_id}"})
        if scope_selections is not None and job.status in ("scope_required", "failed"):
            # 挂起任务续跑：原始参数 + scope 确认
            runner = _make_job_runner(
                job.action,
                job.project_path,
                job.params.get("task", ""),
                job.params.get("origin", "unknown"),
                bool(job.params.get("force_rebuild", False)),
                str(job.params.get("query_type", "feature")),
                decision_choice=str(job.params.get("decision_choice") or ""),
                accept_blocked=bool(job.params.get("accept_blocked", False)),
            )
            resumed = mgr.resume(job_id, runner, scope_selections)
            if resumed is None:
                return _decorate(
                    action, {"error": f"job {job_id} 状态不可续跑（{job.status}）"}
                )
            return _decorate(action, _job_status_view(resumed))
        if action == "status":
            return _decorate(action, {"job": _job_status_view(job)})
        return _decorate(
            action,
            {"error": f"job {job_id} 正在运行（{job.status}），使用 action=status 查询"},
        )
    # Phase 7.8：decision_action 控制（用户取消 / 仅查看影响链）
    if action in ("plan", "auto") and decision_action == "cancel":
        return _decorate(action, {"status": "cancelled", "message": "已取消修改规划（用户决定）"})
    # 长任务后台化：build 类 action + Index=MISSING + 项目规模 ≥ 阈值 →
    # 后台任务 + 短同步窗口（5s 内完成同步返回；否则返回 running + job_id）
    from agentx.index.index import IndexStatus, index_status

    if action in _BUILD_ACTIONS and not force_rebuild:
        pre_status, _ = index_status(Path(project_path))
        if (
            pre_status == IndexStatus.MISSING
            and _count_source_files(project_path) >= _JOB_SOURCE_THRESHOLD
        ):
            mgr = job_manager()
            params = {
                "task": task,
                "origin": origin,
                "force_rebuild": force_rebuild,
                "query_type": query_type,
                "scope_selections": scope_selections,
                "decision_choice": decision_choice,
                "accept_blocked": decision_action == "accept_blocked",
            }
            runner = _make_job_runner(
                action,
                project_path,
                task,
                origin,
                force_rebuild,
                query_type,
                decision_choice,
                decision_action == "accept_blocked",
            )
            job = mgr.submit(action, project_path, params, runner)
            if job.task is not None:
                done, _ = await asyncio.wait({job.task}, timeout=_SYNC_WINDOW_SECONDS)
            if job.status == "completed" and job.result is not None:
                return sanitize_value(job.result)  # 短窗口内完成：同步返回
            if job.status == "scope_required":
                return _decorate(action, _job_status_view(job))  # 挂起等 Scope 确认（不假失败）
            return _decorate(
                action,
                {
                    "status": "running",
                    "job_id": job.id,
                    "phase": "building_index",
                    "message": "Index 构建中（后台任务），使用 action=status + job_id 查询进度",
                },
            )
    handler = _ACTIONS.get(action)
    if handler is None:
        return _decorate(
            action,
            sanitize_value(
                {
                    "error": f"未知 action: {action}（支持: {', '.join(_ACTIONS)}）",
                }
            ),
        )
    app = _app(project_path)
    events = EventCollector()
    # Phase 6.6/6.7：实时流（observability only——stream 失败绝不影响任务）
    session = _session_from_context(context)
    token = _progress_token_from_context(context)
    heartbeat: Heartbeat | None = None
    pending: list[asyncio.Task[None]] = []
    if session is not None:
        from agentx.runtime.progress import ProgressAdapter

        # 双通道：notifications/message（兼容宿主）+ notifications/progress（Reasonix 主通道）
        events.subscribe(_make_stream_listener(session, pending))
        adapter: ProgressAdapter | None = None
        if token is not None:
            adapter = ProgressAdapter(session, token, pending)
            events.subscribe(adapter.on_event)
        heartbeat = Heartbeat(
            events,
            on_beat=_make_heartbeat_sender(session, pending, adapter),
        )
        heartbeat.start()
    from agentx.index.index import index_status

    before_state, _ = index_status(app.project_root)
    try:
        if action == "query":
            result = await _action_query(
                app, task, origin, force_rebuild, events, query_type=query_type
            )
        elif action in ("plan", "auto"):
            result = await handler(
                app,
                task,
                origin,
                force_rebuild,
                events,
                scope_selections=scope_selections,
                decision_choice=decision_choice,
                accept_blocked=(decision_action == "accept_blocked"),
            )
        else:
            result = await handler(
                app, task, origin, force_rebuild, events, scope_selections=scope_selections
            )
        # Phase 7.8：view_impact——只展开候选影响链，不进入 Plan
        if (
            action in ("plan", "auto")
            and decision_action == "view_impact"
            and isinstance(result, dict)
            and result.get("status") == "decision_required"
        ):
            result = {**result, "note": "未进入规划（decision_action=view_impact，仅查看影响链）"}
        # 输出边界统一清洗（Phase 7.9.1）：任何非法编码/surrogate 字段
        # 在此替换为 '?'，保证 response 可被 pydantic/JSON 序列化；
        # 内部数据（index/semantic/filesystem）保持原样。
        return sanitize_value(
            _wrap(
                result,
                app,
                action,
                events,
                force_rebuild=force_rebuild,
                before_state=before_state.value,
            )
        )
    except Exception as e:
        # LLMRequestError：结构化输出（formatter 不做字符串解析）
        from agentx.providers.openai import LLMRequestError

        if isinstance(e, LLMRequestError):
            return _decorate(
                action,
                sanitize_value(
                    {
                        "error": f"{action} 失败: [{e.category}] {e.detail}",
                        "llm_error": e.to_dict(),
                    }
                ),
            )
        return _decorate(
            action, sanitize_value({"error": f"{action} 失败: {type(e).__name__}: {e}"})
        )
    finally:
        if heartbeat is not None:
            heartbeat.stop()
        # 兜底：确保所有 notification 在返回前送达（不因未运行而丢失）
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        app.store.close()


@server.tool(
    name="agentx.capabilities",
    description=(
        "AgentX 权限边界（Phase 8.1）。返回每个 action 的 operation_class："
        "READ（直接允许）/ INDEX_WRITE（修改 AgentX 自身索引/scope 配置，宿主可直接执行，"
        "不需代码审批）/ CODE_WRITE_PREVIEW（plan：规划修改用户代码，需 Decision Gate）/ "
        "CODE_WRITE（auto：修改用户源码，需人工决策）。宿主 AI 据此决定是否走审批，无需猜测。"
    ),
)
async def agentx_capabilities() -> dict[str, Any]:
    return {
        "operations": {
            action: dict(operation_metadata(action)) for action in sorted(_ACTIONS)
        }
    }


async def main_async() -> None:
    await server.run_stdio_async()


def main() -> None:
    """MCP server 入口：stdio 传输，供 Reasonix / Claude 等客户端拉起。"""
    asyncio.run(main_async())


if __name__ == "__main__":
    main()
