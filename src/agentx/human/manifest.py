"""Human Index manifest：记录每份人类文档的知识来源与依赖，支持可靠增量刷新。

设计原则（Phase 8.3）：
- Markdown 是输出，不是事实数据库
- manifest 记录 document → knowledge_sources / modules / knowledge_dependencies，
  刷新判断 = changed knowledge ∩ document dependency，而不是只看文件
- generated_from / source_fingerprint / build_scope_fingerprint 记录生成基线，
  与 index.json 自带指纹一致（不重复维护另一套指纹）

存放：<project>_codebase_index/human/manifest.json
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

MANIFEST_FILENAME = "manifest.json"

DOC_PROJECT_OVERVIEW = "PROJECT_OVERVIEW.md"
DOC_ARCHITECTURE = "ARCHITECTURE.md"
DOC_MODULES = "MODULES.md"

ALL_DOCUMENTS = [DOC_PROJECT_OVERVIEW, DOC_ARCHITECTURE, DOC_MODULES]

# 每份文档的知识依赖声明（knowledge_dependencies 静态定义 + 生成时按 Index 动态补充）
# relations: 变化会触发该文档刷新的关系层
_DOC_KNOWLEDGE_DEPS: dict[str, dict[str, Any]] = {
    DOC_PROJECT_OVERVIEW: {
        "modules": [],  # 动态填充（high/medium 模块）
        "relations": ["build_scope", "module_set", "entry_points"],
        "build_scope": True,
        "type_semantics": False,
        "understanding": True,
    },
    DOC_ARCHITECTURE: {
        "modules": [],
        "relations": ["module_dependencies", "call_graph", "indirect_calls", "build_scope"],
        "build_scope": True,
        "type_semantics": True,
        "understanding": True,
    },
    DOC_MODULES: {
        "modules": [],
        "relations": ["module_dependencies", "call_graph", "module_files"],
        "build_scope": True,
        "type_semantics": True,
        "understanding": False,
    },
}


def human_dir(project_root: Path) -> Path:
    from agentx.index.index import index_dir

    return index_dir(project_root) / "human"


def manifest_path(project_root: Path) -> Path:
    return human_dir(project_root) / MANIFEST_FILENAME


def _empty_manifest() -> dict[str, Any]:
    return {
        "schema": "human_index_manifest",
        "generated_at": datetime.now(UTC).isoformat(),
        "source_fingerprint": None,
        "build_scope_fingerprint": None,
        "scope_fingerprint": None,
        "documents": {},
    }


def load_manifest(project_root: Path) -> dict[str, Any]:
    p = manifest_path(project_root)
    if not p.exists():
        return _empty_manifest()
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or not isinstance(data.get("documents"), dict):
            return _empty_manifest()
        return data
    except Exception:
        return _empty_manifest()


def save_manifest(project_root: Path, manifest: dict[str, Any]) -> Path:
    p = manifest_path(project_root)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return p


def document_dependency_decl(
    doc_name: str, index: Any = None
) -> dict[str, Any]:
    """返回文档的知识依赖声明（静态 + 动态模块清单）。

    modules：静态为空时由调用方在生成时写入实际覆盖模块。
    """
    decl = dict(_DOC_KNOWLEDGE_DEPS.get(doc_name, {}))
    return decl


def record_document(
    manifest: dict[str, Any],
    doc_name: str,
    *,
    modules: list[str],
    knowledge_sources: list[str],
    deps: dict[str, Any],
) -> None:
    """记录一份文档的生成信息到 manifest。"""
    docs = manifest.setdefault("documents", {})
    docs[doc_name] = {
        "generated_from": "index",
        "generated_at": datetime.now(UTC).isoformat(),
        "modules": sorted(set(modules)),
        "knowledge_sources": sorted(set(knowledge_sources)),
        "knowledge_dependencies": deps,
    }


def touch_manifest_baseline(
    manifest: dict[str, Any],
    *,
    source_fingerprint: str | None,
    build_scope_fingerprint: str | None,
    scope_fingerprint: str | None,
) -> None:
    """记录本次生成基线指纹（来自 ProjectIndex 本身，不重复计算）。"""
    manifest["source_fingerprint"] = source_fingerprint
    manifest["build_scope_fingerprint"] = build_scope_fingerprint
    manifest["scope_fingerprint"] = scope_fingerprint
    manifest["generated_at"] = datetime.now(UTC).isoformat()


def stale_documents(
    project_root: Path,
    index: Any,
    *,
    changed_modules: list[str] | None = None,
    changed_relations: list[str] | None = None,
    force: bool = False,
) -> list[str]:
    """按 knowledge_dependencies 判断哪些文档需要刷新。

    规则：
    - force → 全部
    - manifest 无基线/指纹与 Index 不一致 → 全部（首次或大基线变化）
    - changed_modules ∩ doc.modules 非空 → 刷新
    - changed_relations ∩ doc.relations 非空 → 刷新
    - doc 依赖 build_scope 且 build_scope_fingerprint 变化 → 刷新
    - doc 依赖 type_semantics 且 type 层变化（source fp 变化但非模块文件）保守刷新
    """
    manifest = load_manifest(project_root)
    if force:
        return list(ALL_DOCUMENTS)

    # 基线缺失/指纹不一致 → 全部刷新
    base = manifest.get("source_fingerprint")
    cur_src = getattr(index, "source_fingerprint", None)
    if base is None or cur_src is None or base != cur_src:
        return list(ALL_DOCUMENTS)

    docs = manifest.get("documents", {})
    cm = set(changed_modules or [])
    cr = set(changed_relations or [])
    out: list[str] = []
    for doc in ALL_DOCUMENTS:
        entry = docs.get(doc)
        if entry is None:
            out.append(doc)  # 从未生成
            continue
        deps = entry.get("knowledge_dependencies") or {}
        mods = set(deps.get("modules", []) or [])
        if cm & mods:
            out.append(doc)
            continue
        relations = set(deps.get("relations", []) or [])
        if cr & relations:
            out.append(doc)
            continue
        # build_scope 依赖：仅当指纹基线不匹配（build 层真实变化）
        if deps.get("build_scope"):
            base_bs = manifest.get("build_scope_fingerprint")
            cur_bs = getattr(index, "build_scope_fingerprint", None)
            if base_bs is not None and cur_bs is not None and base_bs != cur_bs:
                out.append(doc)
    return out


def doc_generated(project_root: Path, doc_name: str) -> bool:
    p = human_dir(project_root) / doc_name
    return p.exists()


__all__ = [
    "MANIFEST_FILENAME",
    "DOC_PROJECT_OVERVIEW",
    "DOC_ARCHITECTURE",
    "DOC_MODULES",
    "ALL_DOCUMENTS",
    "human_dir",
    "manifest_path",
    "load_manifest",
    "save_manifest",
    "record_document",
    "touch_manifest_baseline",
    "stale_documents",
    "doc_generated",
    "document_dependency_decl",
]
