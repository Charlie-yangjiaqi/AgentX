"""Semantic runtime / capabilities / Quality Gate 测试（Index Pipeline Reliability）。

Case 2: semantic 不可用 → SemanticUnavailableError → capabilities 显式 disabled +
         errors 记录 + Index 仍生成（不静默、不假成功）
+ runtime 状态 / query doctor 建议 / quality 评分
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from agentx.index.index import ProjectIndex, load_index
from agentx.plan.service import enrich_index
from agentx.quality import compute_quality, format_quality_report
from agentx.query.symbol import search_symbol
from agentx.semantic.runtime import (
    SemanticUnavailableError,
    format_runtime_status,
    semantic_runtime_status,
)
from agentx.understanding.graph import ProjectGraph


def _make_project(root: Path) -> None:
    (root / "main.c").write_text("int main(void) { return 0; }\n", encoding="utf-8")
    (root / "key.c").write_text(
        '#include "key.h"\nint key_scan(uint8_t mode) { return mode; }\n', encoding="utf-8"
    )
    (root / "key.h").write_text(
        "#ifndef KEY_H\n#define KEY_H\n#define KEY0_PIN 1\n"
        "typedef struct { int x; } pt_t;\n#endif\n",
        encoding="utf-8",
    )


def _codegraph_graph() -> ProjectGraph:
    return ProjectGraph(
        source="codegraph",
        files=[
            {"path": "main.c", "language": "c"},
            {"path": "key.c", "language": "c"},
            {"path": "key.h", "language": "c"},
        ],
        symbols=[
            {"name": "key_scan", "type": "function", "file": "key.c", "start_line": 2},
            {"name": "pt_t", "type": "struct", "file": "key.h", "start_line": 3},
        ],
        call_graph=[],
        include_map={},
        build_info={},
        errors=[],
    )


# ---------- runtime 状态 ----------


def test_runtime_status_enabled_in_dev_env() -> None:
    st = semantic_runtime_status()
    assert st["grammar"] == "c"
    assert st["parser"] in ("tree_sitter_c", "tree_sitter_language_pack")
    assert st["status"] == "enabled"
    text = format_runtime_status(st)
    assert "tree_sitter=" in text and "status=enabled" in text


def test_error_type_hierarchy() -> None:
    assert issubclass(SemanticUnavailableError, RuntimeError)


# ---------- Case 2: semantic 不可用 → 显式降级，不假成功 ----------


def test_enrich_semantic_unavailable_is_explicit(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    _make_project(tmp_path)
    monkeypatch.setattr("agentx.plan.service.analyze_project", lambda root: _codegraph_graph())
    from agentx.semantic import merge as merge_mod

    def _boom(*args, **kwargs):
        raise SemanticUnavailableError("tree_sitter 不可用（测试模拟）")

    monkeypatch.setattr(merge_mod, "merge_semantics", _boom)

    index, _ = enrich_index(tmp_path)
    # 1. Index 仍生成（不崩、不中断）
    assert index.file_count > 0
    # 2. capabilities 显式 disabled + reason
    caps = index.capabilities or {}
    sem = caps.get("semantic") or {}
    assert sem.get("enabled") is False
    assert "tree_sitter 不可用" in str(sem.get("reason"))
    # 3. errors 明确记录（不是静默）
    assert any("Semantic 不可用" in e for e in index.errors)
    # 4. 落盘后仍可读（状态持久化）
    reloaded = load_index(tmp_path)
    assert reloaded is not None
    assert (reloaded.capabilities or {}).get("semantic", {}).get("enabled") is False


def test_enrich_semantic_enabled_capabilities(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    _make_project(tmp_path)
    monkeypatch.setattr("agentx.plan.service.analyze_project", lambda root: _codegraph_graph())
    index, _ = enrich_index(tmp_path)
    sem = (index.capabilities or {}).get("semantic") or {}
    assert sem.get("enabled") is True
    assert sem.get("parser") == "tree_sitter_c"


# ---------- query：semantic disabled → doctor 建议 ----------


def test_query_doctor_recommendation_when_semantic_disabled() -> None:
    index = ProjectIndex(
        project_fingerprint="fp",
        index_version="1.5",
        generated_at=datetime(2026, 1, 1, tzinfo=UTC),
        files=[{"path": "key.c", "compile_status": "compiled"}],
        symbols=[
            {"name": "key_scan", "type": "function", "file": "key.c", "start_line": 1},
        ],
        capabilities={
            "semantic": {"enabled": False, "reason": "Semantic 不可用: tree_sitter 缺失"},
        },
    )
    result = search_symbol(index, "key_scan")
    assert result["recommended_action"]["type"] == "doctor"
    assert "Semantic unavailable" in result["recommended_action"]["reason"]

    # 无 capabilities 的旧 Index → 保持 reindex 建议（不破坏）
    index.capabilities = {}
    result2 = search_symbol(index, "key_scan")
    assert result2["recommended_action"]["type"] == "reindex"


# ---------- Quality Gate ----------


def _index_with_symbols(symbols: list[dict], caps: dict) -> ProjectIndex:
    return ProjectIndex(
        project_fingerprint="fp",
        index_version="1.5",
        generated_at=datetime(2026, 1, 1, tzinfo=UTC),
        files=[{"path": "key.c", "compile_status": "compiled"}],
        symbols=symbols,
        capabilities=caps,
    )


def test_quality_full_coverage_grade_a() -> None:
    index = _index_with_symbols(
        [
            {
                "name": "key_scan",
                "type": "function",
                "file": "key.c",
                "signature": {"text": "int key_scan(uint8_t mode)"},
                "semantic": True,
            },
            {
                "name": "pt_t",
                "type": "struct",
                "file": "key.h",
                "members": [{"name": "x", "type": "int"}],
                "semantic": True,
            },
            {
                "name": "KEY0_PIN",
                "type": "macro",
                "file": "key.h",
                "value": "1",
                "semantic": True,
            },
        ],
        {"semantic": {"enabled": True, "parser": "tree_sitter_c"}},
    )
    q = compute_quality(index)
    assert q["semantic"] == "enabled"
    assert q["functions_with_signature"] == 1
    assert q["structs_with_members"] == 1
    assert q["macros"] == 1
    assert q["rejected_tokens"] == 0
    assert q["grade"] == "A"
    text = format_quality_report(q)
    assert "AgentX Index Quality Report" in text
    assert "Quality: A" in text


def test_quality_semantic_disabled_grade_c() -> None:
    index = _index_with_symbols(
        [{"name": "key_scan", "type": "function", "file": "key.c"}],
        {"semantic": {"enabled": False, "reason": "Semantic 不可用"}},
    )
    q = compute_quality(index)
    assert q["semantic"] == "disabled"
    assert q["grade"] == "C"
    assert q["functions_with_signature"] == 0


def test_quality_rejected_tokens_downgrade() -> None:
    index = _index_with_symbols(
        [
            {"name": "(*TASKFUNCTION)", "type": "function", "file": "x.c"},
            {"name": "key_scan", "type": "function", "file": "key.c"},
        ],
        {"semantic": {"enabled": True}},
    )
    q = compute_quality(index)
    assert q["rejected_tokens"] == 1
    assert q["grade"] in ("B+", "B", "C")  # 污染降级，不可能是 A
