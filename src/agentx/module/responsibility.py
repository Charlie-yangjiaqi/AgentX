"""Module Responsibilities 理解层（Phase 7.7.2）。

架构（设计约束）：
- 事实层（index.modules）不被污染：responsibility 独立存储
- 存储：<项目>_codebase_index/module_responsibilities.json（理解资产，
  Index rebuild 不破坏）
- confidence/evidence 由 scorer 确定性生成；LLM 只能写 description
  （"只解释证据，不创建知识"）
- 失效机制：facts_snapshot 对比（当前 facts ≠ 快照 → stale → 增量重生成）
- planner 消费：high 直接提供 / medium 标注 [推断] / low+null 不进入规划

生成范围（不追求全模块覆盖）：
- high/medium 且有 stale/缺失 → LLM 批量生成
- low → fallback 保守描述（首次写入，不花 LLM token）
- 已知模块（有效快照）→ 复用，不重新生成
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from agentx.index.index import index_dir
from agentx.module.scorer import score_module, snapshots_match

RESPONSIBILITIES_FILENAME = "module_responsibilities.json"
_LLM_BATCH_SIZE = 6
_CONFIDENCE_ORDER = {"high": 0, "medium": 1, "low": 2}

# LLM 输出 JSON 提取（允许被 ```json 包裹）
_JSON_RE = re.compile(r"\{.*\}", re.DOTALL)


def responsibilities_path(project_root: Path) -> Path:
    return index_dir(project_root) / RESPONSIBILITIES_FILENAME


def load_responsibilities(project_root: Path) -> dict[str, dict[str, Any]]:
    """module_id → entry；文件不存在/损坏返回空（理解资产可缺失，不失败）。"""
    p = responsibilities_path(project_root)
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        entries = data.get("entries", [])
        if not isinstance(entries, list):
            return {}
        return {
            str(e.get("module_id", "")): e
            for e in entries
            if isinstance(e, dict) and e.get("module_id")
        }
    except Exception:
        return {}


def save_responsibilities(project_root: Path, entries: dict[str, dict[str, Any]]) -> Path:
    p = responsibilities_path(project_root)
    p.parent.mkdir(parents=True, exist_ok=True)
    ordered = sorted(entries.values(), key=lambda e: str(e.get("module_id", "")))
    data = {
        "schema": "module_responsibilities",
        "generated_at": datetime.now(UTC).isoformat(),
        "entries": ordered,
    }
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return p


# ---------- stale 判定（facts snapshot 对比） ----------


def stale_module_ids(
    modules: list[dict[str, Any]],
    entries: dict[str, dict[str, Any]],
) -> set[str]:
    """当前 facts 与快照不一致（或缺失）的模块 id 集合。

    - 缺失：从未生成（首次）
    - 不一致：facts 变化（增量重生成目标）
    - 一致：复用
    """
    stale: set[str] = set()
    for mod in modules:
        mid = str(mod.get("name", ""))
        entry = entries.get(mid)
        if entry is None:
            stale.add(mid)
            continue
        current = score_module(mod)["snapshot"]
        stored = entry.get("facts_snapshot") or {}
        if not snapshots_match(current, stored):
            stale.add(mid)
    return stale


def entry_status(mod: dict[str, Any], entry: dict[str, Any] | None) -> str:
    """单个模块的 responsibility 状态：valid | stale | missing。

    stale = 快照不一致（判断依据已变，不能继续使用）
    """
    if entry is None:
        return "missing"
    current = score_module(mod)["snapshot"]
    if snapshots_match(current, entry.get("facts_snapshot") or {}):
        return "valid"
    return "stale"


# ---------- fallback（确定性保守描述，零 LLM） ----------


def fallback_responsibility(mod: dict[str, Any], confidence: str | None = None) -> dict[str, Any]:
    """保守描述：只陈述事实构成，不产生业务结论（防幻觉）。

    confidence 沿用 scorer 的评分（LLM 失败 ≠ 证据变弱）：
    "包含 2 个入口接口、14 个符号，依赖 [Store, Comm] 的模块"
    """
    entry_points = [str(x) for x in mod.get("entry_points", []) or []]
    dependencies = [str(x) for x in mod.get("dependencies", []) or []]
    consumers = [str(x) for x in mod.get("consumers", []) or []]
    symbol_count = len(mod.get("symbols", []) or [])
    parts = []
    if entry_points:
        parts.append(f"{len(entry_points)} 个入口接口")
    if symbol_count:
        parts.append(f"{symbol_count} 个符号")
    parts.append("的业务模块" if parts else "模块")
    desc = "包含 " + "、".join(parts)
    if dependencies:
        desc += f"，依赖 [{', '.join(dependencies[:5])}]"
    if consumers:
        desc += f"，被 [{', '.join(consumers[:5])}] 调用"
    return {
        "module_id": str(mod.get("name", "")),
        "responsibility": desc,
        "confidence": confidence or score_module(mod)["confidence"],
        "generated_by": "fallback",
        "generated_at": datetime.now(UTC).isoformat(),
        "facts_snapshot": score_module(mod)["snapshot"],
    }


# ---------- LLM 批量生成（只解释证据，不创建知识） ----------

_BATCH_PROMPT = (
    "你是 AgentX 的模块职责分析师。任务：根据每个模块的【事实证据】，用一句话"
    "描述该模块可能负责什么。\n\n"
    "硬性约束（违反即拒绝）：\n"
    "1. 只能解释证据中出现的符号/模块/依赖，禁止创造证据中不存在的模块名、"
    "符号名或业务能力（如：没有通信相关证据就不能写'通信'）\n"
    "2. 证据不足（只有路径/文件名）时，输出空字符串，不要猜测\n"
    "3. 输出必须是 JSON 对象，键=模块名，值=一句话职责（或空字符串）：\n"
    '{"ModuleA": "负责xxx管理", "ModuleB": ""}\n'
    "4. 不要输出任何额外文字\n\n"
    "模块证据：\n"
)


def _module_prompt_block(mod: dict[str, Any], score: dict[str, Any]) -> str:
    evidence = score["evidence"]
    lines = [f"=== {mod.get('name')} ==="]
    if evidence:
        lines.extend(f"  {e}" for e in evidence)
    else:
        lines.append("  （无任何关系证据，只有模块名/路径）")
    return "\n".join(lines)


def _parse_batch_output(content: str, modules: list[dict[str, Any]]) -> dict[str, str]:
    """解析 LLM 批量输出：{module_name: description}。

    白名单校验：description 中出现的标识符（模块名/符号）必须来自
    该模块的 evidence（符号/依赖/消费者），越界内容拒绝（降级）。
    """
    text = content.strip()
    m = _JSON_RE.search(text)
    if m is None:
        return {}
    try:
        data = json.loads(m.group(0))
    except json.JSONDecodeError:
        return {}
    if not isinstance(data, dict):
        return {}

    out: dict[str, str] = {}
    for mod in modules:
        mid = str(mod.get("name", ""))
        raw = data.get(mid)
        if not isinstance(raw, str):
            continue
        desc = raw.strip()
        if not desc:
            continue
        if _description_ok(mod, desc):
            out[mid] = desc
    return out


def _description_ok(mod: dict[str, Any], desc: str) -> bool:
    """白名单校验：描述中不能出现证据外的模块/符号名。

    校验对象：长标识符（≥3 字符的驼峰/下划线 token），防 LLM 编造
    不存在的模块（"依赖 Storage 模块" 但 Storage 不在证据里）。
    """
    allowed = set()
    for ep in mod.get("entry_points", []) or []:
        allowed.add(str(ep))
    for s in mod.get("symbols", []) or []:
        allowed.add(str(s))
    for c in mod.get("consumers", []) or []:
        allowed.add(str(c))
    for d in mod.get("dependencies", []) or []:
        allowed.add(str(d))
    if not allowed:
        return False  # 无任何证据模块：LLM 不可写业务结论（fallback 处理）
    tokens = re.findall(r"[A-Za-z_][A-Za-z0-9_]{2,}", desc)
    for tok in tokens:
        if tok in allowed:
            continue
        # 符号族前缀引用（HMI 是 HMIStore_Init / HMI_Service 的前缀）：
        # 嵌入式工程符号族常无下划线分词，前缀引用不算编造
        if any(a.startswith(tok) for a in allowed):
            continue
        if tok in {"负责", "管理", "处理"}:
            continue
        # 含大写字母的 token 视为标识符/专有名词：必须在白名单或前缀族内，
        # 否则拒绝（防 LLM 编造 Storage 等不存在的模块名）
        if any(ch.isupper() for ch in tok):
            return False
    return True


async def generate_module_responsibilities(
    app: Any,
    project_root: Path,
    modules: list[dict[str, Any]] | None = None,
    *,
    force: bool = False,
    hit_modules: set[str] | None = None,
    progress: Any = None,
) -> dict[str, Any]:
    """理解层保障：增量生成/刷新 module responsibilities。

    返回 {"status": "generated"|"reused"|"fallback"|"skipped", "llm_count": N,
          "fallback_count": M, "message": str}

    流程：
    1. scorer 全量评分（零 LLM）
    2. stale 判定（force → 全部重生成；否则仅 facts 变化的模块）
    3. high/medium 且 stale/缺失 → LLM 批量生成（失败降级 fallback）；
       hit_modules 提供时（plan 按需）仅命中模块走 LLM，未命中保留旧条目
    4. low → fallback 保守描述（首次写入；已存在且快照有效则保留）
    5. 保存（独立理解资产文件）
    """
    if modules is None:
        from agentx.index.index import load_index

        index = load_index(project_root)
        if index is None:
            return {"status": "skipped", "message": "Index 不存在，先建立 Index"}
        modules = index.modules or []
    if not modules:
        return {"status": "skipped", "message": "无模块（Index 无 Module Knowledge）"}

    entries = load_responsibilities(project_root)
    stale = (
        stale_module_ids(modules, entries)
        if not force
        else {str(m.get("name", "")) for m in modules}
    )

    llm_generated = 0
    fallback_written = 0
    # 分组：仅 high/medium 且 stale/缺失 的模块进 LLM 队列
    llm_queue: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for mod in modules:
        mid = str(mod.get("name", ""))
        score = score_module(mod)
        entry = entries.get(mid)
        if score["confidence"] == "low":
            if mid not in stale and entry is not None:
                continue  # 已有有效条目：保留
            entries[mid] = fallback_responsibility(mod)
            fallback_written += 1
            continue
        if mid not in stale:
            continue  # 有效快照：复用
        if hit_modules is not None and mid not in hit_modules:
            continue  # 按需：未命中模块不花 LLM token（保留旧条目/缺失）
        llm_queue.append((mod, score))

    for i in range(0, len(llm_queue), _LLM_BATCH_SIZE):
        batch = llm_queue[i : i + _LLM_BATCH_SIZE]
        descriptions = await _llm_batch(app, batch)
        for mod, _ in batch:
            mid = str(mod.get("name", ""))
            desc = descriptions.get(mid)
            if desc:
                entries[mid] = {
                    "module_id": mid,
                    "responsibility": desc,
                    "confidence": score_module(mod)["confidence"],
                    "generated_by": "llm",
                    "generated_at": datetime.now(UTC).isoformat(),
                    "facts_snapshot": score_module(mod)["snapshot"],
                }
                llm_generated += 1
            else:
                # LLM 无输出/校验失败 → fallback（不丢失、不编造）
                entries[mid] = fallback_responsibility(mod)
                fallback_written += 1

    save_responsibilities(project_root, entries)
    if progress is not None:
        progress(
            f"Module Responsibilities: llm={llm_generated} fallback={fallback_written} "
            f"total={len(entries)}"
        )
    status = "generated" if llm_generated else ("fallback" if fallback_written else "reused")
    return {
        "status": status,
        "llm_count": llm_generated,
        "fallback_count": fallback_written,
        "total": len(entries),
        "message": (
            f"模块职责: LLM 生成 {llm_generated}，fallback {fallback_written}，总计 {len(entries)}"
        ),
    }


async def _llm_batch(
    app: Any,
    batch: list[tuple[dict[str, Any], dict[str, Any]]],
) -> dict[str, str]:
    """LLM 批量生成一批模块职责；任何异常返回空（调用方 fallback）。"""
    try:
        from agentx.core.orchestrator import _env_hint
        from agentx.providers.messages import ChatMessage

        runtime = app.orchestrator.agents.get("plan")
        if runtime is None:
            return {}
        blocks = [_module_prompt_block(mod, score) for mod, score in batch]
        messages = [
            ChatMessage(role="user", content=_BATCH_PROMPT),
            ChatMessage(role="user", content=_env_hint()),
            ChatMessage(role="user", content="\n\n".join(blocks)),
        ]
        ctx = app.orchestrator._ctx(app._dummy_task())
        result = await runtime.run(messages, ctx)
        return _parse_batch_output(result.content or "", [mod for mod, _ in batch])
    except Exception:
        return {}


# ---------- planner 消费（high 直接 / medium [推断] / low+null 不提供） ----------


def format_responsibilities_for_planning(
    modules: list[dict[str, Any]],
    entries: dict[str, dict[str, Any]],
    hit_names: list[str],
) -> str:
    """Planner 上下文：命中模块的 responsibility 视图。

    规则：
    - high：直接提供（"负责xxx"）
    - medium：提供 + 标注 [推断]
    - low/null/缺失：不提供（禁止作为修改依据）
    """
    if not hit_names:
        return ""
    lines: list[str] = []
    for mid in hit_names:
        mod = next((m for m in modules if str(m.get("name", "")) == mid), None)
        entry = entries.get(mid)
        if mod is None or entry is None:
            continue
        confidence = str(entry.get("confidence", ""))
        if confidence not in ("high", "medium"):
            continue  # low/null/缺失：不进入规划上下文
        desc = str(entry.get("responsibility", "")).strip()
        if not desc:
            continue
        if entry.get("generated_by") == "llm":
            lines.append(
                f"  {mid}: {desc} [职责置信度={confidence}]"
                + ("（推断）" if confidence == "medium" else "")
            )
        else:
            # fallback 保守描述：明确标注为事实构成而非职责结论
            lines.append(f"  {mid}: {desc} [仅事实构成，未做职责判断]")
    if not lines:
        return ""
    return "模块职责（Module Responsibilities）:\n" + "\n".join(lines)
