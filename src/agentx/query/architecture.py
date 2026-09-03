"""Architecture Query："这个项目 XXX 流程怎么走？"

基于 Index 事实输出 Evidence Flow（符号链），不解释语义——
"数据从串口进入，然后处理业务..." 是宿主（Reasonix）的职责，
AgentX 只给事实链。

流程构造（零 LLM）：
1. 启动流程（含"启动"主题）：优先用 understanding.startup_flow / entry_points
2. 其他流程（UART/参数保存/UI刷新）：主题符号命中 → 沿 call_graph 线性化
   （上游 callers → 命中符号 → 下游 callees），每步附定义文件证据
"""

from __future__ import annotations

from typing import Any

from agentx.index.index import ProjectIndex
from agentx.query.index_query import (
    build_facts,
    expand_call_edges,
    find_symbols,
    no_evidence,
)
from agentx.understanding.query import extract_keywords

NEXT_ANSWER = "answer"
NEXT_READ_SOURCE = "read_source"

_ZH_FLOW_ALIASES = {
    "启动": "startup",
    "开机": "startup",
    "串口": "uart",
    "通信": "comm",
    "参数": "param",
    "保存": "save",
    "刷新": "refresh",
    "界面": "ui",
    "显示": "display",
    "初始化": "init",
}


def search_architecture(index: ProjectIndex, task: str) -> dict[str, Any]:
    """架构流程查询：topic → Evidence Flow（事实链，不解释）。"""
    topic = _topic_from_task(task)

    # 启动流程：understanding 优先
    if _is_startup_topic(task):
        flow, evidence = _startup_flow(index)
        if flow:
            return _result(topic, flow, evidence, index, confidence="high")

    # 其他流程：主题符号 → 调用链线性化
    keywords = extract_keywords(task)
    hit_symbols = find_symbols(index, keywords)
    if not hit_symbols:
        # 主题别名兜底（如 "uart"）
        hit_symbols = find_symbols(index, [topic] if topic else keywords)
    if not hit_symbols:
        return no_evidence("no matching symbols for architecture topic")

    flow, evidence = _linearize_chain(index, set(hit_symbols))
    if not flow:
        return no_evidence("topic symbols found but no call chain")
    confidence = "high" if len(flow) >= 2 else "medium"
    return _result(topic, flow, evidence, index, confidence)


# ---------- 内部辅助 ----------


def _result(
    topic: str,
    flow: list[str],
    evidence: list[str],
    index: ProjectIndex,
    confidence: str,
) -> dict[str, Any]:
    return {
        "query": topic,
        "topic": topic,
        "flow": flow,
        "evidence": evidence[:30],
        "confidence": confidence,
        "build": build_facts(index, []) or {},
        "recommended_action": {"type": NEXT_ANSWER},
        "reason": [f"flow constructed from {len(flow)} symbols"],
    }


def _topic_from_task(task: str) -> str:
    for zh, en in _ZH_FLOW_ALIASES.items():
        if zh in (task or ""):
            return en
    return task or ""


def _is_startup_topic(task: str) -> bool:
    return any(zh in (task or "") for zh in ("启动", "开机", "上电"))


def _startup_flow(index: ProjectIndex) -> tuple[list[str], list[str]]:
    """启动流程：understanding.startup_flow / entry_points 事实。"""
    understanding = index.project_understanding or {}
    flow: list[str] = []
    evidence: list[str] = []
    startup = understanding.get("startup_flow")
    if startup:
        if isinstance(startup, list):
            flow = [str(x) for x in startup]
        elif isinstance(startup, str):
            flow = [line.strip() for line in startup.splitlines() if line.strip()]
    elif understanding.get("entry_points"):
        flow = [str(x) for x in understanding["entry_points"]]
    if not flow:
        entries = [str(x) for x in (understanding.get("entry_points") or [])]
        if entries:
            flow = entries
    for step in flow:
        evidence.append(f"startup step: {step}")
    return flow, evidence


def _linearize_chain(index: ProjectIndex, hit_sym_names: set[str]) -> tuple[list[str], list[str]]:
    """调用链线性化：按调用边顺序构造事实链（caller 在前）。

    命中符号之间的边决定顺序（如 uart_protocol_adapter → uart_callback →
    app_controller_handle_uart），非命中符号（上游/下游）也纳入边。
    """
    edges = expand_call_edges(index, hit_sym_names)
    flow: list[str] = []
    in_flow: set[str] = set()
    for e in edges:
        caller = str(e.get("caller", ""))
        callee = str(e.get("callee", ""))
        if caller and caller not in in_flow:
            flow.append(caller)
            in_flow.add(caller)
        if callee and callee not in in_flow:
            flow.append(callee)
            in_flow.add(callee)
    # 孤立命中符号（无边）追加
    for s in sorted(hit_sym_names):
        if s not in in_flow:
            flow.append(s)
            in_flow.add(s)
    if not flow:
        return [], []

    evidence: list[str] = []
    file_by_symbol: dict[str, str] = {}
    for sym in index.symbols:
        if sym.get("name") not in file_by_symbol and sym.get("file"):
            file_by_symbol[str(sym.get("name"))] = str(sym.get("file"))
    meta = {f.path: f for f in index.files}
    for step in flow:
        f = file_by_symbol.get(step, "")
        if f:
            cs = str(meta[f].compile_status) if f in meta else "unknown"
            evidence.append(f"{step} defined in {f} (compile_status={cs})")
        else:
            evidence.append(f"{step} defined")
    for e in edges:
        evidence.append(f"{e.get('caller')} -> {e.get('callee')}")
    return flow, evidence


def search_architecture_legacy(index: ProjectIndex, task: str) -> dict[str, Any]:
    result = search_architecture(index, task)
    result["recommended_next_action"] = result["recommended_action"].get("type", NEXT_READ_SOURCE)
    return result
