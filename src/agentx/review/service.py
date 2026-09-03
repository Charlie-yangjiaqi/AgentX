"""Review 服务：审查 Reasonix 已完成的修改。

输入最小上下文：Project Index + Plan + Git Diff + 必要局部代码。
默认不重新扫描整个项目。
Review 挑问题，不负责修复。
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

from agentx.app.application import Application
from agentx.index.index import IndexStatus, index_status, load_index
from agentx.plan.service import _index_preview, load_plan
from agentx.providers.messages import ChatMessage

_NO_DIFF_HINT = "(无 Git Diff：项目不是 git 仓库，请结合 Index 与 Plan 审查当前代码)"


async def _git_diff(project_root: Path) -> str:
    """获取未提交修改的 Git Diff（项目非 git 仓库时返回空）。"""
    try:
        proc = await asyncio.create_subprocess_exec(
            "git",
            "diff",
            cwd=project_root,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        output, _ = await asyncio.wait_for(proc.communicate(), timeout=30)
        if proc.returncode != 0:
            return ""
        text = output.decode("utf-8", errors="replace").strip()
        return text[:8000]
    except Exception:
        return ""


def _parse_review(content: str) -> tuple[str, list[dict[str, Any]]]:
    """解析 Review 输出：verdict + findings（容错取第一个 JSON 对象）。"""
    start = content.find("{")
    end = content.rfind("}")
    if start == -1 or end <= start:
        return "FAIL", []
    try:
        data: dict[str, Any] = json.loads(content[start : end + 1])
    except json.JSONDecodeError:
        return "FAIL", []
    verdict = str(data.get("verdict", "FAIL"))
    findings = data.get("findings")
    if not isinstance(findings, list):
        findings = []
    return verdict, [f for f in findings if isinstance(f, dict)]


class ReviewService:
    def __init__(self, application: Application) -> None:
        self.app = application

    async def review(
        self,
        goal: str,
        progress: Callable[[str], None] | None = None,
        origin: str = "unknown",
        on_event: Callable[[str, str, str], None] | None = None,
    ) -> dict[str, Any]:
        """审查当前修改：Index + Plan + Diff 最小上下文。

        on_event：结构化 workflow 事件回调 (stage, status, message)；None 时行为不变。
        """
        from agentx.core.orchestrator import _env_hint
        from agentx.core.progress import ProgressReporter

        def emit(stage: str, status: str, message: str = "") -> None:
            if on_event is not None:
                on_event(stage, status, message)

        root = self.app.project_root
        emit("index_check", "running", "checking fingerprint")
        if progress is not None:
            progress("[1/5] 核对 Index 指纹")
        status, reason = index_status(root)
        emit("index_check", "completed", f"{status} {reason}")

        if status == IndexStatus.MISSING:
            emit("index_decision", "completed", "rebuild_index: index missing")
            return {
                "error": "Project Index 不存在，请先调用 agentx.plan 建立项目认知。",
                "index_status": status,
            }
        if status == IndexStatus.CORRUPTED:
            emit("index_decision", "completed", "rebuild_index: index corrupted")
            return {
                "error": "Project Index 损坏，请先调用 agentx.plan 重建。",
                "index_status": status,
            }
        if status == IndexStatus.STALE:
            # 任务前置检查：Index Sync（分级维护 + 外部变化报告）
            emit("index_decision", "completed", "sync_index: project changed since index")
            from agentx.index.sync import sync_index

            emit("index_sync", "running", "syncing project knowledge")
            sync_result = sync_index(root, origin=origin, progress=progress)
            emit("index_sync", "completed", f"{sync_result['level']}: {sync_result['message']}")
            reason = f"Index 已同步（原状态 STALE: {reason}）→ {sync_result['message']}"
        else:
            from agentx.runtime.context import decide_index_action

            decision = decide_index_action(status.value)
            emit("index_decision", "completed", f"{decision['action']}: {decision['reason']}")

        index = load_index(root)
        plan = load_plan(root)
        if index is None:
            return {"error": "Index 读取失败", "index_status": status}
        if plan is None:
            return {
                "error": "没有 Plan，请先调用 agentx.plan 制定实施方案。",
                "index_status": status,
            }

        diff = await _git_diff(root)
        if progress is not None:
            progress("[2/5] 读取 Git Diff")
            progress("[3/5] 组装审查上下文（Index + Plan + Diff）")
        from agentx.understanding.query import format_query_result, query_index

        query_result = query_index(index, goal)
        emit(
            "query_context",
            "completed",
            f"命中 {len(query_result['files'])} 文件 / {len(query_result['symbols'])} 符号",
        )
        if query_result["files"] or query_result["symbols"]:
            context_note = f"任务相关认知子图（Index Query）：\n{format_query_result(query_result)}"
        else:
            context_note = f"项目认知（Index 预览）：\n{_index_preview(index)}"
        reporter = ProgressReporter(self.app.event_bus, progress) if progress else None
        if reporter is not None:
            reporter.start()
        try:
            runtime = self.app.orchestrator.agents.get("reviewer")
            if runtime is None:
                raise RuntimeError("Reviewer agent 未配置")
            ctx = self.app.orchestrator._ctx(self.app._dummy_task())
            messages = [
                ChatMessage(role="user", content=f"任务目标：{goal}"),
                ChatMessage(role="user", content=_env_hint()),
                ChatMessage(
                    role="user",
                    content=(f"Index 状态: {status}（{reason}）\n{context_note}"),
                ),
                ChatMessage(
                    role="user",
                    content=f"实施计划（Plan）：\n{plan.model_dump_json(indent=2)}",
                ),
                ChatMessage(
                    role="user",
                    content=(f"待审查的修改（Git Diff）：\n{diff if diff else _NO_DIFF_HINT}"),
                ),
            ]
            if progress is not None:
                progress("[4/5] Reviewer 审查修改")
            emit("review", "running", "reviewer checking changes")
            result = await runtime.run(messages, ctx)
        finally:
            if reporter is not None:
                reporter.close()
        verdict, findings = _parse_review(result.content or "")
        emit("review", "completed", f"verdict: {verdict}")
        emit("completed", "completed", "review done")
        return {
            "index_status": status,
            "verdict": verdict,
            "findings": findings,
            "diff": diff[:2000],
        }
