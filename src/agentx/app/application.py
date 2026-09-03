"""Application 层：CLI / TUI / 未来 IDE 共用的装配与业务入口。

原则：上层只依赖下层契约。CLI 不直接碰 Provider / Store，
一切通过 Application 的高层操作完成。
"""

from __future__ import annotations

import uuid
from pathlib import Path

from agentx.agents.definitions import AgentDefinition
from agentx.agents.prompts import ROLE_PROMPTS
from agentx.agents.runtime import AgentRuntime
from agentx.config.config import (
    AgentXConfig,
    resolve_agent_model,
    resolve_agent_provider_cfg,
    resolve_model_source,
    resolve_no_proxy,
    resolve_permission_mode,
)
from agentx.config.llm import resolve_llm
from agentx.core.control import TaskControl
from agentx.core.decision import DecisionEngine, DecisionRules
from agentx.core.event_bus import EventBus
from agentx.core.orchestrator import Orchestrator
from agentx.providers.fallback import FallbackProvider
from agentx.providers.mock import MockProvider
from agentx.providers.openai import OpenAIProvider
from agentx.providers.reasonix import ReasonixProvider
from agentx.state.models import AgentRole, AgentSession, Project, Task, TaskState
from agentx.state.store import SQLiteStore
from agentx.tools import build_default_registry


class Application:
    """AgentX 单项目应用：装配所有组件并提供高层操作。"""

    def __init__(
        self,
        project_root: Path,
        *,
        db_path: Path | None = None,
        config: AgentXConfig | None = None,
        api_key: str | None = None,
        base_url: str | None = None,
        model: str | None = None,
        no_proxy: bool | None = None,
        permission_mode: str | None = None,
        max_iterations: int = 5,
    ) -> None:
        self.project_root = project_root.resolve()
        self._db_path = db_path or self.project_root / ".agentx" / "agentx.db"
        cfg = config or AgentXConfig()
        self.config = cfg

        self.store = SQLiteStore(self._db_path)
        self.store.open()
        self.event_bus = EventBus()
        self.registry = build_default_registry(self.event_bus)

        self.no_proxy = resolve_no_proxy(cfg) if no_proxy is None else no_proxy
        self.permission_mode = resolve_permission_mode(cfg, permission_mode)
        self.model_source = resolve_model_source(cfg)
        # 唯一配置入口：resolve_llm（env > ~/.agentx/.env > config.json > 预设）。
        # 显式构造参数（CLI --model 等）作为 override；默认不传则由 resolve_llm 全权决定。
        llm = resolve_llm(cfg)
        if api_key is not None:
            llm = {**llm, "api_key": api_key, "key_source": "override"}
        if base_url is not None:
            llm = {**llm, "base_url": base_url}
        if model is not None:
            llm = {**llm, "model": model}
        self.llm_config = llm  # ResolvedLLMConfig：provider/model/base_url/api_key/api_key_env
        self.api_key = llm["api_key"]  # 兼容属性（resolved 值）
        self.base_url = llm["base_url"]
        self.model = llm["model"]
        self.provider_name, self.provider = self._build_provider(cfg, llm)
        self.control = TaskControl()

        agents: dict[str, AgentRuntime] = {}
        for role in ("plan", "executor", "reviewer", "verifier"):
            role_provider_ref, role_model = resolve_agent_model(cfg, role)
            role_key, role_base_url, _ = resolve_agent_provider_cfg(cfg, role)
            role_provider = self.provider
            if role_key:
                # 独立 Key 的角色使用独立 Provider（plan 用 Key A、review 用 Key B）
                role_provider = OpenAIProvider(
                    api_key=role_key,
                    base_url=role_base_url or self.base_url,
                    trust_env=not self.no_proxy,
                    generation=cfg.generation or None,
                )
            elif role_provider_ref and role_provider_ref != self.provider_name:
                raise ValueError(f"Agent 角色 {role} 配置了未注册的 Provider: {role_provider_ref}")
            defn = AgentDefinition.from_role(
                f"{role}-1",
                AgentRole(role),
                ROLE_PROMPTS[role],
                model=role_model or self.model,
            )
            agents[role] = AgentRuntime(
                defn,
                role_provider,
                self.registry,
                self.event_bus,
                auto_reject_high_risk=self.permission_mode == "auto",
                stop_check=lambda: self.control.is_cancelled(self._active_task_id or ""),
            )
            self.store.insert_agent(
                AgentSession(
                    id=defn.id, role=defn.role, provider_ref=self.provider_name, model=defn.model
                )
            )

        self.orchestrator = Orchestrator(
            store=self.store,
            registry=self.registry,
            decision_engine=DecisionEngine(DecisionRules(max_iterations=max_iterations)),
            agents=agents,
            event_bus=self.event_bus,
            control=self.control,
        )
        self._active_task_id: str | None = None

    def _build_provider(self, cfg: AgentXConfig, llm: dict[str, object]) -> tuple[str, object]:
        """Provider 工厂：由 resolve_llm 的 ResolvedLLMConfig 驱动，不自行解析 key。

        OpenAI Compatible 类型（OpenAI / DeepSeek / vLLM / 本地网关）统一复用
        OpenAIProvider（UA / timeout / retry / 错误分类全部复用）。
        """
        if self.model_source == "reasonix":
            provider: object = ReasonixProvider()
            name = "reasonix"
        elif llm.get("api_key") and llm.get("base_url"):
            provider = OpenAIProvider(
                api_key=str(llm["api_key"]),
                base_url=str(llm["base_url"]),
                trust_env=not self.no_proxy,
                generation=cfg.generation or None,
            )
            name = str(llm.get("provider") or "openai")
        else:
            return "mock", MockProvider()
        if cfg.fallback_provider:
            fallback_name = cfg.fallback_provider
            if fallback_name == "mock":
                provider = FallbackProvider(provider, MockProvider(name="fallback-mock"))
                name = f"{name}+{fallback_name}"
        return name, provider

    # ---------- 项目与任务 ----------

    def ensure_project(self, branch: str | None = None) -> Project:
        """init：确保 .agentx 工作区存在，返回项目记录。"""
        self.project_root.mkdir(parents=True, exist_ok=True)
        (self.project_root / ".agentx").mkdir(exist_ok=True)
        for project in self.store.list_projects():
            if project.root_path == str(self.project_root):
                return project
        project = Project(id=uuid.uuid4().hex[:12], root_path=str(self.project_root), branch=branch)
        self.store.insert_project(project)
        return project

    def create_task(self, goal: str, *, max_iterations: int = 5) -> Task:
        project = self.ensure_project()
        task = Task(
            id=uuid.uuid4().hex[:12],
            project_id=project.id,
            goal=goal,
            max_iterations=max_iterations,
        )
        self.store.insert_task(task)
        return task

    # ---------- 任务控制 ----------

    async def run(self, task_id: str) -> Task:
        self._active_task_id = task_id
        try:
            return await self.orchestrator.run_task(task_id)
        finally:
            self._active_task_id = None
            self.control.clear(task_id)

    async def resume(self, task_id: str) -> Task:
        self.control.clear(task_id)
        self._active_task_id = task_id
        try:
            return await self.orchestrator.resume(task_id)
        finally:
            self._active_task_id = None
            self.control.clear(task_id)

    def cancel(self, task_id: str) -> Task | None:
        task = self.store.get_task(task_id)
        if task is None:
            return None
        if not task.state.is_terminal and task.state != TaskState.CANCELLED:
            self.control.cancel(task_id)
            return self.store.update_task_state(task_id, TaskState.CANCELLED)
        return task

    def latest_task(self) -> Task | None:
        tasks = self.store.list_tasks()
        return tasks[-1] if tasks else None

    def list_tasks(self) -> list[Task]:
        return self.store.list_tasks()

    # ---------- 轻量单阶段操作（MCP 用） ----------

    async def review_project(self, goal: str) -> dict[str, object]:
        """只做审查：Reviewer 独立分析当前项目（不修改），返回 findings。

        供 MCP 的 agentx_review 调用：不入完整状态机，轻量单次审查。
        """
        from agentx.core.orchestrator import _env_hint
        from agentx.providers.messages import ChatMessage

        runtime = self.orchestrator.agents["reviewer"]
        ctx = self.orchestrator._ctx(self._dummy_task())
        messages = [
            ChatMessage(role="user", content=f"审查目标：{goal}"),
            ChatMessage(role="user", content=_env_hint()),
            ChatMessage(
                role="user",
                content=(
                    "请审查当前项目代码：需求符合度、架构一致性、代码质量、回归风险。"
                    "输出 findings JSON。"
                ),
            ),
        ]
        result = await runtime.run(messages, ctx)
        from agentx.core.orchestrator import _parse_findings

        findings = _parse_findings(result.content or "")
        return {
            "agent": result.agent_id,
            "summary": result.content or "",
            "findings": findings,
        }

    async def verify_project(self, goal: str) -> dict[str, object]:
        """只做验证：Verifier 用真实工具检查当前项目，返回证据与结论。

        供 MCP 的 agentx_verify 调用：不入完整状态机，轻量单次验证。
        """
        from agentx.core.decision import parse_verdict
        from agentx.core.orchestrator import _env_hint
        from agentx.providers.messages import ChatMessage

        runtime = self.orchestrator.agents["verifier"]
        ctx = self.orchestrator._ctx(self._dummy_task())
        messages = [
            ChatMessage(role="user", content=f"验证目标：{goal}"),
            ChatMessage(role="user", content=_env_hint()),
            ChatMessage(
                role="user",
                content=(
                    "请用真实工具（构建/测试）验证当前项目状态，"
                    "输出 verdict JSON（build / tests / conclusion）。"
                ),
            ),
        ]
        result = await runtime.run(messages, ctx)
        verdict = parse_verdict(result.content or "")
        evidence = [
            {
                "type": e.type,
                "command": e.command,
                "exit_code": e.exit_code,
            }
            for e in result.evidence
        ]
        return {
            "agent": result.agent_id,
            "summary": result.content or "",
            "verdict": verdict.model_dump() if verdict else None,
            "evidence": evidence,
        }

    def _dummy_task(self) -> Task:
        """轻量单阶段操作用的最小 Task（提供 project 关联）。"""
        project = self.ensure_project()
        return Task(id="_standalone", project_id=project.id, goal="_standalone")
