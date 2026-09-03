"""AgentX CLI：快速启动任务、脚本化运行、CI 集成、故障排查。

CLI 只做参数解析与展示，业务逻辑全部调用 Application Service。
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from typing import Any

import typer

from agentx.app.application import Application
from agentx.config.config import (
    AgentModelConfig,
    default_config_path,
    load_config,
    save_config,
)
from agentx.state.models import StoredEvent, Task, TaskState

app = typer.Typer(
    help="AgentX: Human-in-the-loop Multi-Agent 协作引擎",
    no_args_is_help=False,
)


@app.callback(invoke_without_command=True)
def _main_callback(
    ctx: typer.Context,
    path: Path = typer.Option(".", "--path", "-p", help="项目目录（进入 TUI 时的工作区）"),
) -> None:
    """无参数运行 agentx 时启动 TUI 工作台。"""
    if ctx.invoked_subcommand is None:
        from agentx.tui.app import AgentXApp

        AgentXApp(path).run()


def _print_workflow_events(collector: object) -> None:
    """打印 workflow 事件（GBK 安全）。"""
    from agentx.runtime.events import EventCollector

    if not isinstance(collector, EventCollector) or len(collector.events()) == 0:
        return
    typer.echo("Workflow:")
    for e in collector.events():
        msg = e.get("message", "")
        suffix = f" | {msg}" if msg else ""
        typer.echo(f"  [{e['status']:>9}] {e['stage']}{suffix}")


_STATUS_ICON = {
    "pending": "[.. ]",
    "running": "[RUN]",
    "completed": "[DONE]",
    "failed": "[FAIL]",
}


def _print_live_event(counter: dict[str, Any], event: dict[str, str]) -> None:
    """实时事件行（Phase 6.6）：ASCII 安全，无颜色，PowerShell 兼容。

    counter：共享 {"n": 序号, "stage": 当前序号归属的 stage}；
    同一 stage 的 running/completed 共享一个序号；无 running 直发 completed 递增。
    """
    stage = event.get("stage", "")
    status = event.get("status", "")
    icon = _STATUS_ICON.get(status, "[?? ]")
    if status == "running":
        counter["n"] += 1
        counter["stage"] = stage
        n = counter["n"]
        typer.echo(f"[{n:>2}] {stage:<18} {icon} {event.get('message', '')}")
    else:
        if counter.get("stage") != stage:
            counter["n"] += 1
            counter["stage"] = stage
        n = counter["n"]
        msg = event.get("message", "")
        if status == "completed":
            typer.echo(f"[{n:>2}] {stage:<18} [DONE] {msg}")
        elif status == "failed":
            typer.echo(f"[{n:>2}] {stage:<18} [FAIL] {msg}")
        else:
            typer.echo(f"[{n:>2}] {stage:<18} {icon} {msg}")


def _make_live_printer() -> tuple[Any, Any]:
    """返回 (listener, heartbeat_printer)：CLI 实时显示 workflow 事件。"""
    counter: dict[str, Any] = {"n": 0, "stage": ""}

    def _listener(event: dict[str, str]) -> None:
        _print_live_event(counter, event)

    def _heartbeat(beat: dict[str, object]) -> None:
        stage = str(beat.get("stage", ""))
        elapsed = beat.get("elapsed", 0)
        typer.echo(f"      {stage:<18} [RUN] still running ({elapsed}s)")

    return _listener, _heartbeat


def _print_project_status(root: Path) -> None:
    """Project Status 区块：Index / Fingerprint / Last Sync / Understanding。"""
    from agentx.index.index import index_status, load_index

    status, reason = index_status(root)
    index = load_index(root)
    typer.echo("Project Status:")
    typer.echo("  Index:")
    typer.echo(f"    State: {status.value}")
    if index is not None:
        typer.echo(f"    Fingerprint: {index.project_fingerprint[:12]}")
        typer.echo(f"    Last Sync: {str(index.generated_at)[:10]}")
        understanding = index.project_understanding or {}
        if understanding:
            typer.echo(f"    Understanding: {understanding.get('status', 'valid')}")
        else:
            typer.echo("    Understanding: none")
    else:
        typer.echo("    Fingerprint: (no index)")
        typer.echo("    Last Sync: (never)")
        typer.echo("    Understanding: none")
    typer.echo("  Workflow: idle")


def _app(
    path: Path | None,
    *,
    api_key: str | None = None,
    model: str | None = None,
    permission_mode: str | None = None,
) -> Application:
    cfg = load_config()
    root = (path or Path.cwd()).resolve()
    return Application(
        root,
        config=cfg,
        api_key=api_key,
        model=model,
        permission_mode=permission_mode,
    )


# ---------- 配置 ----------


@app.command("setup")
def setup(
    config_path: Path = typer.Option(
        None, "--config", help="配置文件路径（默认 ~/.agentx/config.json）"
    ),
) -> None:
    """交互式配置向导：API Key / 模型 / 直连 / 权限模式。"""
    cfg = load_config(config_path)
    cfg_path = config_path or default_config_path()
    typer.echo(f"AgentX setup — 配置将保存到 {cfg_path}")

    existing = typer.prompt(
        f"检测到已有配置（model={cfg.model}）。重新配置？", default=True, type=bool
    )
    if not existing:
        typer.echo("保留现有配置。")
        return

    use_key = typer.prompt("配置 API Key？（输入 sk- 开头的 Key，或留空跳过）", default="")
    if use_key:
        cfg.api_key = use_key.strip()
    else:
        cfg.api_key = None

    cfg.base_url = typer.prompt(
        "API Base URL", default=cfg.base_url or "https://api.deepseek.com/v1"
    ).strip()
    cfg.model = typer.prompt("模型", default=cfg.model or "deepseek-v4-flash").strip()
    cfg.no_proxy = typer.prompt("绕过系统代理直连？", default=cfg.no_proxy, type=bool)
    cfg.permission_mode = (
        typer.prompt(
            "权限模式（review=高风险需审批 / auto=自动拒绝高风险）",
            default=cfg.permission_mode or "review",
        )
        .strip()
        .lower()
    )
    if cfg.permission_mode not in {"review", "auto"}:
        cfg.permission_mode = "review"

    # 角色级独立 Key / 模型配置
    for role in ("plan", "review", "verify"):
        agent_cfg = cfg.agents.get(role)
        current = (
            f"（当前: model={agent_cfg.model}, key={_mask_key(agent_cfg.api_key)}）"
            if agent_cfg and (agent_cfg.model or agent_cfg.api_key)
            else "（未单独配置，使用全局）"
        )
        use_own = typer.prompt(
            f"为 {role} 角色配置独立 API Key / 模型？{current}", default=False, type=bool
        )
        if not use_own:
            if role in cfg.agents:
                cfg.agents.pop(role)
            continue
        role_key = typer.prompt("独立 API Key（留空 = 用全局 Key）", default="").strip()
        role_model = typer.prompt("独立模型（留空 = 用全局模型）", default="").strip()
        cfg.agents[role] = AgentModelConfig(
            api_key=role_key or None,
            model=role_model or None,
        )

    save_config(cfg, config_path)
    typer.echo("已保存。环境变量 OPENAI_API_KEY / OPENAI_BASE_URL 会覆盖配置文件。")


# ---------- 配置（config / config api / config api test） ----------

config_app = typer.Typer(help="查看/配置 AgentX（LLM Provider 等）", no_args_is_help=False)
api_app = typer.Typer(help="LLM Provider 配置与测试", no_args_is_help=False)
config_app.add_typer(api_app, name="api")
app.add_typer(config_app, name="config")


@config_app.callback(invoke_without_command=True)
def _config_callback(ctx: typer.Context) -> None:
    """无子命令时显示当前配置。"""
    if ctx.invoked_subcommand is None:
        show_config()


def show_config() -> None:
    """查看当前生效的角色配置（Key/模型/直连/LLM Provider）。"""
    cfg = load_config()
    from agentx.config.llm import resolve_llm

    llm = resolve_llm(cfg)
    typer.echo("LLM Provider:")
    typer.echo(f"  provider     : {llm['provider_name']} ({llm['provider']})")
    typer.echo(f"  model        : {llm['model']}")
    typer.echo(f"  base_url     : {llm['base_url'] or '(未配置)'}")
    typer.echo(f"  api_key      : {_mask_key(llm['api_key'])}")
    typer.echo(f"  key_source   : {llm['key_source'] or '(未配置)'}")
    typer.echo(f"  configured   : {'yes' if llm['configured'] else 'no'}")
    if not llm["configured"]:
        typer.echo("  提示: 运行 `agentx config api` 配置，或 `agentx config api test` 测试")
    typer.echo("")
    typer.echo(f"model_source : {cfg.model_source}")
    typer.echo(f"全局 API Key : {_mask_key(cfg.api_key)}")
    typer.echo(f"全局 Base URL: {cfg.base_url}")
    typer.echo(f"全局 Model   : {cfg.model}")
    typer.echo(f"直连(no_proxy): {cfg.no_proxy}")
    typer.echo(f"权限模式     : {cfg.permission_mode}")
    typer.echo("\n角色配置：")
    for role in ("plan", "review", "verify"):
        ac = cfg.agents.get(role)
        if ac is None:
            typer.echo(f"  {role:8s}: 使用全局（key={_mask_key(cfg.api_key)} model={cfg.model}）")
        else:
            typer.echo(
                f"  {role:8s}: key={_mask_key(ac.api_key) or _mask_key(cfg.api_key)}"
                f" model={ac.model or cfg.model}"
            )


@api_app.callback(invoke_without_command=True)
def _api_callback(ctx: typer.Context) -> None:
    """无子命令时进入交互配置向导。"""
    if ctx.invoked_subcommand is None:
        config_api()


@api_app.command("setup")
def config_api() -> None:
    """配置 LLM Provider（交互向导；API Key 存 ~/.agentx/.env，不入 config.json）。"""
    from agentx.config.config import save_config
    from agentx.config.llm import (
        PROVIDER_NAMES,
        PROVIDER_PRESETS,
        resolve_llm,
        write_secret_env,
    )

    cfg = load_config()
    current = resolve_llm(cfg)

    typer.echo("AgentX AI Provider Setup")
    typer.echo("")
    typer.echo("Choose provider:")
    names = list(PROVIDER_NAMES)
    for i, name in enumerate(names, 1):
        mark = " (current)" if name == current["provider"] else ""
        typer.echo(f"  {i}. {PROVIDER_NAMES[name]}{mark}")
    choice = typer.prompt("Select", default=str(names.index(current["provider"]) + 1))
    try:
        provider = names[int(choice) - 1]
    except (ValueError, IndexError):
        typer.echo(f"[ERROR] 无效选择: {choice}")
        raise typer.Exit(code=1) from None

    preset = PROVIDER_PRESETS[provider]
    explicit = dict(cfg.llm or {})
    # 切换 provider 时使用新 provider 的预设（env/base_url/model），
    # 同一 provider 重新配置时保留已有值
    default_env = explicit.get("api_key_env") or preset["api_key_env"]
    if provider == explicit.get("provider"):
        default_base = explicit.get("base_url") or preset["base_url"]
        default_model = explicit.get("model") or preset["model"]
    else:
        default_base = preset["base_url"]
        default_model = preset["model"]
    typer.echo(f"\nProvider: {PROVIDER_NAMES[provider]}")

    key = typer.prompt("API Key（输入则写入 ~/.agentx/.env；回车=保留现有）", default="")
    key = key.strip()
    if key:
        write_secret_env({default_env: key})
        typer.echo(f"  [OK] API Key 已保存到 ~/.agentx/.env（{default_env}），未写入 config.json")

    if provider == "compatible":
        base_url = typer.prompt("Base URL（如 https://xxx/v1）", default=default_base or "")
        base_url = base_url.strip()
        if not base_url:
            typer.echo("[ERROR] OpenAI Compatible API 必须提供 Base URL")
            raise typer.Exit(code=1) from None
    else:
        base_url = typer.prompt(
            "Base URL（回车=使用预设）",
            default=default_base,
        ).strip()
        base_url = base_url or preset["base_url"]

    model = typer.prompt("Model", default=default_model).strip()
    if not model:
        model = preset["model"]

    cfg.llm = {
        "provider": provider,
        "model": model,
        "base_url": base_url,
        "api_key_env": default_env,
    }
    save_config(cfg)
    typer.echo("")
    typer.echo(f"[OK] 已保存 provider={provider} model={model} base_url={base_url}")
    typer.echo("验证连通性: `agentx config api test`")


@api_app.command("test")
def config_api_test() -> None:
    """测试 LLM API 连通性（GET /models，OpenAI Compatible 协议）。"""
    from agentx.config.llm import resolve_llm, test_llm_connection

    resolved = resolve_llm()
    typer.echo(f"Provider: {resolved['provider_name']} ({resolved['provider']})")
    typer.echo(f"Model   : {resolved['model']}")
    typer.echo(f"Endpoint: {resolved['base_url'] or '(未配置)'}")
    typer.echo(f"API Key : {_mask_key(resolved['api_key'])} ({resolved['key_source'] or '缺失'})")
    typer.echo("")
    typer.echo("Testing /models ...")
    result = test_llm_connection(resolved)
    if result["ok"]:
        typer.echo(f"Status : OK ({result['status']})")
        typer.echo(f"Latency: {result['latency_ms']} ms")
        if result["detail"] != "OK":
            typer.echo(f"Note   : {result['detail']}")
    else:
        typer.echo("Status : FAIL")
        typer.echo(f"Reason : {result['detail']}")
        raise typer.Exit(code=1) from None


def _mask_key(key: str | None) -> str:
    if not key:
        return "(未配置)"
    if len(key) <= 8:
        return "****"
    return f"{key[:6]}...{key[-4:]}"


def _run_scope_wizard(root: Path, *, yes: bool = False) -> str:
    """首次建立 Index 前的 Scope 引导（Phase 7.8 引导层，复用 ScopeInitializer）。

    返回："written"（已生成配置）| "cancelled"（全部取消）| ""（已配置/无建议，跳过）。
    """
    from agentx.scope.config import SCOPE_CONFIG_FILENAME
    from agentx.scope.initializer import apply_scope_selections, check_scope_init

    gate = check_scope_init(root)
    if gate is None:
        return ""
    detail_ignore = gate["detail"]["ignore"]
    detail_third = gate["detail"]["third_party"]

    typer.echo("首次初始化：未发现 .agentxscope.yaml，检测到以下范围建议：")
    if detail_ignore:
        typer.echo("Ignore 建议（完全不进入 Index）：")
        for s in detail_ignore:
            typer.echo(f"  - {s['path']}: {s['reason']}")
    if detail_third:
        typer.echo("Third_party 建议（保留 API/调用关系，不拆业务模块）：")
        for s in detail_third:
            typer.echo(f"  - {s['path']}: {s['reason']}")

    def _decide(label: str, path: str) -> bool:
        answer = typer.prompt(f"采纳 {label} {path}？[Y/n]", default="y").strip()
        return answer.casefold() not in ("n", "no")

    if yes:
        selections = {
            "ignore": [s["path"] for s in detail_ignore],
            "third_party": [s["path"] for s in detail_third],
        }
    else:
        chosen_ignore = [s["path"] for s in detail_ignore if _decide("ignore", s["path"])]
        chosen_third = [s["path"] for s in detail_third if _decide("third_party", s["path"])]
        extra_ignore = _ask_extra("Ignore（逗号分隔，回车跳过）")
        extra_third = _ask_extra("Third_party（逗号分隔，回车跳过）")
        if not chosen_ignore and not chosen_third and not extra_ignore and not extra_third:
            typer.echo("未采纳任何建议（全部按 project 分析，可随时运行 agentx init 重新引导）。")
            return "cancelled"
        selections = {
            "ignore": chosen_ignore + extra_ignore,
            "third_party": chosen_third + extra_third,
        }

    target = apply_scope_selections(root, selections)
    typer.echo(f"已生成 {SCOPE_CONFIG_FILENAME}：")
    for line in target.read_text(encoding="utf-8").splitlines():
        typer.echo(f"  {line}")
    typer.echo(
        "后续 agentx plan 将按三层 Scope 分析（project 完整 / third_party 接口级 / ignore 排除）。"
    )
    return "written"


def _ask_extra(label: str) -> list[str]:
    raw = typer.prompt(f"手动增加 {label}", default="").strip()
    if not raw:
        return []
    return [p.strip() for p in raw.split(",") if p.strip()]


@app.command("doctor")
def doctor(
    path: Path = typer.Option(".", "--path", "-p", help="项目目录"),
) -> None:
    """健康检查：配置 / LLM Provider / Semantic Runtime / 项目状态。"""
    from agentx.config.config import load_config

    cfg = load_config()
    application = _app(path)

    typer.echo("=== LLM Provider ===")
    from agentx.config.llm import resolve_llm, test_llm_connection

    llm = resolve_llm(cfg)
    if not llm["configured"]:
        typer.echo("  [警告] 未配置 LLM Provider（API Key 或 Base URL 缺失）")
        typer.echo("  Run: agentx config api")
    else:
        typer.echo(f"  Provider: {llm['provider_name']} ({llm['provider']})")
        typer.echo(f"  Model   : {llm['model']}")
        typer.echo(f"  Endpoint: {llm['base_url']}")
        typer.echo(f"  API Key : [OK] configured ({llm['key_source']})")
        result = test_llm_connection(llm)
        if result["ok"]:
            typer.echo(f"  API reachable: [OK] ({result['status']}, {result['latency_ms']} ms)")
        else:
            typer.echo(f"  API reachable: [FAIL] {result['detail']}")
    if llm.get("key_source") == "config(legacy)" or cfg.api_key:
        typer.echo(
            "  [提示] Legacy API configuration detected, please migrate to agentx config api"
        )

    typer.echo("\n=== 配置 ===")
    typer.echo(f"  config: {default_config_path()}")
    typer.echo(f"  api_key: {'已配置' if application.api_key else '未配置（将使用 mock）'}")
    typer.echo(f"  base_url: {application.base_url}")
    typer.echo(f"  model: {application.model}")
    typer.echo(f"  直连(no_proxy): {application.no_proxy}")
    typer.echo(f"  权限模式: {application.permission_mode}")

    typer.echo("\n=== API 连通性 ===")
    if not application.api_key:
        typer.echo("  [警告] 未配置 API Key，真实任务无法验证，请运行 agentx setup 配置。")
    else:
        import asyncio as _asyncio

        import httpx

        async def _ping() -> str:
            try:
                from agentx.http import with_default_user_agent

                async with httpx.AsyncClient(
                    base_url=application.base_url,
                    timeout=15.0,
                    trust_env=not application.no_proxy,
                    headers=with_default_user_agent(
                        {"Authorization": f"Bearer {application.api_key}"}
                    ),
                ) as client:
                    resp = await client.get("/models")
                    if resp.status_code == 200:
                        return f"  [OK] {application.base_url}/models → {resp.status_code}"
                    return f"  [FAIL] HTTP {resp.status_code}: {resp.text[:100]}"
            except Exception as e:
                return f"  [FAIL] {type(e).__name__}: {e}"

        typer.echo(_asyncio.run(_ping()))

    typer.echo("\n=== Semantic Runtime ===")
    from agentx.semantic.runtime import format_runtime_status, semantic_runtime_status

    rt = semantic_runtime_status()
    typer.echo(f"  {format_runtime_status(rt)}")
    if rt["status"] == "disabled":
        typer.echo("  [警告] 语义提取不可用：请安装 tree-sitter + tree-sitter-c")
        typer.echo("          （或 tree-sitter-language-pack），然后重新运行 agentx plan")

    typer.echo("\n=== 项目 ===")
    project = application.ensure_project()
    tasks = application.list_tasks()
    typer.echo(f"  root: {project.root_path}")
    typer.echo(f"  任务数: {len(tasks)}")
    if tasks:
        latest = tasks[-1]
        typer.echo(f"  最近任务: #{latest.id} [{latest.state.value}] {latest.goal[:60]}")
    application.store.close()


# ---------- 任务 ----------


@app.command("run")
def run(
    goal: str = typer.Argument(..., help="任务描述（传 - 则从 stdin 读取）"),
    path: Path = typer.Option(".", "--path", "-p", help="项目目录"),
    max_iterations: int = typer.Option(5, "--max-iterations", help="修复迭代上限"),
    print_only: bool = typer.Option(False, "--print", help="只输出最终结论，不打印过程"),
    model: str = typer.Option(None, "--model", help="覆盖模型"),
    permission_mode: str = typer.Option(None, "--permission-mode", help="review | auto"),
) -> None:
    """启动一个任务：Executor → Reviewer → Verifier → Decision 闭环。"""
    if goal == "-":
        goal = sys.stdin.read().strip()
        if not goal:
            typer.echo("stdin 为空。")
            raise typer.Exit(code=1)

    application = _app(path, model=model, permission_mode=permission_mode)
    task = application.create_task(goal, max_iterations=max_iterations)
    typer.echo(f"Task #{task.id}: {goal}")
    try:
        result = asyncio.run(application.run(task.id))
    except KeyboardInterrupt:
        typer.echo("\n已中断，任务停留在运行状态，可用 agentx stop 取消。")
        application.store.close()
        raise typer.Exit(code=130) from None

    if print_only:
        typer.echo(f"#{task.id} [{result.state.value}]")
        _print_decision(application, result)
    else:
        _print_task_outcome(result)
        if result.state == TaskState.WAITING_USER:
            _print_waiting_for_approval(application, result)
    application.store.close()


@app.command("resume")
def resume(
    path: Path = typer.Option(".", "--path", "-p", help="项目目录"),
    task_id: str = typer.Option(None, "--task", "-t", help="任务 ID（默认最近一个）"),
) -> None:
    """从 WAITING_USER 恢复任务（表示已批准高风险操作）。"""
    application = _app(path)
    tid = task_id or _require_latest(application).id
    result = asyncio.run(application.resume(tid))
    _print_task_outcome(result)
    if result.state == TaskState.WAITING_USER:
        _print_waiting_for_approval(application, result)
    application.store.close()


@app.command("stop")
def stop(
    path: Path = typer.Option(".", "--path", "-p", help="项目目录"),
    task_id: str = typer.Option(None, "--task", "-t", help="任务 ID（默认最近一个）"),
) -> None:
    """取消任务。"""
    application = _app(path)
    tid = task_id or _require_latest(application).id
    task = application.cancel(tid)
    if task is None:
        typer.echo(f"任务不存在: {tid}")
        raise typer.Exit(code=1)
    typer.echo(f"Task #{task.id} → {task.state.value}")
    application.store.close()


@app.command("status")
def status(
    path: Path = typer.Option(".", "--path", "-p", help="项目目录"),
    task_id: str = typer.Option(None, "--task", "-t", help="任务 ID（默认最近一个）"),
) -> None:
    """查看任务状态与最近的运行痕迹。"""
    application = _app(path)
    _print_project_status(application.project_root)
    task = _resolve_task(application, task_id)
    if task is None:
        typer.echo('没有任务。先用 agentx run "任务描述" 启动一个。')
        application.store.close()
        raise typer.Exit(code=1)
    _print_task_outcome(task)
    _print_decision(application, task)
    _print_recent_events(application, task, limit=10)
    application.store.close()


@app.command("sync")
def sync(
    path: Path = typer.Option(".", "--path", "-p", help="项目目录"),
    origin: str = typer.Option(
        "external",
        "--origin",
        help="变化来源: agentx_execution | external | unknown",
    ),
) -> None:
    """手动 Index Sync：按 Change Level 分级维护 Index（git diff 优先）。"""
    from agentx.index.sync import sync_index

    application = _app(path)
    root = application.project_root
    wizard = _run_scope_wizard(root)
    if wizard == "cancelled":
        typer.echo("未确认范围，Index 未建立（运行 agentx init 可重新引导）。")
        application.store.close()
        return
    if wizard == "written":
        typer.echo("首次初始化：Scope 配置已生成，开始建立 Index...")
    result = sync_index(root, origin=origin, progress=lambda m: typer.echo(f"  {m}"))
    typer.echo(f"\nLevel: {result['level']} | action: {result['action']}")
    typer.echo(f"变化文件: {result['changed_files'] or '无'}")
    typer.echo(f"说明: {result['message']}")
    if result.get("report_dir"):
        typer.echo(f"变更报告: {result['report_dir']}")
    application.store.close()


@app.command("build")
def build_cmd(
    path: Path = typer.Option(".", "--path", "-p", help="项目目录"),
) -> None:
    """Build Reality 状态：target / compiled / excluded / defines。"""
    from agentx.build import build_status_from_info
    from agentx.index.index import IndexStatus, index_status, load_index

    application = _app(path)
    root = application.project_root
    status, _ = index_status(root)
    if status not in (IndexStatus.VALID, IndexStatus.STALE):
        typer.echo("Build: unknown（Index 不可用，请先运行 agentx plan）")
        application.store.close()
        raise typer.Exit(code=1)
    index = load_index(root)
    if index is None:
        typer.echo("Build: unknown（Index 不存在）")
        application.store.close()
        raise typer.Exit(code=1)

    result = build_status_from_info(index.build_info or {})
    if result["build_status"] == "unknown":
        typer.echo("Build: unknown（未检测到构建配置）")
        application.store.close()
        return
    typer.echo(f"Build: {result['system']}")
    typer.echo(f"Target: {result['target']}")
    if result.get("cpu"):
        typer.echo(f"CPU: {result['cpu']}")
    typer.echo(f"Compiled Files: {result['compiled_count']}")
    typer.echo(f"Excluded: {result['excluded_count']}")
    typer.echo(f"Defines: {len(result['defines'])}")
    if result["defines"]:
        typer.echo(f"  {', '.join(result['defines'][:15])}")
    if result.get("project_file"):
        typer.echo(f"Project: {result['project_file']}")
    application.store.close()


@app.command("query")
def query_cmd(
    task: str = typer.Argument("", help="查询描述（feature/architecture 用）"),
    path: Path = typer.Option(".", "--path", "-p", help="项目目录"),
    symbol: str = typer.Option(None, "--symbol", "-s", help="符号查询（如 key_scan）"),
    architecture: str = typer.Option(
        None, "--architecture", "-a", help="架构流程查询（如 UART 接收流程）"
    ),
    module: str = typer.Option(None, "--module", "-m", help="模块查询（如 KEY）"),
) -> None:
    """Project Knowledge Query：功能/符号/架构/模块（纯 Index 证据，不扫描工程）。"""
    from agentx.index.index import IndexStatus, index_status, load_index
    from agentx.query.evidence import build_evidence_card, format_flow

    application = _app(path)
    root = application.project_root
    status, reason = index_status(root)
    if status not in (IndexStatus.VALID, IndexStatus.STALE):
        typer.echo(f"Index 不可用（{status}: {reason}）。请先运行 agentx plan 建立项目认知。")
        application.store.close()
        raise typer.Exit(code=1)
    index = load_index(root)
    if index is None:
        typer.echo("Index 不存在，请先运行 agentx plan。")
        application.store.close()
        raise typer.Exit(code=1)

    if module:
        from agentx.query.module_query import format_module_card, search_module

        result = search_module(index, module)
        qtype = "module"
    elif symbol:
        from agentx.query.symbol import search_symbol

        result = search_symbol(index, symbol)
        qtype = "symbol"
    elif architecture:
        from agentx.query.architecture import search_architecture

        result = search_architecture(index, architecture)
        qtype = "architecture"
    else:
        from agentx.query.feature import search_feature

        result = search_feature(index, task)
        qtype = "feature"

    typer.echo(f"Query type: {qtype}")
    typer.echo(f"Confidence: {result['confidence']}")
    typer.echo("")
    if result["confidence"] != "low":
        if qtype == "module":
            typer.echo(format_module_card(result))
        elif qtype == "architecture":
            typer.echo(format_flow(result.get("flow", []), result.get("evidence", [])))
        elif qtype == "symbol":
            from agentx.query.evidence import format_symbol_card

            typer.echo(format_symbol_card(result))
        else:
            typer.echo(build_evidence_card(index, result))
    else:
        typer.echo(f"Reason: {result['reason'][0]}")
    ra = result.get("recommended_action", {})
    typer.echo("")
    typer.echo(f"Recommended action: {ra.get('type', 'unknown')}")
    if ra.get("files"):
        typer.echo(f"Suggested files: {', '.join(ra['files'])}")
    application.store.close()


@app.command("search-feature")
def search_feature_cmd(
    task: str = typer.Argument(..., help="功能描述，如：实体按键实现"),
    path: Path = typer.Option(".", "--path", "-p", help="项目目录"),
) -> None:
    """工程探索：某功能在哪里实现/作用/调用链（纯 Index 证据，不扫描工程）。"""
    from agentx.index.index import IndexStatus, index_status, load_index
    from agentx.query.evidence import build_evidence_card
    from agentx.query.feature import search_feature

    application = _app(path)
    root = application.project_root
    status, reason = index_status(root)
    if status not in (IndexStatus.VALID, IndexStatus.STALE):
        typer.echo(f"Index 不可用（{status}: {reason}）。请先运行 agentx plan 建立项目认知。")
        application.store.close()
        raise typer.Exit(code=1)
    index = load_index(root)
    if index is None:
        typer.echo("Index 不存在，请先运行 agentx plan。")
        application.store.close()
        raise typer.Exit(code=1)

    result = search_feature(index, task)
    typer.echo(f"Feature: {result['feature'] or '(未识别)'}")
    typer.echo(f"Confidence: {result['confidence']}")
    typer.echo("")
    if result["confidence"] != "low":
        typer.echo(build_evidence_card(index, result))
    else:
        typer.echo(f"Reason: {result['reason'][0]}")
    ra = result.get("recommended_action", {})
    typer.echo("")
    typer.echo(f"Recommended action: {ra.get('type', 'unknown')}")
    if ra.get("files"):
        typer.echo(f"Suggested files: {', '.join(ra['files'])}")
    application.store.close()


@app.command("understand")
def understand(
    path: Path = typer.Option(".", "--path", "-p", help="项目目录"),
) -> None:
    """主动刷新工程理解（Core Path Understanding）。"""
    from agentx.index.index import load_index
    from agentx.plan.service import enrich_index, ensure_index, is_skeleton_index
    from agentx.understanding.understand import (
        discover_entry_candidates,
        ensure_understanding,
    )

    application = _app(path)
    root = application.project_root
    wizard = _run_scope_wizard(root)
    if wizard == "cancelled":
        typer.echo("未确认范围，Index 未建立（运行 agentx init 可重新引导）。")
        application.store.close()
        return
    if wizard == "written":
        typer.echo("首次初始化：Scope 配置已生成，开始建立 Index...")
    ensure_index(root)
    index = load_index(root)
    if index is None:
        typer.echo("Index 不存在，请先运行 agentx plan")
        application.store.close()
        raise typer.Exit(code=1)
    # Phase 7.9 Index 完整性：ensure_index 只建骨架（files 无认知）。骨架必须
    # enrich 补全（CodeGraph symbols/call_graph + semantic + modules），否则
    # 用户得到 files 有、但 symbols/modules/call_graph 全空的伪完整 Index。
    if is_skeleton_index(index):
        index, _ = enrich_index(root)

    candidates = discover_entry_candidates(index)
    typer.echo(f"候选入口（{len(candidates)}）:")
    for c in candidates:
        typer.echo(f"  {c.file} ({c.symbol}) [{c.confidence}] {c.reason}")

    typer.echo("探索核心路径...")
    result = asyncio.run(
        ensure_understanding(
            application,
            root,
            force=True,
            progress=lambda m: typer.echo(f"  {m}"),
        )
    )
    typer.echo(f"\n结果: {result['status']} | {result['message']}")
    application.store.close()


@app.command("plan")
def plan(
    goal: str = typer.Argument(..., help="任务描述"),
    path: Path = typer.Option(".", "--path", "-p", help="项目目录"),
    origin: str = typer.Option(
        "external",
        "--origin",
        help="变化来源: agentx_execution | external | unknown",
    ),
    force_rebuild: bool = typer.Option(
        False,
        "--force-rebuild",
        help="显式重建 Index（VALID 也重建，用户意图优先）",
    ),
) -> None:
    """执行 Plan：检查/建立 Project Index + 实施方案（实时阶段输出）。"""
    from agentx.providers.openai import LLMRequestError
    from agentx.runtime.events import EventCollector, Heartbeat

    application = _app(path)
    wizard = _run_scope_wizard(application.project_root)
    if wizard == "cancelled":
        typer.echo("未确认范围，Index 未建立（运行 agentx init 可重新引导）。")
        application.store.close()
        return
    if wizard == "written":
        typer.echo("首次初始化：Scope 配置已生成，开始建立 Index...")
    collector = EventCollector()
    listener, heartbeat_printer = _make_live_printer()
    collector.subscribe(listener)
    heartbeat = Heartbeat(
        collector,
        on_beat=heartbeat_printer,
        interval=float(os.environ.get("AGENTX_HEARTBEAT_INTERVAL", "15")),
    )
    result = None
    try:
        result = asyncio.run(
            _plan_with_heartbeat(application, goal, origin, force_rebuild, collector, heartbeat)
        )
    except LLMRequestError as e:
        # LLM 调用失败：结构化输出（formatter 不做字符串解析）
        typer.echo(f"\nLLM 调用失败: [{e.category}] {e.detail}")
        typer.echo(
            f"  provider={e.provider} model={e.model} status={e.status} "
            f"request_id={e.request_id} trace_id={e.trace_id}"
        )
        if e.error_body:
            typer.echo(f"  body: {e.error_body[:300]}")
        application.store.close()
        raise typer.Exit(code=1) from None
    assert result is not None
    for warning in result.get("warnings") or []:
        typer.echo(f"[WARN] {warning}")
    before = result["index_before"]
    after = result["index_after"]
    arrow = "->" if before["status"] != after["status"] else ""
    typer.echo(f"\nIndex: {before['status']} {arrow} {after['status']} | {after['reason']}")
    if result.get("index_created"):
        typer.echo("本次调用新建了 Index")
    typer.echo(f"Index 目录: {result['index_dir']}")
    typer.echo(f"认知来源: {result['codegraph_source']}")
    plan_data = result["plan"]
    typer.echo(f"\n方案摘要: {plan_data['summary']}")
    typer.echo(f"验证命令: {plan_data.get('verification')}")
    typer.echo(f"涉及文件: {plan_data.get('files_involved')}")
    application.store.close()


async def _plan_with_heartbeat(
    application: Application,
    goal: str,
    origin: str,
    force_rebuild: bool,
    collector: Any,
    heartbeat: Any,
) -> dict[str, Any]:
    """plan + Heartbeat 同事件循环（心跳才能实时驱动）。"""
    from agentx.plan.service import PlanService

    heartbeat.start()
    try:
        return await PlanService(application).plan(
            goal,
            progress=lambda m: typer.echo(f"  {m}"),
            origin=origin,
            force_rebuild=force_rebuild,
            on_event=lambda s, st, m: collector.emit(s, st, m),
        )
    finally:
        heartbeat.stop()


@app.command("review")
def review(
    goal: str = typer.Argument(..., help="任务描述"),
    path: Path = typer.Option(".", "--path", "-p", help="项目目录"),
    origin: str = typer.Option(
        "external",
        "--origin",
        help="变化来源: agentx_execution | external | unknown",
    ),
) -> None:
    """审查当前修改：Index + Plan + Diff（带进度输出）。"""
    from agentx.review.service import ReviewService

    application = _app(path)
    result = asyncio.run(
        ReviewService(application).review(
            goal, progress=lambda m: typer.echo(f"  {m}"), origin=origin
        )
    )
    typer.echo(f"\nverdict: {result.get('verdict')}")
    for f in result.get("findings", []):
        sev = f.get("severity", "")
        typer.echo(f"  [{sev}] {f.get('location', '')}: {f.get('description', '')}")
    application.store.close()


@app.command("verify")
def verify(
    goal: str = typer.Argument(..., help="任务描述"),
    path: Path = typer.Option(".", "--path", "-p", help="项目目录"),
    origin: str = typer.Option(
        "external",
        "--origin",
        help="变化来源: agentx_execution | external | unknown",
    ),
) -> None:
    """确定性验证：执行 Plan 验证命令（带进度输出）。"""
    from agentx.verify.service import VerifyService

    application = _app(path)
    result = asyncio.run(
        VerifyService(application).verify(
            goal, progress=lambda m: typer.echo(f"  {m}"), origin=origin
        )
    )
    typer.echo(f"\nverdict: {result.get('verdict')}")
    for ev in result.get("evidence", []):
        typer.echo(f"  证据: {ev.get('command', '')} exit={ev.get('exit_code')}")
    typer.echo(f"结论: {result.get('conclusion')}")
    application.store.close()


# ---------- CodeGraph 管理 ----------

cg_app = typer.Typer(help="内置 CodeGraph 管理（版本锁定 + 自动 bootstrap）", no_args_is_help=True)
app.add_typer(cg_app, name="codegraph")


@cg_app.command("install")
def codegraph_install() -> None:
    """安装/重装锁定版本的 CodeGraph（下载 + SHA512 校验 + 原子安装）。"""
    from agentx.understanding.graph import reset_codegraph_cache
    from agentx.vendor.bootstrap import BootstrapError, bootstrap_install

    try:
        node, bin_path = bootstrap_install(force=True)
    except BootstrapError as e:
        typer.echo(f"[ERROR] CodeGraph 安装失败: {e}")
        raise typer.Exit(code=1) from None
    reset_codegraph_cache()
    typer.echo(f"[OK] CodeGraph 已安装 (v{node.parent.name})")
    typer.echo(f"  node: {node}")
    typer.echo(f"  bin : {bin_path}")
    typer.echo("  后续 plan/index 将自动使用内置 CodeGraph；不可用时降级文件扫描。")


@cg_app.command("status")
def codegraph_status() -> None:
    """显示内置 CodeGraph 状态（版本 / 平台 / 安装位置）。"""
    from agentx.vendor.bootstrap import bootstrap_status

    status = bootstrap_status()
    typer.echo(f"锁定版本: {status['version']}")
    typer.echo(f"平台     : {status['target'] or '不支持'}")
    if status.get("installed"):
        typer.echo("状态     : [OK] 已安装")
        typer.echo(f"node     : {status['node']}")
        typer.echo(f"bin      : {status['bin']}")
        meta = status.get("metadata") or {}
        if meta:
            typer.echo(
                f"来源     : {meta.get('source')} ({meta.get('downloaded_at', '')[:19]} UTC)"
            )
    else:
        typer.echo("状态     : 未安装（首次使用 plan/index 时自动安装）")
        if status["target"] is None:
            typer.echo("注意     : 当前平台没有官方 CodeGraph binary，将使用文件扫描")


@cg_app.command("upgrade")
def codegraph_upgrade() -> None:
    """重新安装到 AgentX 锁定的版本（不跟随 CodeGraph 最新版）。"""
    from agentx.understanding.graph import reset_codegraph_cache
    from agentx.vendor.bootstrap import BootstrapError, bootstrap_install

    try:
        node, _ = bootstrap_install(force=True)
    except BootstrapError as e:
        typer.echo(f"[ERROR] CodeGraph 升级失败: {e}")
        raise typer.Exit(code=1) from None
    reset_codegraph_cache()
    typer.echo(f"[OK] CodeGraph 已更新到锁定版本 v{node.parent.name}")


@app.command("sessions")
def sessions(
    path: Path = typer.Option(".", "--path", "-p", help="项目目录"),
) -> None:
    """列出所有任务（会话）。"""
    application = _app(path)
    tasks = application.list_tasks()
    if not tasks:
        typer.echo("没有任务。")
        application.store.close()
        raise typer.Exit(code=1)
    for task in reversed(tasks[-20:]):
        ts = task.created_at.astimezone().strftime("%m-%d %H:%M")
        state = f"{task.state.value:12s}"
        typer.echo(f"#{task.id} [{state}] iter={task.iteration} {ts} {task.goal[:60]}")
    application.store.close()


@app.command("logs")
def logs(
    path: Path = typer.Option(".", "--path", "-p", help="项目目录"),
    task_id: str = typer.Option(None, "--task", "-t", help="任务 ID（默认最近一个）"),
    all_tasks: bool = typer.Option(False, "--all", help="显示所有任务的事件"),
) -> None:
    """查看任务完整时间线（事件回放）。"""
    application = _app(path)
    task = _resolve_task(application, task_id)
    if task is None and not all_tasks:
        typer.echo("没有任务。")
        application.store.close()
        raise typer.Exit(code=1)
    _print_events(application, task.id if task else None, all_tasks=all_tasks)
    application.store.close()


@app.command("agents")
def agents(
    path: Path = typer.Option(".", "--path", "-p", help="项目目录"),
) -> None:
    """列出 Agent 配置。"""
    application = _app(path)
    typer.echo(f"Provider: {application.provider_name} | Model: {application.model}")
    for agent in application.store.list_agents():
        typer.echo(f"  {agent.id}  role={agent.role.value}  status={agent.status.value}")
    application.store.close()


@app.command("init")
def init(
    path: Path = typer.Option(".", "--path", "-p", help="项目目录"),
    yes: bool = typer.Option(False, "--yes", help="跳过交互：自动采纳建议，生成 .agentxscope.yaml"),
) -> None:
    """初始化项目工作区 (.agentx/) + Scope Discovery（三层：project/third_party/ignore）。"""
    from agentx.scope.config import load_scope_config

    application = _app(path)
    root = application.project_root
    application.ensure_project()
    typer.echo(f"已初始化: {root}")
    typer.echo(f"Provider: {application.provider_name} | Model: {application.model}")

    existing = load_scope_config(root)
    if existing.get("third_party") or existing.get("ignore") or existing.get("project_include"):
        typer.echo("Scope 已配置（.agentxscope.yaml），如需调整请直接编辑。")
        application.store.close()
        return

    result = _run_scope_wizard(root, yes=yes)
    if result == "cancelled":
        typer.echo("未采纳任何建议（全部按 project 分析）。")
    elif result == "":
        typer.echo("未发现第三方库或需要忽略的目录（全部按 project 分析）。")
    application.store.close()


# ---------- 展示辅助 ----------


def _print_task_outcome(task: Task) -> None:
    typer.echo(f"\nTask #{task.id} [{task.state.value}] iteration={task.iteration}")


def _print_decision(application: Application, task: Task) -> None:
    for d in application.store.list_decisions(task.id)[-1:]:
        typer.echo(f"决策: {d.result} | {d.reason}")


def _print_waiting_for_approval(application: Application, task: Task) -> None:
    typer.echo(
        "\n需要用户审批（高风险操作）。\n"
        f"  批准并继续: agentx resume --task {task.id}\n"
        f"  拒绝并取消: agentx stop --task {task.id}"
    )


def _resolve_task(application: Application, task_id: str | None) -> Task | None:
    if task_id is not None:
        return application.store.get_task(task_id)
    return application.latest_task()


def _require_latest(application: Application) -> Task:
    task = application.latest_task()
    if task is None:
        typer.echo("没有任务。")
        raise typer.Exit(code=1)
    return task


def _print_recent_events(application: Application, task: Task, limit: int) -> None:
    events = application.store.list_events(task.id)
    typer.echo(f"\n最近 {min(limit, len(events))} 条事件:")
    for event in events[-limit:]:
        typer.echo(_format_event(event))


def _print_events(application: Application, task_id: str | None, all_tasks: bool) -> None:
    events = application.store.list_events(task_id=None if all_tasks else task_id)
    if not events:
        typer.echo("(无事件)")
        return
    for event in events:
        typer.echo(_format_event(event))


def _format_event(event: StoredEvent) -> str:
    ts = event.ts.astimezone().strftime("%H:%M:%S")
    detail = ""
    if event.payload:
        detail = " " + _compact_payload(event.payload)
    return f"{event.seq:>4} {ts} [{event.type}] task={event.task_id}{detail}"


def _compact_payload(payload: dict[str, object]) -> str:
    parts = []
    for key in ("to", "from", "tool", "ok", "exit_code", "finding_id"):
        if key in payload:
            parts.append(f"{key}={payload[key]}")
    return " ".join(parts)


def main() -> None:
    app()
