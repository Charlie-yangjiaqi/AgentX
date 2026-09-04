"""Verify 服务：判断任务是否真的完成。

机器证据优先：Build → Tests → Evidence → Verdict。
Verify 尽量少用 LLM——能通过命令执行得到答案，就不让模型猜。
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

from agentx.app.application import Application
from agentx.index.fingerprint import compute_fingerprint as _cf
from agentx.index.index import IndexStatus, index_exclude_name, index_status
from agentx.plan.service import load_plan
from agentx.tools.base import ROLE_PERMISSIONS, ToolContext

# 命令提取：从（可能混入自然语言的）verification 文本中取出第一条可执行命令
_CMD_PATTERN = re.compile(
    r"(?:^|[\n：:;；])\s*"
    r"((?:gcc|cc|mingw32-make|make|python|pytest|cargo|go\s+build|node|npm|echo|del|dir)"
    r"[^\n。；;]*?)"
    r"(?:$|[\n。；;])"
)


def _extract_command(text: str) -> str:
    """Plan 的 verification 可能混入自然语言：提取第一条可执行命令。"""
    text = text.strip()
    m = _CMD_PATTERN.search(text)
    if m:
        return m.group(1).strip()
    return text


class VerifyService:
    def __init__(self, application: Application) -> None:
        self.app = application

    async def verify(
        self,
        goal: str,
        progress: Callable[[str], None] | None = None,
        origin: str = "unknown",
        on_event: Callable[[str, str, str], None] | None = None,
    ) -> dict[str, Any]:
        """确定性验证：执行 Plan 中的验证方案，产出 Evidence 与 Verdict。

        on_event：结构化 workflow 事件回调 (stage, status, message)；None 时行为不变。
        """

        def emit(stage: str, status: str, message: str = "") -> None:
            if on_event is not None:
                on_event(stage, status, message)

        root = self.app.project_root
        emit("index_check", "running", "checking fingerprint")
        if progress is not None:
            progress("[1/3] 核对 Index 与 Plan")
        status, reason = index_status(root)
        emit("index_check", "completed", f"{status} {reason}")
        if status in {IndexStatus.MISSING, IndexStatus.CORRUPTED}:
            emit("index_decision", "completed", "rebuild_index: index unavailable")
            return {
                "error": "Project Index 不可用，请先调用 agentx.plan。",
                "index_status": status,
            }
        if status == IndexStatus.STALE:
            # 任务前置检查：禁止用旧 Index 验证 → Freshness 分级自动维护；REQUIRED 硬停
            emit("index_decision", "completed", "sync_index: project changed since index")
            from agentx.index.sync import sync_index

            emit("index_sync", "running", "syncing project knowledge")
            sync_result = sync_index(root, origin=origin, progress=progress)
            emit("index_sync", "completed", f"{sync_result['level']}: {sync_result['message']}")
            if sync_result.get("action") == "reindex_required":
                freshness = sync_result.get("index_freshness") or {}
                return {
                    "error": (
                        "工程变化需完整重建 Index 后才能验证"
                        f"（原因: {freshness.get('reason', sync_result.get('message', ''))}）。"
                        "请先 action=reindex。"
                    ),
                    "index_status": "REINDEX_REQUIRED",
                    "index_freshness": freshness,
                    "requires_confirmation": True,
                }
        else:
            from agentx.runtime.context import decide_index_action

            decision = decide_index_action(status.value)
            emit("index_decision", "completed", f"{decision['action']}: {decision['reason']}")
        plan = load_plan(root)
        if plan is None:
            return {"error": "没有 Plan，请先调用 agentx.plan 制定验证方案。"}

        verification = _extract_command((plan.verification or "").strip())
        if not verification and plan.validation.commands:
            verification = plan.validation.commands[0]
        if not verification:
            return {
                "error": "Plan 未提供 verification 命令，无法进行确定性验证。",
                "index_status": status,
            }
        if progress is not None:
            progress(f"[2/3] 执行验证命令: {verification}")
        emit("verify", "running", f"executing: {verification}")
        ctx = ToolContext(project_root=root)
        permissions = ROLE_PERMISSIONS["verifier"]
        result = await self.app.orchestrator.registry.execute(
            "test.run",
            {"command": verification},
            ctx,
            permissions,
            task_id="_verify",
            agent_id="verify",
        )
        if progress is not None:
            progress(f"[3/3] 命令完成 exit={result.exit_code}")
        emit("verify", "completed", f"exit={result.exit_code}")
        emit("completed", "completed", "verify done")

        evidence = [
            {
                "type": "test.run",
                "command": verification,
                "exit_code": result.exit_code,
                "output": (result.output or "")[:2000],
            }
        ]
        passed = result.ok and result.exit_code == 0
        verdict = "PASS" if passed else "FAIL"
        conclusion = (
            f"验证命令 exit={result.exit_code}"
            if result.exit_code is not None
            else f"验证执行失败: {result.error}"
        )
        return {
            "index_status": status,
            "fingerprint": _cf(root, extra_excludes={index_exclude_name(root)}),
            "verdict": verdict,
            "build": {},
            "tests": [{"command": verification, "passed": passed}],
            "evidence": evidence,
            "conclusion": conclusion,
        }
