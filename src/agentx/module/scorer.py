"""Module Responsibility Scorer（Phase 7.7.2 确定性评分层，零 LLM）。

把 module facts（entry_points/dependencies/consumers/symbols/files）转成：

- confidence：high | medium | low（证据强度决定，LLM 不参与）
- evidence：可追溯的证据列表（scorer 生成，LLM 不产生 evidence）
- snapshot：facts 快照（stale 判定依据，见 responsibility.py）

置信度规则（设计约束：confidence 不由 LLM 决定）：
- high：多个入口函数 + 调用关系（consumers）+ 依赖关系（dependencies）同时支持
- medium：入口函数 / 调用关系 / 依赖关系任一存在（函数命名或结构支持）
- low：仅模块名/文件路径（无任何关系证据）
"""

from __future__ import annotations

from typing import Any


def _snapshot(mod: dict[str, Any]) -> dict[str, Any]:
    """facts 快照：stale 判定依据（无序比较）。"""
    return {
        "entry_points": sorted(str(x) for x in mod.get("entry_points", []) or []),
        "dependencies": sorted(str(x) for x in mod.get("dependencies", []) or []),
        "consumers": sorted(str(x) for x in mod.get("consumers", []) or []),
        "symbol_count": len(mod.get("symbols", []) or []),
    }


def _symbol_prefix_family(symbols: list[str]) -> bool:
    """符号命名族：≥3 符号共享首 token 且占比 ≥50%（函数命名支持证据）。

    嵌入式工程符号族常无下划线分词（HMIStore_Init），首 token 按
    '_' 分割；共享族说明模块内函数围绕同一业务命名。
    """
    if len(symbols) < 3:
        return False
    counts: dict[str, int] = {}
    for s in symbols:
        tok = s.split("_", 1)[0].casefold()
        if len(tok) >= 2 and tok[0].isalpha():
            counts[tok] = counts.get(tok, 0) + 1
    if not counts:
        return False
    return max(counts.values()) >= 3 and max(counts.values()) / len(symbols) >= 0.5


def score_module(mod: dict[str, Any]) -> dict[str, Any]:
    """对单个模块评分：返回 {"confidence", "evidence", "snapshot"}。

    evidence 顺序：entry_point → symbol → consumer → dependency → path
    （证据按价值排序，LLM 生成时可见）

    high（证据充分，任一）：
    a) ≥2 入口 + 调用关系 + 依赖关系
    b) ≥3 入口 + 任一关系
    c) ≥1 入口 + ≥2 消费者 + ≥1 依赖
    d) ≥1 入口 + ≥2 依赖 + ≥10 符号
    e) ≥1 入口 + 任一关系 + 符号命名族 + ≥10 符号
    f) 无入口但 ≥3 依赖 + ≥1 消费者 + ≥10 符号（强关系无入口）

    medium：任一 entry_points/consumers/dependencies 存在
    low：仅模块名/路径
    """
    entry_points = [str(x) for x in mod.get("entry_points", []) or []]
    symbols = [str(x) for x in mod.get("symbols", []) or []]
    consumers = [str(x) for x in mod.get("consumers", []) or []]
    dependencies = [str(x) for x in mod.get("dependencies", []) or []]
    files = [str(x) for x in mod.get("files", []) or []]

    evidence: list[str] = []
    for ep in entry_points:
        evidence.append(f"entry_point:{ep}")
    for s in symbols[:10]:  # 符号过多只引前 10 个（证据可读性）
        evidence.append(f"symbol:{s}")
    for c in consumers:
        evidence.append(f"consumer:{c}")
    for d in dependencies:
        evidence.append(f"dependency:{d}")
    if files:
        evidence.append(f"path:{files[0].replace('\\\\', '/').split('/')[0]}")

    n_ep = len(entry_points)
    n_cons = len(consumers)
    n_deps = len(dependencies)
    n_syms = len(symbols)
    fam = _symbol_prefix_family(symbols)

    if n_ep >= 2 and n_cons >= 1 and n_deps >= 1:
        confidence = "high"  # a
    elif n_ep >= 3 and (n_cons >= 1 or n_deps >= 1):
        confidence = "high"  # b
    elif n_ep >= 1 and n_cons >= 2 and n_deps >= 1:
        confidence = "high"  # c
    elif n_ep >= 1 and n_deps >= 2 and n_syms >= 10:
        confidence = "high"  # d
    elif n_ep >= 1 and (n_cons >= 1 or n_deps >= 1) and fam and n_syms >= 10:
        confidence = "high"  # e
    elif n_ep == 0 and n_deps >= 3 and n_cons >= 1 and n_syms >= 10:
        confidence = "high"  # f
    elif n_ep or n_cons or n_deps:
        confidence = "medium"
    else:
        confidence = "low"

    return {
        "confidence": confidence,
        "evidence": evidence,
        "snapshot": _snapshot(mod),
    }


def snapshots_match(current: dict[str, Any], stored: dict[str, Any]) -> bool:
    """facts 快照一致性：列表无序比较（顺序变化不算事实变化）。"""
    for key in ("entry_points", "dependencies", "consumers"):
        if sorted(str(x) for x in current.get(key, []) or []) != sorted(
            str(x) for x in stored.get(key, []) or []
        ):
            return False
    return int(current.get("symbol_count", 0)) == int(stored.get("symbol_count", 0))
