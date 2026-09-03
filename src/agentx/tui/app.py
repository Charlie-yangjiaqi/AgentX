"""AgentX TUI：两栏对话流工作台。

左栏：Agent 对话时间线（Executor / Reviewer / Verifier / AgentX 按时间交错，
      Tool 调用默认折叠可展开，Finding / Verdict / Evidence 结构化卡片）
右栏：陈列区 / TODO（任务状态机进度 + Findings / Evidence / Changes 汇总）
"""

from __future__ import annotations

import re
import uuid
from pathlib import Path

from rich.markup import escape
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.events import Click
from textual.widgets import Footer, Header, Input, Static

from agentx.app.application import Application
from agentx.config.config import load_config
from agentx.core.timeline import (
    KIND_CHANGE,
    KIND_DECISION,
    KIND_EVIDENCE,
    KIND_STATE,
    KIND_TOOL,
    ROLE_AGENTX,
    ROLE_EXECUTOR,
    ROLE_REVIEWER,
    ROLE_USER,
    ROLE_VERIFIER,
    TimelineEntry,
    build_timeline,
)
from agentx.state.models import (
    EVENT_AGENT_STARTED,
    EVENT_FINDING_CREATED,
    EVENT_TASK_STATE_CHANGED,
    EVENT_TOOL_FINISHED,
    Message,
    MessageScope,
    StoredEvent,
    TaskState,
)

CSS = """
Screen { layout: vertical; }
#main { height: 1fr; }
#conversation { width: 3fr; border: round $accent; }
#display { width: 2fr; border: round $secondary; }
#conversation-title, #display-title { text-style: bold; padding: 0 1; }
#timeline { padding: 0 1; }
#display-body { padding: 0 1; }
#input { dock: bottom; }
"""

# 角色视觉：头像 / 名称 / 颜色
ROLE_STYLE: dict[str, tuple[str, str, str]] = {
    ROLE_EXECUTOR: ("👷", "Executor", "cyan"),
    ROLE_REVIEWER: ("🕵️", "Reviewer", "yellow"),
    ROLE_VERIFIER: ("🧪", "Verifier", "green"),
    ROLE_AGENTX: ("🤖", "AgentX", "magenta"),
    ROLE_USER: ("👤", "User", "white"),
}

PHASE_TODO = [
    (TaskState.EXECUTING, "实现 (Executor)"),
    (TaskState.REVIEWING, "审查 (Reviewer)"),
    (TaskState.VERIFYING, "验证 (Verifier)"),
    (TaskState.DECIDING, "决策 (AgentX)"),
]


class AgentXApp(App[None]):
    """AgentX TUI：对话流 + 陈列区。"""

    TITLE = "AgentX"
    CSS = CSS

    def __init__(self, project_root: Path | None = None) -> None:
        super().__init__()
        self.project_root = (project_root or Path.cwd()).resolve()
        self.application = Application(self.project_root, config=load_config())
        self.current_task_id: str | None = None
        self._expanded_tools: set[str] = set()
        self._tl_cards: dict[str, Static] = {}

    # ---------- 生命周期 ----------

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Horizontal(id="main"):
            with Vertical(id="conversation"):
                yield Static("对话流 — Agent 协作时间线", id="conversation-title")
                with VerticalScroll(id="timeline"):
                    pass
            with Vertical(id="display"):
                yield Static("陈列区 / TODO", id="display-title")
                yield Static("(无任务)", id="display-body", classes="display-body")
        yield Input(placeholder="任务目标，或 /help 查看命令，@agent 与 Agent 对话", id="input")
        yield Footer()

    def on_mount(self) -> None:
        latest = self.application.latest_task()
        if latest is not None:
            self.current_task_id = latest.id
            self._refresh_all()
        self.application.event_bus.subscribe(self._on_event, event_type=EVENT_TOOL_FINISHED)
        self.application.event_bus.subscribe(self._on_event, event_type=EVENT_FINDING_CREATED)
        self.application.event_bus.subscribe(self._on_event, event_type=EVENT_TASK_STATE_CHANGED)
        self.application.event_bus.subscribe(self._on_event, event_type=EVENT_AGENT_STARTED)
        self._emit_center("AgentX TUI 就绪。输入任务目标启动，/help 查看命令。")

    # ---------- 事件订阅 ----------

    def _on_event(self, event: StoredEvent) -> None:
        self._refresh_all()

    # ---------- 输入处理 ----------

    def on_input_submitted(self, message: Input.Submitted) -> None:
        text = message.value.strip()
        input_widget = message.input
        if not text:
            return
        input_widget.value = ""
        self._dispatch(text)

    def _dispatch(self, text: str) -> None:
        if text.startswith("/"):
            self._slash(text)
        elif text.startswith("@"):
            self._at_message(text)
        else:
            self._start_task(text)

    def _slash(self, text: str) -> None:
        parts = text.split(maxsplit=1)
        cmd = parts[0].lower()
        arg = parts[1] if len(parts) > 1 else ""

        if cmd == "/help":
            self._emit_center(
                "命令：/help /status /sessions /approve /reject /stop /quit\n"
                "对话：@executor 消息 ｜ @reviewer 消息 ｜ @verifier 消息 ｜ @all 消息\n"
                "输入其他内容 = 创建并运行新任务。\n"
                "点击 Tool 行可展开查看参数与结果。"
            )
        elif cmd == "/status":
            self._refresh_all()
            self._emit_center("已刷新。")
        elif cmd == "/sessions":
            for task in reversed(self.application.list_tasks()[-10:]):
                self._emit_center(f"#{task.id} [{task.state.value}] {task.goal[:50]}")
        elif cmd in {"/approve", "/resume"}:
            self._resume_task(arg)
        elif cmd in {"/reject", "/stop"}:
            self._cancel_task(arg)
        elif cmd == "/quit":
            self.exit()
        else:
            self._emit_center(f"未知命令: {cmd}（/help 查看）")

    def _at_message(self, text: str) -> None:
        app = self.application
        target, _, content = text.partition(" ")
        scope = MessageScope.MEETING if target == "@all" else MessageScope.AGENT_PRIVATE
        app.store.insert_message(
            Message(
                id=uuid.uuid4().hex,
                task_id=self.current_task_id,
                sender="user",
                target=target[1:],
                scope=scope,
                content=content,
            )
        )
        self._refresh_all()

    def _start_task(self, goal: str) -> None:
        app = self.application
        if app.api_key is None:
            self._emit_center("✖ 未配置 API Key，任务将用 mock 运行（无法验证，会失败）。")
        task = app.create_task(goal)
        self.current_task_id = task.id
        self._refresh_all()
        self.run_worker(self._run_task_worker(task.id), name=f"task-{task.id}")

    async def _run_task_worker(self, task_id: str) -> None:
        app = self.application
        result = await app.run(task_id)
        self._emit_center(f"✔ 任务结束: #{result.id} [{result.state.value}]")
        if result.state == TaskState.WAITING_USER:
            self._emit_center("⚠ 需要审批：/approve 继续，/reject 停止。")
        self._refresh_all()

    def _resume_task(self, task_id: str) -> None:
        app = self.application
        tid = task_id or self.current_task_id
        task = app.store.get_task(tid) if tid else None
        if task is None:
            self._emit_center("没有可恢复的任务。")
            return
        self.current_task_id = task.id
        self.run_worker(self._resume_worker(task.id), name=f"resume-{task.id}")

    async def _resume_worker(self, task_id: str) -> None:
        app = self.application
        result = await app.resume(task_id)
        self._emit_center(f"✔ 任务结束: #{result.id} [{result.state.value}]")
        if result.state == TaskState.WAITING_USER:
            self._emit_center("⚠ 仍需审批：/approve 继续。")
        self._refresh_all()

    def _cancel_task(self, task_id: str) -> None:
        app = self.application
        tid = task_id or self.current_task_id
        task = app.cancel(tid) if tid else None
        if task is not None:
            self._emit_center(f"✖ 任务 #{task.id} → {task.state.value}")
            self._refresh_all()

    # ---------- 面板刷新 ----------

    def _refresh_all(self) -> None:
        self._refresh_timeline()
        self._refresh_display()

    def _refresh_timeline(self) -> None:
        scroll = self.query_one("#timeline", VerticalScroll)
        tid = self.current_task_id
        if tid is None:
            return
        entries = build_timeline(self.application.store, tid)
        new_keys = {e.key for e in entries}
        # 移除已消失的卡片（异步无害）
        for key in list(self._tl_cards):
            if key not in new_keys:
                card = self._tl_cards.pop(key)
                scroll.remove_children([card])
        # 新增 / 更新
        for entry in entries:
            key = entry.key
            expanded = entry.kind == KIND_TOOL and key in self._expanded_tools
            text = self._render_entry(entry, expanded=expanded)
            if key in self._tl_cards:
                self._tl_cards[key].update(text)
            else:
                card = Static(text, classes="entry", id=f"tl-{_safe_id(key)}")
                self._tl_cards[key] = card
                scroll.mount(card)
        scroll.scroll_end(animate=False)

    def on_click(self, event: Click) -> None:
        if event.widget is None:
            return
        key = next((k for k, c in self._tl_cards.items() if c is event.widget), None)
        if key is None:
            return
        if key in self._expanded_tools:
            self._expanded_tools.discard(key)
        else:
            self._expanded_tools.add(key)
        self._refresh_timeline()

    def _refresh_display(self) -> None:
        app = self.application
        body = self.query_one("#display-body", Static)
        tid = self.current_task_id
        if tid is None:
            body.update("(无任务)\n输入任务目标开始。")
            return
        task = app.store.get_task(tid)
        if task is None:
            body.update("(任务不存在)")
            return

        lines = [f"[bold]Task #{task.id}[/bold] [{task.state.value}] iter={task.iteration}"]
        lines.append(f"[dim]目标: {escape(task.goal[:60])}[/dim]")
        lines.append("")
        lines.append("[bold]── TODO ──[/bold]")
        lines.extend(self._todo_lines(task.state))
        lines.append("")
        lines.append(f"[bold]── Findings ({len(app.store.list_findings(tid))}) ──[/bold]")
        for f in app.store.list_findings(tid)[:6]:
            sev = f.severity.value
            color = {"HIGH": "red", "BLOCKER": "red", "MEDIUM": "yellow"}.get(sev, "white")
            lines.append(f"  [{color}]{sev}[/{color}] {escape(f.description[:40])}")
            if f.location:
                lines.append(f"      [dim]{escape(f.location)}[/dim]")
        lines.append("")
        lines.append(f"[bold]── Evidence ({len(app.store.list_evidence(tid))}) ──[/bold]")
        for e in app.store.list_evidence(tid)[-5:]:
            ok = "✓" if e.exit_code == 0 else "✗"
            lines.append(f"  {ok} [dim]{escape((e.command or '')[:40])}[/dim] exit={e.exit_code}")
        lines.append("")
        lines.append(f"[bold]── Changes ({len(app.store.list_changes(tid))}) ──[/bold]")
        for c in app.store.list_changes(tid)[-5:]:
            lines.append(f"  {c.operation} {escape(c.file)}")
        body.update("\n".join(lines))

    def _todo_lines(self, state: TaskState) -> list[str]:
        lines = []
        phase_done = _phase_index(state)
        for i, (_, label) in enumerate(PHASE_TODO):
            if phase_done is None:
                mark = "○"
            elif i < phase_done:
                mark = "✓"
            elif i == phase_done:
                mark = "●"
            else:
                mark = "○"
            lines.append(f"  {mark} {label}")
        if state == TaskState.WAITING_USER:
            lines.append("  ⚠ 等待用户审批")
        elif state == TaskState.REPAIRING:
            lines.append("  ↻ 修复中，回到实现")
        return lines

    # ---------- 时间线渲染 ----------

    def _render_entry(self, entry: TimelineEntry, *, expanded: bool = False) -> str:
        if entry.kind == KIND_STATE:
            return f"[dim]── {escape(entry.content)} ──[/dim]"
        if entry.kind == KIND_TOOL:
            return self._render_tool(entry, expanded=expanded)
        if entry.kind == KIND_EVIDENCE:
            return self._render_evidence(entry)
        if entry.kind == KIND_CHANGE:
            return self._render_change(entry)
        if entry.kind == KIND_DECISION:
            return self._render_decision(entry)
        structured = entry.payload.get("structured")
        if structured == "finding":
            return self._render_finding(entry)
        if structured == "verdict":
            return self._render_verdict(entry)
        return self._render_message(entry)

    def _header(self, entry: TimelineEntry) -> str:
        icon, name, color = ROLE_STYLE.get(
            entry.sender_role or ROLE_AGENTX, ROLE_STYLE[ROLE_AGENTX]
        )
        ts = entry.ts.astimezone().strftime("%H:%M:%S")
        return f"[{color}]{icon} {name}[/{color}] [dim]{ts}[/dim]"

    def _render_message(self, entry: TimelineEntry) -> str:
        return f"{self._header(entry)}\n  {escape(entry.content)}"

    def _render_finding(self, entry: TimelineEntry) -> str:
        findings = entry.payload.get("finding") or []
        parts = [self._header(entry)]
        for f in findings[:5]:
            sev = str(f.get("severity", "INFO"))
            color = {"HIGH": "red", "BLOCKER": "red", "MEDIUM": "yellow"}.get(sev, "white")
            loc = f.get("location") or ""
            desc = f.get("description") or ""
            parts.append(
                f"  ┌─ [{color}]{sev}[/{color}] {escape(str(f.get('category', '')))} ──────"
            )
            if loc:
                parts.append(f"  │ {escape(str(loc))}")
            parts.append(f"  │ {escape(str(desc))}")
            parts.append("  └──────────────")
        return "\n".join(parts)

    def _render_verdict(self, entry: TimelineEntry) -> str:
        v = entry.payload.get("verdict") or {}
        conclusion = str(v.get("conclusion", "FAIL"))
        color = "green" if conclusion == "PASS" else "red"
        parts = [self._header(entry)]
        parts.append(f"  ┌─ 验证结论: [{color}]{conclusion}[/{color}] ────")
        build = v.get("build")
        if isinstance(build, dict):
            parts.append(f"  │ build: {escape(str(build.get('command', '')))}")
        for t in v.get("tests") or []:
            if isinstance(t, dict):
                parts.append(f"  │ test: {escape(str(t.get('command', '')))}")
        notes = v.get("notes")
        if notes:
            parts.append(f"  │ {escape(str(notes))}")
        parts.append("  └──────────────")
        return "\n".join(parts)

    def _render_decision(self, entry: TimelineEntry) -> str:
        result = str(entry.payload.get("result", ""))
        color = "green" if result == "PASS" else "red"
        parts = [self._header(entry)]
        parts.append(f"  ┌─ [{color}]{result}[/{color}] ─────────────")
        for line in (entry.content or "").split(";"):
            line = line.strip()
            if line:
                parts.append(f"  │ {escape(line)}")
        parts.append("  └──────────────")
        return "\n".join(parts)

    def _render_evidence(self, entry: TimelineEntry) -> str:
        exit_code = entry.payload.get("exit_code")
        ok = "✓" if exit_code == 0 else "✗"
        return f"{self._header(entry)}\n  {ok} [dim]{escape(entry.content)}[/dim]"

    def _render_change(self, entry: TimelineEntry) -> str:
        return f"{self._header(entry)}\n  📝 [dim]{escape(entry.content)}[/dim]"

    def _render_tool(self, entry: TimelineEntry, *, expanded: bool) -> str:
        payload = entry.payload
        tool = entry.content
        ok = payload.get("ok")
        exit_code = payload.get("exit_code")
        marker = "▾" if expanded else "▸"
        status = ""
        if ok is True:
            status = "[green]✓[/green]"
        elif ok is False:
            status = "[red]✗[/red]"
        cmd = ""
        args = payload.get("args")
        if isinstance(args, dict) and args.get("command"):
            cmd = f": {escape(str(args['command']))[:60]}"
        head = (
            f"{self._header(entry)}\n"
            f"  {marker} [dim]⚙ {escape(tool)}[/dim]{cmd} {status} exit={exit_code}"
        )
        if not expanded:
            return head
        output = payload.get("output")
        lines = [head]
        if isinstance(args, dict):
            for k, v in list(args.items())[:4]:
                lines.append(f"    arg {k} = {escape(str(v))[:100]}")
        if output:
            lines.append(f"    [dim]{escape(str(output)[:300])}[/dim]")
        return "\n".join(lines)

    def _emit_center(self, text: str) -> None:
        key = "_system"
        card_text = f"[bold]{escape(text)}[/bold]"
        scroll = self.query_one("#timeline", VerticalScroll)
        if key in self._tl_cards:
            self._tl_cards[key].update(card_text)
        else:
            card = Static(card_text, classes="entry", id="tl-_system")
            self._tl_cards[key] = card
            scroll.mount(card)
        scroll.scroll_end(animate=False)


def _phase_index(state: TaskState) -> int | None:
    for i, (phase, _) in enumerate(PHASE_TODO):
        if state == phase:
            return i
    return None


def _safe_id(key: str) -> str:
    """Textual id 只允许字母数字下划线连字符：把冒号等转成下划线。"""
    return re.sub(r"[^a-zA-Z0-9_-]", "_", key)


def main() -> None:
    AgentXApp().run()
