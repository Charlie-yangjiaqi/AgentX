"""Phase 7.7.2 Module Responsibilities 测试。

覆盖（设计验收）：
1. 证据充分（多入口 + 调用 + 依赖）→ confidence=high + responsibility 非空
2. 证据不足（仅模块名）→ low + fallback 保守描述（无业务结论）
3. 禁止幻觉：无通信调用关系 → LLM 输出白名单外符号 → 拒绝 → fallback
4. stale 判定：facts 变化 → stale；不变 → valid
5. planner 消费：high 直接 / medium [推断] / low+null 不进入规划
6. 序列化：理解资产独立存储（不污染 index.modules）、roundtrip 安全
7. scorer 单元：各证据组合
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from agentx.module.responsibility import (
    fallback_responsibility,
    format_responsibilities_for_planning,
    generate_module_responsibilities,
    load_responsibilities,
    save_responsibilities,
    stale_module_ids,
)
from agentx.module.scorer import score_module


def _mod(
    name: str,
    entry_points: list[str] | None = None,
    deps: list[str] | None = None,
    consumers: list[str] | None = None,
    symbols: list[str] | None = None,
    files: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "name": name,
        "files": files or [f"{name.lower()}.c"],
        "symbols": symbols or [],
        "entry_points": entry_points or [],
        "dependencies": deps or [],
        "consumers": consumers or [],
    }


# ---------- 1. Scorer：置信度由证据强度决定（LLM 不参与） ----------


def test_scorer_high_multiple_entries_with_relations() -> None:
    """多入口 + 调用关系 + 依赖关系 → high。"""
    mod = _mod(
        "HMI_Store",
        entry_points=["HMIStore_Init", "HMIStore_Save", "HMIStore_Load"],
        deps=["NORFLASH", "Config"],
        consumers=["HMI_Service"],
        symbols=["HMIStore_Init", "HMIStore_Save", "HMIStore_Load", "HMIStore_Cache"],
    )
    score = score_module(mod)
    assert score["confidence"] == "high"
    assert any(e.startswith("entry_point:HMIStore_Init") for e in score["evidence"])
    assert any(e.startswith("consumer:HMI_Service") for e in score["evidence"])
    assert any(e.startswith("dependency:NORFLASH") for e in score["evidence"])
    assert score["snapshot"]["entry_points"] == ["HMIStore_Init", "HMIStore_Load", "HMIStore_Save"]


def test_scorer_medium_single_relation() -> None:
    """单一证据（仅有 consumers）→ medium（函数命名/结构支持）。"""
    score = score_module(_mod("LCD", consumers=["UI"], symbols=["lcd_init", "lcd_write"]))
    assert score["confidence"] == "medium"


def test_scorer_low_name_only() -> None:
    """仅模块名/路径 → low（无任何关系证据）。"""
    score = score_module(_mod("Misc"))
    assert score["confidence"] == "low"
    assert score["evidence"]  # path 证据存在但不足以上调置信度


def test_scorer_high_three_entries_only() -> None:
    """≥3 入口 + 任一关系 → high（多入口强信号）。"""
    mod = _mod(
        "Comm",
        entry_points=["Comm_Init", "Comm_Tick", "Comm_Flush"],
        deps=["HAL"],
        symbols=["Comm_Init", "Comm_Tick", "Comm_Flush"],
    )
    assert score_module(mod)["confidence"] == "high"


# ---------- 2. fallback：保守描述，禁止业务结论 ----------


def test_fallback_no_business_claim() -> None:
    """fallback 只陈述事实构成，不生成业务结论（如"通信""参数管理"）。"""
    mod = _mod(
        "HMI_Comm",
        entry_points=["HMIComm_Init", "HMIComm_Tick"],
        deps=["UART"],
        consumers=["HMI_Service"],
        symbols=["HMIComm_Init", "HMIComm_Tick", "HMIComm_Send"],
    )
    fb = fallback_responsibility(mod)
    assert fb["confidence"] == "high"  # 沿用 scorer 评分（LLM 失败≠证据变弱）
    assert fb["generated_by"] == "fallback"
    assert "2 个入口接口" in fb["responsibility"]
    assert "依赖 [UART]" in fb["responsibility"]
    assert "被 [HMI_Service] 调用" in fb["responsibility"]
    # 不得出现业务性结论（"负责通信"）
    assert "负责" not in fb["responsibility"]
    assert "通信" not in fb["responsibility"]


def test_fallback_low_module_keeps_low() -> None:
    """low 证据模块：fallback confidence 仍为 low。"""
    fb = fallback_responsibility(_mod("Misc", files=["misc.c"]))
    assert fb["confidence"] == "low"


# ---------- 3. 禁止幻觉：LLM 文本白名单校验 ----------


def test_parse_batch_output_rejects_outside_symbols() -> None:
    """LLM 输出含白名单外模块名（如凭空出现 Storage）→ 拒绝该模块。"""
    from agentx.module.responsibility import _parse_batch_output

    mod = _mod(
        "Comm",
        entry_points=["Comm_Init"],
        deps=["HAL"],
        symbols=["Comm_Init", "Comm_Send"],
    )
    # 幻觉：描述引用不存在于证据的 "Storage"
    content = json.dumps({"Comm": "负责数据存储管理，依赖 Storage 模块"})
    out = _parse_batch_output(content, [mod])
    assert "Comm" not in out  # 拒绝


def test_parse_batch_output_accepts_evidence_symbols() -> None:
    """描述中符号均来自证据 → 接受。"""
    from agentx.module.responsibility import _parse_batch_output

    mod = _mod(
        "Comm",
        entry_points=["Comm_Init"],
        deps=["HAL"],
        consumers=["HMI_Service"],
        symbols=["Comm_Init", "Comm_Send"],
    )
    content = json.dumps({"Comm": "负责串口收发处理，被 HMI_Service 调用"})
    out = _parse_batch_output(content, [mod])
    assert out.get("Comm") == "负责串口收发处理，被 HMI_Service 调用"


def test_parse_batch_output_no_evidence_rejected() -> None:
    """无任何证据模块：LLM 输出一律拒绝（宁可缺失不猜）。"""
    from agentx.module.responsibility import _parse_batch_output

    mod = _mod("Misc", files=["misc.c"])
    content = json.dumps({"Misc": "负责系统控制"})
    out = _parse_batch_output(content, [mod])
    assert out == {}


def test_parse_batch_output_bad_json_empty() -> None:
    """LLM 输出非 JSON / 空 → 空结果（fallback 兜底）。"""
    from agentx.module.responsibility import _parse_batch_output

    mod = _mod("A", entry_points=["A_Init"], consumers=["B"])
    assert _parse_batch_output("这不是 JSON", [mod]) == {}
    assert _parse_batch_output("", [mod]) == {}


# ---------- 4. stale 判定（facts snapshot） ----------


def test_stale_detection_facts_changed() -> None:
    """dependencies 变化 → stale；不变 → 复用。"""
    entries = {
        "Comm": {
            "module_id": "Comm",
            "responsibility": "负责串口收发",
            "confidence": "high",
            "generated_by": "llm",
            "facts_snapshot": {
                "entry_points": ["Comm_Init"],
                "dependencies": ["HAL"],
                "consumers": [],
                "symbol_count": 2,
            },
        }
    }
    # 事实未变 → 无 stale
    same = _mod(
        "Comm", entry_points=["Comm_Init"], deps=["HAL"], symbols=["Comm_Init", "Comm_Send"]
    )
    assert stale_module_ids([same], entries) == set()
    # 依赖变化（UART 加入）→ stale
    changed = _mod(
        "Comm", entry_points=["Comm_Init"], deps=["HAL", "UART"], symbols=["Comm_Init", "Comm_Send"]
    )
    assert stale_module_ids([changed], entries) == {"Comm"}


def test_stale_detection_entry_order_insensitive() -> None:
    """entry_points 顺序变化不算事实变化（无序比较）。"""
    entries = {
        "M": {
            "module_id": "M",
            "facts_snapshot": {
                "entry_points": ["A", "B"],
                "dependencies": [],
                "consumers": [],
                "symbol_count": 2,
            },
        }
    }
    mod = _mod("M", entry_points=["B", "A"], symbols=["a", "b"])
    assert stale_module_ids([mod], entries) == set()


def test_missing_entry_is_stale() -> None:
    mod = _mod("New", entry_points=["New_Init"])
    assert stale_module_ids([mod], {}) == {"New"}


# ---------- 5. planner 消费规则 ----------


def test_planning_context_high_direct_medium_inferred_low_hidden() -> None:
    modules = [
        _mod("HMI_Store", entry_points=["HMIStore_Init"], consumers=["Svc"]),
        _mod("LCD", consumers=["UI"]),
        _mod("Misc", files=["misc.c"]),
    ]
    entries = {
        "HMI_Store": {
            "module_id": "HMI_Store",
            "responsibility": "负责参数管理",
            "confidence": "high",
            "generated_by": "llm",
            "facts_snapshot": {},
        },
        "LCD": {
            "module_id": "LCD",
            "responsibility": "负责显示渲染",
            "confidence": "medium",
            "generated_by": "llm",
            "facts_snapshot": {},
        },
        "Misc": fallback_responsibility(_mod("Misc", files=["misc.c"])),
    }
    text = format_responsibilities_for_planning(modules, entries, ["HMI_Store", "LCD", "Misc"])
    assert "负责参数管理 [职责置信度=high]" in text
    assert "负责显示渲染 [职责置信度=medium]（推断）" in text
    assert "Misc" not in text  # low/null/缺失：不进入规划上下文


def test_planning_context_missing_and_null_hidden() -> None:
    modules = [_mod("A", entry_points=["A_Init"]), _mod("B", entry_points=["B_Init"])]
    entries = {
        "A": {
            "module_id": "A",
            "responsibility": None,
            "confidence": "low",
            "generated_by": "fallback",
            "facts_snapshot": {},
        }
    }
    text = format_responsibilities_for_planning(modules, entries, ["A", "B"])
    assert "A" not in text and "B" not in text  # low/null/缺失全隐藏


def test_planning_context_empty_hit() -> None:
    assert format_responsibilities_for_planning([], {}, []) == ""


# ---------- 6. 存储：独立理解资产 + 序列化 ----------


def test_save_load_roundtrip(tmp_path: Path) -> None:
    entries = {
        "HMI_Store": {
            "module_id": "HMI_Store",
            "responsibility": "负责参数管理",
            "confidence": "high",
            "generated_by": "llm",
            "facts_snapshot": {
                "entry_points": ["Init"],
                "dependencies": [],
                "consumers": [],
                "symbol_count": 1,
            },
        }
    }
    save_responsibilities(tmp_path, entries)
    loaded = load_responsibilities(tmp_path)
    assert loaded["HMI_Store"]["responsibility"] == "负责参数管理"
    # 序列化安全：json roundtrip 无编码异常
    text = json.dumps(loaded, ensure_ascii=False)
    assert "负责参数管理" in text
    # 中文路径项目
    cn_root = tmp_path / "工程"
    save_responsibilities(cn_root, entries)
    assert load_responsibilities(cn_root)["HMI_Store"]["confidence"] == "high"


def test_facts_snapshot_not_polluting_modules() -> None:
    """理解资产独立于 index.modules（模块事实结构不被污染）。"""
    mod = _mod("A", entry_points=["A_Init"])
    score = score_module(mod)
    assert set(score["snapshot"].keys()) == {
        "entry_points",
        "dependencies",
        "consumers",
        "symbol_count",
    }
    # modules 原始结构不变（无 responsibility 字段）
    assert "responsibility" not in mod


# ---------- 7. 端到端：generate（LLM mock + fallback + 增量） ----------


class _FakeRuntime:
    def __init__(self, content: str) -> None:
        self._content = content

    async def run(self, messages: list[Any], ctx: Any) -> Any:
        return type("R", (), {"content": self._content})()


class _FakeApp:
    def __init__(self, content: str) -> None:
        self.orchestrator = type(
            "O",
            (),
            {"agents": {"plan": _FakeRuntime(content)}, "_ctx": lambda self, t: None},
        )()

    def _dummy_task(self) -> None:
        return None


@pytest.mark.asyncio
async def test_generate_high_module_llm(tmp_path: Path) -> None:
    """high 模块 + LLM 有效输出 → llm 生成 + 快照正确。"""
    mod = _mod(
        "HMI_Store",
        entry_points=["HMIStore_Init", "HMIStore_Save"],
        deps=["Storage"],
        consumers=["HMI_Service"],
        symbols=["HMIStore_Init", "HMIStore_Save"],
    )
    app = _FakeApp(json.dumps({"HMI_Store": "负责HMI参数状态管理"}))
    result = await generate_module_responsibilities(app, tmp_path, [mod])
    assert result["llm_count"] == 1
    entries = load_responsibilities(tmp_path)
    entry = entries["HMI_Store"]
    assert entry["confidence"] == "high"
    assert entry["generated_by"] == "llm"
    assert "参数" in entry["responsibility"]
    assert entry["facts_snapshot"]["entry_points"] == ["HMIStore_Init", "HMIStore_Save"]


@pytest.mark.asyncio
async def test_generate_low_module_fallback_no_llm(tmp_path: Path) -> None:
    """low 模块：不调 LLM，直接 fallback。"""
    mod = _mod("Misc", files=["misc.c"])
    app = _FakeApp("")
    result = await generate_module_responsibilities(app, tmp_path, [mod])
    assert result["llm_count"] == 0
    assert result["fallback_count"] == 1
    entry = load_responsibilities(tmp_path)["Misc"]
    assert entry["generated_by"] == "fallback"
    assert entry["confidence"] == "low"


@pytest.mark.asyncio
async def test_generate_llm_hallucination_falls_back(tmp_path: Path) -> None:
    """LLM 输出幻觉（引用不存在模块）→ 拒绝 → fallback（不产生错误理解）。"""
    mod = _mod("Comm", entry_points=["Comm_Init"], deps=["HAL"], symbols=["Comm_Init"])
    app = _FakeApp(json.dumps({"Comm": "负责数据存储，依赖 Storage 模块"}))
    result = await generate_module_responsibilities(app, tmp_path, [mod])
    assert result["llm_count"] == 0
    entry = load_responsibilities(tmp_path)["Comm"]
    assert entry["generated_by"] == "fallback"
    assert "Storage" not in entry["responsibility"]


@pytest.mark.asyncio
async def test_generate_incremental_reuse_valid(tmp_path: Path) -> None:
    """快照有效 → 复用（不重新生成、不调 LLM）。"""
    mod = _mod(
        "Comm",
        entry_points=["Comm_Init"],
        deps=["HAL"],
        consumers=["HMI_Service"],
        symbols=["Comm_Init", "Comm_Send"],
    )
    save_responsibilities(
        tmp_path,
        {
            "Comm": {
                "module_id": "Comm",
                "responsibility": "负责串口收发",
                "confidence": "medium",
                "generated_by": "llm",
                "facts_snapshot": score_module(mod)["snapshot"],
            }
        },
    )
    app = _FakeApp(json.dumps({"Comm": "新的错误描述"}))
    result = await generate_module_responsibilities(app, tmp_path, [mod])
    assert result["llm_count"] == 0
    assert load_responsibilities(tmp_path)["Comm"]["responsibility"] == "负责串口收发"


@pytest.mark.asyncio
async def test_generate_incremental_stale_refreshed(tmp_path: Path) -> None:
    """facts 变化 → stale → 重生成。"""
    old = _mod("Comm", entry_points=["Comm_Init"], deps=["HAL"], symbols=["Comm_Init"])
    save_responsibilities(
        tmp_path,
        {
            "Comm": {
                "module_id": "Comm",
                "responsibility": "旧职责",
                "confidence": "high",
                "generated_by": "llm",
                "facts_snapshot": score_module(old)["snapshot"],
            }
        },
    )
    changed = _mod(
        "Comm",
        entry_points=["Comm_Init", "Comm_Tick"],
        deps=["HAL", "UART"],
        symbols=["Comm_Init"],
    )
    app = _FakeApp(json.dumps({"Comm": "负责串口与网络收发"}))
    result = await generate_module_responsibilities(app, tmp_path, [changed])
    assert result["llm_count"] == 1
    entry = load_responsibilities(tmp_path)["Comm"]
    assert entry["responsibility"] == "负责串口与网络收发"


@pytest.mark.asyncio
async def test_generate_hit_modules_filter(tmp_path: Path) -> None:
    """按需（hit_modules）：未命中模块不花 LLM token，但 low 仍 fallback。"""
    hit = _mod("Hit", entry_points=["Hit_Init"], consumers=["Svc"], symbols=["Hit_Init"])
    miss = _mod("Miss", entry_points=["Miss_Init"], deps=["HAL"], symbols=["Miss_Init"])
    app = _FakeApp(json.dumps({"Hit": "命中模块职责", "Miss": "不应生成"}))
    result = await generate_module_responsibilities(app, tmp_path, [hit, miss], hit_modules={"Hit"})
    assert result["llm_count"] == 1
    entries = load_responsibilities(tmp_path)
    assert entries["Hit"]["responsibility"] == "命中模块职责"
    assert "Miss" not in entries  # 未命中：不生成（宁可缺失）


@pytest.mark.asyncio
async def test_generate_force_full(tmp_path: Path) -> None:
    """force=True：全部重生成（含快照有效的）。"""
    mod = _mod("A", entry_points=["A_Init"], consumers=["B"], symbols=["A_Init"])
    save_responsibilities(
        tmp_path,
        {
            "A": {
                "module_id": "A",
                "responsibility": "旧",
                "confidence": "high",
                "generated_by": "llm",
                "facts_snapshot": score_module(mod)["snapshot"],
            }
        },
    )
    app = _FakeApp(json.dumps({"A": "新职责"}))
    result = await generate_module_responsibilities(app, tmp_path, [mod], force=True)
    assert result["llm_count"] == 1
    assert load_responsibilities(tmp_path)["A"]["responsibility"] == "新职责"
