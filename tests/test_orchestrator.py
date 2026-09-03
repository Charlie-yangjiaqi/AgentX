"""P5 测试：Decision Engine + Orchestrator 全链路闭环。

用 MockProvider 脚本化三个 Agent 的行为，验证：
Executor 做 → Reviewer 找问题 → Verifier 找证据 → 决策 → 修复 → 最终 COMPLETED。
"""

from __future__ import annotations

import uuid

import pytest

from agentx.agents.definitions import AgentDefinition
from agentx.agents.prompts import ROLE_PROMPTS
from agentx.agents.runtime import AgentRuntime
from agentx.core.decision import DecisionEngine, DecisionRules, Verdict, parse_verdict
from agentx.core.event_bus import EventBus
from agentx.core.orchestrator import Orchestrator
from agentx.providers.messages import ModelResponse, ToolCall
from agentx.providers.mock import MockProvider, text_response
from agentx.state.models import (
    EVENT_FINDING_CREATED,
    EVENT_TASK_STATE_CHANGED,
    AgentRole,
    AgentSession,
    FindingSeverity,
    Project,
    Task,
    TaskState,
)
from agentx.state.store import SQLiteStore
from agentx.tools import build_default_registry
from agentx.tools.base import ToolContext


def _new_id() -> str:
    return uuid.uuid4().hex[:12]


@pytest.fixture()
def store(tmp_path) -> SQLiteStore:
    s = SQLiteStore(tmp_path / "agentx.db")
    s.open()
    yield s
    s.close()


@pytest.fixture()
def project(tmp_path) -> Project:
    return Project(id="proj1", root_path=str(tmp_path))


def _tool_call(name: str, **args) -> ToolCall:
    return ToolCall(id=_new_id(), name=name, arguments=args)


def _build_orchestrator(
    store: SQLiteStore, ctx: ToolContext
) -> tuple[Orchestrator, dict[str, MockProvider]]:
    bus = EventBus()
    store_events: list = []
    bus.subscribe(lambda e: store_events.append(e))
    registry = build_default_registry(bus)

    providers: dict[str, MockProvider] = {}
    runtimes: dict[str, AgentRuntime] = {}
    for role in ("executor", "reviewer", "verifier"):
        defn = AgentDefinition.from_role(
            f"{role}-1", AgentRole(role), ROLE_PROMPTS[role], model="mock"
        )
        provider = MockProvider(name=role)
        providers[role] = provider
        runtimes[role] = AgentRuntime(defn, provider, registry, bus)
        store.insert_agent(
            AgentSession(
                id=defn.id, role=defn.role, provider_ref=defn.provider_ref, model=defn.model
            )
        )
    return (
        Orchestrator(
            store=store,
            registry=registry,
            decision_engine=DecisionEngine(DecisionRules(max_iterations=3)),
            agents=runtimes,
            event_bus=bus,
        ),
        providers,
    )


# ---------- Decision Engine ----------


def _evidence(task_id: str, command: str, exit_code: int):
    from agentx.state.models import Evidence

    return Evidence(
        id=_new_id(), task_id=task_id, type="test.run", command=command, exit_code=exit_code
    )


def test_decision_pass_when_all_conditions_met() -> None:
    engine = DecisionEngine()
    verdict = Verdict(
        build={"command": "make", "required": True},
        tests=[{"command": "pytest", "required": True}],
        conclusion="PASS",
    )
    result = engine.decide(
        findings=[],
        evidence=[
            _evidence("t", "make", 0),
            _evidence("t", "pytest", 0),
        ],
        verdict=verdict,
        iteration=0,
    )
    assert result.is_pass


def test_decision_fail_on_high_finding() -> None:
    engine = DecisionEngine()
    from agentx.state.models import Finding

    finding = Finding(
        id="f1",
        task_id="t",
        severity=FindingSeverity.HIGH,
        category="回归",
        description="rollback 未恢复",
    )
    result = engine.decide(
        findings=[finding],
        evidence=[_evidence("t", "make", 0), _evidence("t", "pytest", 0)],
        verdict=Verdict(
            build={"command": "make"}, tests=[{"command": "pytest"}], conclusion="PASS"
        ),
        iteration=0,
    )
    assert not result.is_pass
    assert any("BLOCKER/HIGH" in r for r in result.reasons)


def test_decision_fail_missing_evidence() -> None:
    engine = DecisionEngine()
    result = engine.decide(
        findings=[],
        evidence=[],
        verdict=Verdict(
            build={"command": "make"}, tests=[{"command": "pytest"}], conclusion="PASS"
        ),
        iteration=0,
    )
    assert not result.is_pass
    assert any("make" in r for r in result.reasons)


def test_decision_fail_iteration_limit() -> None:
    engine = DecisionEngine(DecisionRules(max_iterations=2))
    result = engine.decide(
        findings=[],
        evidence=[],
        verdict=Verdict(),
        iteration=2,
    )
    assert not result.is_pass
    assert any("迭代" in r for r in result.reasons)


def test_parse_verdict_with_fences() -> None:
    content = (
        "好的，验证完毕。\n"
        "```json\n"
        '{"build": {"command": "make"}, "tests": [{"command": "pytest"}], '
        '"conclusion": "PASS", "notes": "ok"}\n'
        "```"
    )
    verdict = parse_verdict(content)
    assert verdict is not None
    assert verdict.build is not None
    assert verdict.build.command == "make"
    assert verdict.conclusion == "PASS"


def test_parse_verdict_invalid() -> None:
    assert parse_verdict("什么都没验证") is None


# ---------- 全链路闭环 ----------


async def test_full_loop_success(store: SQLiteStore, project: Project, tmp_path) -> None:
    store.insert_project(project)
    (tmp_path / "main.c").write_text("int main() { return 0; }\n", encoding="utf-8")
    task = Task(id="t1", project_id=project.id, goal="实现参数事务功能")
    store.insert_task(task)

    ctx = ToolContext(project_root=tmp_path)
    orchestrator, providers = _build_orchestrator(store, ctx)

    providers["executor"].respond(
        ModelResponse(
            tool_calls=[_tool_call("fs.write_file", path="param.c", content="int tx = 0;\n")]
        ),
        text_response("已实现 param.c"),
    )
    providers["reviewer"].respond(
        text_response(
            '{"findings": [{"severity": "MEDIUM", "category": "质量", '
            '"location": "param.c", "description": "缺少注释"}]}'
        )
    )
    providers["verifier"].respond(
        ModelResponse(tool_calls=[_tool_call("test.run", command="echo build-ok")]),
        text_response('{"build": {"command": "echo build-ok"}, "tests": [], "conclusion": "PASS"}'),
    )

    result = await orchestrator.run_task("t1")

    assert result.state == TaskState.COMPLETED
    assert (tmp_path / "param.c").exists()

    # 状态机完整走过
    states = [
        e.payload["to"] for e in store.list_events("t1") if e.type == EVENT_TASK_STATE_CHANGED
    ]
    assert states == ["EXECUTING", "REVIEWING", "VERIFYING", "DECIDING", "COMPLETED"]

    # Finding / Evidence / Decision / Change 全部落库
    findings = store.list_findings("t1")
    assert len(findings) == 1
    assert findings[0].severity == FindingSeverity.MEDIUM

    evidence = store.list_evidence("t1")
    assert len(evidence) == 1
    assert evidence[0].exit_code == 0

    decisions = store.list_decisions("t1")
    assert len(decisions) == 1
    assert decisions[0].result == "PASS"

    changes = store.list_changes("t1")
    assert len(changes) == 1
    assert changes[0].file == "param.c"

    # 事件可见
    event_types = {e.type for e in store.list_events("t1")}
    assert EVENT_FINDING_CREATED in event_types


async def test_full_loop_repair_then_pass(store: SQLiteStore, project: Project, tmp_path) -> None:
    """第一轮 FAIL（HIGH finding + 测试失败）→ REPAIRING → 第二轮 PASS。"""
    store.insert_project(project)
    (tmp_path / "main.c").write_text("int main() { return 0; }\n", encoding="utf-8")
    task = Task(id="t2", project_id=project.id, goal="实现参数事务功能")
    store.insert_task(task)

    ctx = ToolContext(project_root=tmp_path)
    orchestrator, providers = _build_orchestrator(store, ctx)

    # 第一轮：executor 写文件，reviewer 报 HIGH，verifier 测试失败
    providers["executor"].respond(
        ModelResponse(
            tool_calls=[_tool_call("fs.write_file", path="param.c", content="int tx = 0;\n")]
        ),
        text_response("第一轮完成"),
    )
    providers["reviewer"].respond(
        text_response(
            '{"findings": [{"severity": "HIGH", "category": "回归", '
            '"location": "param.c", "description": "缺少回滚处理"}]}'
        )
    )
    providers["verifier"].respond(
        ModelResponse(tool_calls=[_tool_call("test.run", command="exit 1")]),
        text_response('{"build": {"command": "exit 1"}, "tests": [], "conclusion": "FAIL"}'),
    )
    # 第二轮：executor 修复，reviewer 无问题，verifier 通过
    providers["executor"].respond(
        ModelResponse(
            tool_calls=[
                _tool_call(
                    "fs.write_file",
                    path="param.c",
                    content="int tx = 0; void rollback() {}\n",
                )
            ]
        ),
        text_response("已修复回滚"),
    )
    providers["reviewer"].respond(text_response('{"findings": []}'))
    providers["verifier"].respond(
        ModelResponse(tool_calls=[_tool_call("test.run", command="exit 0")]),
        text_response('{"build": {"command": "exit 0"}, "tests": [], "conclusion": "PASS"}'),
    )

    result = await orchestrator.run_task("t2")

    assert result.state == TaskState.COMPLETED
    assert result.iteration == 1

    states = [
        e.payload["to"] for e in store.list_events("t2") if e.type == EVENT_TASK_STATE_CHANGED
    ]
    assert "REPAIRING" in states
    assert states.count("EXECUTING") == 2

    decisions = store.list_decisions("t2")
    assert len(decisions) == 2
    assert decisions[0].result == "FAIL"
    assert decisions[1].result == "PASS"


async def test_full_loop_waits_for_user_on_high_risk(
    store: SQLiteStore, project: Project, tmp_path
) -> None:
    """Executor 触发高风险命令 → WAITING_USER，resume 后继续。"""
    store.insert_project(project)
    task = Task(id="t3", project_id=project.id, goal="清理临时文件")
    store.insert_task(task)

    ctx = ToolContext(project_root=tmp_path)
    orchestrator, providers = _build_orchestrator(store, ctx)

    providers["executor"].respond(
        ModelResponse(tool_calls=[_tool_call("shell.run", command="rm -rf /tmp/cache")]),
    )

    result = await orchestrator.run_task("t3")
    assert result.state == TaskState.WAITING_USER

    # 用户审批后恢复
    providers["executor"].respond(text_response("已完成清理"))
    providers["reviewer"].respond(text_response('{"findings": []}'))
    providers["verifier"].respond(
        text_response('{"build": null, "tests": [], "conclusion": "PASS"}')
    )

    result2 = await orchestrator.resume("t3")
    assert result2.state == TaskState.COMPLETED


async def test_full_loop_fails_after_max_iterations(
    store: SQLiteStore, project: Project, tmp_path
) -> None:
    """一直 FAIL → 迭代超限 → FAILED。"""
    store.insert_project(project)
    task = Task(id="t4", project_id=project.id, goal="不可能完成的任务", max_iterations=2)
    store.insert_task(task)

    ctx = ToolContext(project_root=tmp_path)
    orchestrator, providers = _build_orchestrator(store, ctx)

    for _ in range(4):
        providers["executor"].respond(text_response("完成"))
        providers["reviewer"].respond(
            text_response(
                '{"findings": [{"severity": "HIGH", "category": "需求", '
                '"description": "未满足需求"}]}'
            )
        )
        providers["verifier"].respond(text_response('{"conclusion": "FAIL", "notes": "验证不过"}'))

    result = await orchestrator.run_task("t4")
    assert result.state == TaskState.FAILED
