"""Core Path Understanding：把 Project Index 从"代码关系数据库"升级为"工程认知模型"。

事实层（CodeGraph/symbols/call_graph）与解释层（project_understanding）严格分离：
- 入口候选发现：规则化、零 LLM、可验证（符号名模式 + Build Reality 交叉）
- 核心路径探索：LLM 沿 Entry → Init Flow → Core Task 有限探索（不读全项目）
- 过期判断：project_understanding 内嵌基于的 fingerprint，不一致即过期；
  Plan 时按需刷新（命中相关区域才重新探索，不每次重建）

触发策略（A+B 混合）：
- Index 首次建立/重大重建 → 自动探索一次
- 普通任务 Plan → 校验 fingerprint，过期且命中相关区域才刷新
- 用户主动 → agentx understand
"""

from __future__ import annotations

import json
import re
from typing import Any

from agentx.index.index import ProjectIndex, load_index, save_index
from agentx.state.models import AgentXModel

# 入口符号名模式（规则化，与 Build Reality 交叉验证）
_ENTRY_SYMBOL_PATTERNS: list[tuple[str, str, str]] = [
    (r"^(main|app_main)$", "contains main function", "high"),
    (r"^startup(_\w+)?$", "startup entry", "high"),
    (r"^vTaskStartScheduler$", "RTOS scheduler start", "medium"),
    (r"^osKernelStart$", "CMSIS-RTOS kernel start", "medium"),
]

# RTOS 任务创建调用者（创建位置 = 业务启动点）
_RTOS_TASK_CREATORS = {"xTaskCreate", "osThreadNew", "xTaskCreateStatic"}


class EntryCandidate(AgentXModel):
    file: str
    symbol: str
    reason: str
    confidence: str  # high | medium | low


def discover_entry_candidates(index: ProjectIndex) -> list[EntryCandidate]:
    """规则化入口发现：符号名模式 + Build Reality 交叉。

    返回候选列表（有序：confidence high 在前）。永不失败，无命中返回空。
    """
    candidates: list[EntryCandidate] = []
    built = {
        str(f.get("file", "")).split("/")[-1] for f in index.build_info.get("compiled_files", [])
    }

    for sym in index.symbols:
        name = str(sym.get("name", ""))
        fpath = str(sym.get("file", ""))
        base = fpath.split("/")[-1]
        for pattern, reason, confidence in _ENTRY_SYMBOL_PATTERNS:
            if re.match(pattern, name):
                # Build Reality 交叉：参与编译的入口置信度上调
                eff_conf = confidence
                if base in built and confidence != "high":
                    eff_conf = "high"
                candidates.append(
                    EntryCandidate(file=fpath, symbol=name, reason=reason, confidence=eff_conf)
                )
                break
        if name in _RTOS_TASK_CREATORS:
            # 找到 task 创建函数的调用者（1-hop callers）作为业务起点
            for e in index.call_graph:
                if e.get("callee") == name:
                    caller = str(e.get("caller", ""))
                    caller_file = _symbol_file(index, caller)
                    if caller_file:
                        candidates.append(
                            EntryCandidate(
                                file=caller_file,
                                symbol=caller,
                                reason=f"creates task via {name}",
                                confidence="medium",
                            )
                        )

    # 去重（file+symbol），保序
    seen: set[tuple[str, str]] = set()
    unique: list[EntryCandidate] = []
    for c in candidates:
        key = (c.file, c.symbol)
        if key not in seen:
            seen.add(key)
            unique.append(c)
    order = {"high": 0, "medium": 1, "low": 2}
    unique.sort(key=lambda c: (order.get(c.confidence, 3), c.file))
    return unique


def _symbol_file(index: ProjectIndex, name: str) -> str | None:
    for sym in index.symbols:
        if sym.get("name") == name:
            return str(sym.get("file", ""))
    return None


def understanding_status(index: ProjectIndex, current_fingerprint: str) -> tuple[bool, str]:
    """判断工程理解是否可用。

    返回 (可用, 说明)。规则：
    - 无理解 → 不可用（"未建立"）
    - fingerprint 一致 → 可用
    - fingerprint 不一致 → 不可用（"过期"）
    """
    u = index.project_understanding or {}
    if not u:
        return False, "工程理解未建立"
    if u.get("based_on_fingerprint") == current_fingerprint:
        return True, "工程理解有效"
    return False, "工程理解过期（Index 已变化）"


def understanding_hits_goal(index: ProjectIndex, goal: str) -> bool:
    """任务是否命中理解层相关区域（入口/核心文件/关键路径）。

    命中才值得按需刷新理解。规则化：Query 子图文件与 understanding
    覆盖文件的交集非空。
    """
    from agentx.understanding.query import query_index

    u = index.project_understanding or {}
    covered: set[str] = set()
    for key in ("core_modules", "critical_files", "startup_flow"):
        for f in u.get(key, []) or []:
            covered.add(str(f).lower())
    for e in u.get("entry_points", []) or []:
        covered.add(str(e.get("file", "")).lower())

    result = query_index(index, goal)
    for f in result.get("files", []):
        if str(f.get("path", "")).lower() in covered:
            return True
    for s in result.get("symbols", []):
        if str(s.get("name", "")) in {e.get("symbol") for e in u.get("entry_points", []) or []}:
            return True
    return False


# ---------- LLM 核心路径探索 ----------

_EXPLORE_PROMPT = (
    "你是 AgentX 的项目理解分析师。目标：基于 Project Index 认知，沿核心路径做有限探索，"
    "形成工程理解。\n\n"
    "约束（严格遵守）：\n"
    "1. 只沿 Entry Point → Initialization Flow → Core Task → Important Modules 探索，"
    "不读全项目源码\n"
    "2. 需要细节时用工具（fs.read_file / project.inspect）读取必要文件，控制在 5 个文件以内\n"
    "3. 输出必须是 JSON 对象（不要任何额外文字），字段：\n"
    '\n{\n  "architecture_summary": "一两句话描述项目架构",\n'
    '  "startup_flow": ["按顺序列出启动流程涉及的文件或符号"],\n'
    '  "core_modules": ["核心业务模块文件路径"],\n'
    '  "critical_files": ["修改风险最高的文件路径"]\n}\n\n'
    "4. 不确定的项目不要编造：core_modules 可为空数组"
)


async def explore_understanding(
    app: Any,
    index: ProjectIndex,
    candidates: list[EntryCandidate],
    progress: Any = None,
) -> dict[str, Any]:
    """LLM 核心路径探索：返回 project_understanding 内容（不含元数据）。

    复用 plan 角色的 runtime（provider/工具），沿入口候选做有限探索。
    """
    from agentx.core.orchestrator import _env_hint
    from agentx.core.progress import ProgressReporter
    from agentx.providers.messages import ChatMessage
    from agentx.understanding.query import format_query_result, query_index

    runtime = app.orchestrator.agents.get("plan")
    if runtime is None:
        raise RuntimeError("Plan agent 未配置，无法执行理解探索")

    reporter = ProgressReporter(app.event_bus, progress) if progress else None
    if reporter is not None:
        reporter.start()
    try:
        entry_lines = "\n".join(
            f"  {c.file} ({c.symbol}) [{c.confidence}] {c.reason}" for c in candidates
        )
        preview = query_index(index, " ".join(c.symbol for c in candidates))
        messages = [
            ChatMessage(role="user", content=_EXPLORE_PROMPT),
            ChatMessage(role="user", content=_env_hint()),
            ChatMessage(
                role="user",
                content=(
                    f"候选入口：\n{entry_lines or '  （未发现，请基于项目结构自行判断）'}\n\n"
                    "项目认知（Index 子图）：\n"
                    f"{format_query_result(preview)}"
                ),
            ),
        ]
        ctx = app.orchestrator._ctx(app._dummy_task())
        result = await runtime.run(messages, ctx)
    finally:
        if reporter is not None:
            reporter.close()

    understanding = _parse_understanding(result.content or "")
    return understanding


def _parse_understanding(content: str) -> dict[str, Any]:
    """解析 LLM 输出的理解 JSON；失败时降级为空理解（不失败）。"""
    text = content.strip()
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if m is None:
        return _empty_understanding()
    try:
        data = json.loads(m.group(0))
    except json.JSONDecodeError:
        return _empty_understanding()
    out = _empty_understanding()
    for key in ("architecture_summary", "startup_flow", "core_modules", "critical_files"):
        if key in data and isinstance(data[key], (str, list)):
            out[key] = data[key]
    return out


def _empty_understanding() -> dict[str, Any]:
    return {
        "architecture_summary": "",
        "startup_flow": [],
        "core_modules": [],
        "critical_files": [],
    }


def format_understanding(u: dict[str, Any]) -> str:
    """把工程理解转成 Planner 可读文本。"""
    lines: list[str] = []
    if u.get("architecture_summary"):
        lines.append(f"架构: {u['architecture_summary']}")
    entries = u.get("entry_points") or []
    if entries:
        lines.append("入口:")
        for e in entries:
            if isinstance(e, dict):
                lines.append(
                    f"  {e.get('file')} ({e.get('symbol')}) [{e.get('confidence')}] "
                    f"{e.get('reason')}"
                )
    flows = u.get("startup_flow") or []
    if flows:
        lines.append(f"启动流程: {' -> '.join(str(f) for f in flows[:10])}")
    cores = u.get("core_modules") or []
    if cores:
        lines.append(f"核心模块: {', '.join(str(c) for c in cores[:10])}")
    criticals = u.get("critical_files") or []
    if criticals:
        lines.append(f"高影响文件: {', '.join(str(c) for c in criticals[:10])}")
    if not lines:
        lines.append("（暂无工程理解）")
    return "\n".join(lines)


async def ensure_understanding(
    app: Any,
    project_root: Any,
    goal: str | None = None,
    *,
    force: bool = False,
    progress: Any = None,
    scope_selections: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """工程理解保障：A+B 混合策略。

    - Index 无理解 → 自动探索一次（A：首次建立）
    - 理解过期（fingerprint 变）→ 任务命中相关区域才刷新（B：按需）
    - force=True → 无条件重建（用户主动 agentx understand）

    Phase 7.9.2 体验：Index=MISSING 不再直接跳过——先 ensure_index
    bootstrap（scope gate 前置，不绕过），再执行理解探索。
    返回 {"status": "created"|"refreshed"|"reused"|"skipped"|"scope_required", ...}
    """
    from agentx.index.fingerprint import compute_fingerprint as _cf
    from agentx.index.index import index_exclude_name
    from agentx.plan.service import enrich_index, ensure_index, is_skeleton_index

    index = load_index(project_root)
    if index is None:
        # 统一 bootstrap：ensure_index 内部处理 scope gate（返回 scope_required）
        status, reason, idx = ensure_index(project_root, scope_selections=scope_selections)
        if idx is None:
            from agentx.scope.initializer import check_scope_init

            gate = check_scope_init(project_root) or {}
            return {
                "status": "scope_required",
                "message": gate.get("message", "Need user confirmation before index build"),
                "suggestions": gate.get("suggestions", {}),
                "index_before": {"status": str(status), "reason": reason},
            }
        index = load_index(project_root)
        if index is None:
            return {"status": "skipped", "message": "Index 不存在，先建立 Index"}
    # Phase 7.9 Index 完整性：ensure_index 建的骨架（files 无认知）必须 enrich 补全
    # （CodeGraph symbols/call_graph + semantic + modules）。骨架被当 VALID 复用会让
    # 后续 understand 一直拿到 files 有、symbols/modules 全空的伪完整 Index。
    if is_skeleton_index(index):
        try:
            index, _ = enrich_index(project_root)
        except Exception as e:  # enrich 失败：明确 degraded，不静默当完整 Index
            return {
                "status": "degraded",
                "message": f"Index 骨架已建立，但认知补全失败（degraded）: {type(e).__name__}: {e}",
            }

    current = _cf(project_root, extra_excludes={index_exclude_name(project_root)})
    usable, reason = understanding_status(index, current)

    if force:
        pass  # 无条件重建
    elif usable:
        return {"status": "reused", "message": reason}
    elif index.project_understanding and goal and not understanding_hits_goal(index, goal):
        # 过期：任务未命中相关区域 → 跳过刷新
        return {"status": "skipped", "message": f"{reason}，且任务未命中理解层，暂不刷新"}

    candidates = discover_entry_candidates(index)
    try:
        understanding = await explore_understanding(app, index, candidates, progress=progress)
    except Exception as e:
        return {"status": "failed", "message": f"工程理解探索失败（不阻塞任务）: {e}"}
    if not understanding.get("architecture_summary") and not understanding.get("core_modules"):
        return {
            "status": "failed",
            "message": "工程理解探索无有效输出（不阻塞任务）",
        }
    index.project_understanding = dict(understanding)
    index.project_understanding["based_on_fingerprint"] = current
    index.project_understanding["source"] = "llm"
    index.project_understanding["entry_points"] = [c.model_dump() for c in candidates]
    save_index(project_root, index)
    verb = "created" if not usable else "refreshed"
    return {"status": verb, "message": f"工程理解已{'建立' if verb == 'created' else '刷新'}"}
