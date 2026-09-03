"""Decision Engine：规则优先、模型辅助。

Reviewer / Verifier 可以提出 Finding 和结论，但最终是否 PASS
由结构化规则 + 可验证证据决定，不依赖模型一句"完成"。
"""

from __future__ import annotations

from typing import Any

from agentx.state.models import AgentXModel, Evidence, Finding, FindingSeverity, FindingStatus


class VerdictCommand(AgentXModel):
    command: str
    required: bool = True


class Verdict(AgentXModel):
    """Verifier 的结构化结论（由 LLM 输出，由规则核验）。"""

    build: VerdictCommand | None = None
    tests: list[VerdictCommand] = []
    conclusion: str = "FAIL"
    notes: str | None = None


class DecisionRules(AgentXModel):
    max_iterations: int = 5
    """修复迭代上限：超过则判定失败，转人工。"""


class DecisionResult(AgentXModel):
    outcome: str  # PASS | FAIL
    reasons: list[str] = []

    @property
    def is_pass(self) -> bool:
        return self.outcome == "PASS"


class DecisionEngine:
    def __init__(self, rules: DecisionRules | None = None) -> None:
        self.rules = rules or DecisionRules()

    def decide(
        self,
        *,
        findings: list[Finding],
        evidence: list[Evidence],
        verdict: Verdict | None,
        iteration: int,
    ) -> DecisionResult:
        reasons: list[str] = []
        ok = True

        blockers = [
            f
            for f in findings
            if f.status == FindingStatus.OPEN
            and f.severity in {FindingSeverity.BLOCKER, FindingSeverity.HIGH}
        ]
        if blockers:
            ok = False
            reasons.append(f"存在 {len(blockers)} 个 BLOCKER/HIGH Finding 未解决")

        if verdict is None:
            ok = False
            reasons.append("Verifier 未给出结构化结论")
        else:
            checks = [(verdict.build, "build")] if verdict.build else []
            checks += [
                (VerdictCommand(command=t.command, required=t.required), "test")
                for t in verdict.tests
            ]
            for cmd, kind in checks:
                if not cmd.required:
                    reasons.append(f"[{kind}] 未要求验证: {cmd.command}")
                    continue
                ev = _find_evidence(evidence, cmd.command)
                if ev is not None and ev.exit_code == 0:
                    reasons.append(f"[{kind}] 通过: {cmd.command} (exit=0)")
                else:
                    ok = False
                    reasons.append(
                        f"[{kind}] 未通过: {cmd.command} (exit={ev.exit_code if ev else '无证据'})"
                    )
            if verdict.conclusion == "FAIL":
                ok = False
                reasons.append(f"Verifier 结论 FAIL: {verdict.notes or '未说明'}")

        if iteration >= self.rules.max_iterations:
            ok = False
            reasons.append(f"迭代次数达上限 ({iteration}/{self.rules.max_iterations})，转人工处理")

        return DecisionResult(outcome="PASS" if ok else "FAIL", reasons=reasons)


def _find_evidence(evidence: list[Evidence], command: str) -> Evidence | None:
    """宽容匹配 Verifier 声称的命令与实际执行的证据。

    Verifier 常把验证链路写成一个复合命令（a && b && c），
    而实际执行是分步 Tool 调用、逐条记录。规则：
    1. 归一化（去空白/注释）后精确匹配；
    2. 复合命令拆分片段，单条证据覆盖全部片段才算命中；
    3. 任一证据与整条命令互为子串也算命中。
    """
    import re

    target = _norm(command)
    if not target:
        return None

    for ev in evidence:
        if _norm(ev.command or "") == target:
            return ev

    parts = [_norm(p) for p in re.split(r"&&|\|\||;|\n", _strip_notes(command)) if _norm(p)]
    if len(parts) > 1:
        for ev in evidence:
            n = _norm(ev.command or "")
            if n and all(p in n for p in parts):
                return ev
        for ev in evidence:
            n = _norm(ev.command or "")
            if n and n in parts:
                return ev if ev.exit_code == 0 else None

    for ev in evidence:
        n = _norm(ev.command or "")
        if n and (target in n or n in target):
            return ev
    return None


def _norm(s: str) -> str:
    import re

    return re.sub(r"\s+", "", _strip_notes(s))


def _strip_notes(s: str) -> str:
    """去掉命令里的中文/英文括号注释。"""
    import re

    return re.sub(r"（[^）]*）|\([^)]*\)", "", s)


def parse_verdict(content: str) -> Verdict | None:
    """从 Verifier 的最终消息中解析 Verdict JSON。

    容错：去除代码围栏与前后杂文，取第一个 { ... } 片段。
    """
    import json

    start = content.find("{")
    end = content.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        data: dict[str, Any] = json.loads(content[start : end + 1])
    except json.JSONDecodeError:
        return None
    try:
        return Verdict.model_validate(data)
    except Exception:
        return None
