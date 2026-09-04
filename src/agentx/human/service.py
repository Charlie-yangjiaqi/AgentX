"""HumanKnowledgeService：Human Project Knowledge 生成/刷新/状态（Phase 8.3）。

编排层（不承载具体渲染逻辑）：
- generate：完整生成 3 份文档（PROJECT_OVERVIEW / ARCHITECTURE / MODULES）
- refresh：只刷新受影响的文档（依据 manifest knowledge_dependencies）
- status：查看 Human Index 状态（存在/基线/过期）
- 生命周期门：Index MISSING → 走现有 bootstrap；STALE → 交给 8.2 freshness
  （小自动增量/大 REINDEX_REQUIRED）；REINDEX_REQUIRED → 不偷跑 reindex，
  返回当前状态
- project_understanding 缺失 → 尝试 ensure_understanding 补齐；失败不阻塞，
  文档标记 Needs verification

LLM 职责仅限"语言组织"：
- 确定性生成器负责：模块表/依赖/Mermaid 图/入口/路径/Evidence/Traceability
- LLM 负责：Project Summary 散文 / Architecture Overview 散文 / 分层解释
  硬约束：LLM 只能解释 Bundle 中已存在的节点与关系，不能新增事实。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from agentx.human.bundle import collect_bundle
from agentx.human.docs import render_document
from agentx.human.manifest import (
    ALL_DOCUMENTS,
    DOC_ARCHITECTURE,
    DOC_MODULES,
    DOC_PROJECT_OVERVIEW,
    doc_generated,
    human_dir,
    load_manifest,
    record_document,
    save_manifest,
    stale_documents,
    touch_manifest_baseline,
)

PROSE_DOCS = {DOC_PROJECT_OVERVIEW, DOC_ARCHITECTURE}  # MODULES.md 用确定性渲染


class HumanKnowledgeService:
    """Human Index 编排服务。构造只需 project_root；app 用于 LLM 散文（可缺省降级）。"""

    def __init__(self, project_root: Path, app: Any = None) -> None:
        self.root = project_root.resolve()
        self.app = app

    # ---------- Index lifecycle 门 ----------

    def _ensure_index_ready(
        self,
        *,
        force_rebuild: bool = False,
        scope_selections: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Index 前置：MISSING → bootstrap；STALE → freshness 处理；VALID → 通过。

        返回 {"ok": True, "index": ProjectIndex, "note": ...} 或
        {"ok": False, "blocked": "reindex_required"|"scope_required"|"missing", ...}
        """
        from agentx.index.index import IndexStatus, index_status, load_index

        root = self.root
        status, reason = index_status(root)
        if status == IndexStatus.MISSING:
            # 走现有 bootstrap：ensure_index（scope gate 前置）+ enrich 补全认知
            from agentx.plan.service import enrich_index, ensure_index, is_skeleton_index

            pre, pre_reason, idx = ensure_index(
                root, force_rebuild=force_rebuild, scope_selections=scope_selections
            )
            if idx is None:
                gate_reason = pre_reason or "index_unavailable"
                if gate_reason == "scope_required":
                    from agentx.scope.initializer import check_scope_init

                    gate = check_scope_init(root) or {}
                    return {
                        "ok": False,
                        "blocked": "scope_required",
                        "message": gate.get("message", "Need user confirmation before index build"),
                        "suggestions": gate.get("suggestions", {}),
                        "index_status": "MISSING",
                    }
                if gate_reason == "build_target_required":
                    from agentx.scope.initializer import check_build_target_init

                    gate = check_build_target_init(root) or {}
                    return {
                        "ok": False,
                        "blocked": "build_target_required",
                        "message": gate.get("message", "Need user confirm Keil target"),
                        "build_targets": gate.get("build_targets", []),
                        "index_status": "MISSING",
                    }
                return {
                    "ok": False,
                    "blocked": "missing",
                    "message": f"Index 不可用: {pre_reason}",
                    "index_status": "MISSING",
                }
            # bootstrap 后若仍骨架 → enrich 补全
            cur = load_index(root)
            if cur is not None and is_skeleton_index(cur):
                enrich_index(root)
            index = load_index(root)
            if index is None:
                return {
                    "ok": False,
                    "blocked": "missing",
                    "message": "Index bootstrap 后仍不可读",
                    "index_status": "MISSING",
                }
            return {"ok": True, "index": index, "note": f"bootstrap: {pre_reason}"}

        if status == IndexStatus.CORRUPTED:
            return {
                "ok": False,
                "blocked": "reindex_required",
                "message": "Index 损坏，需 action=reindex 重建",
                "index_status": "CORRUPTED",
            }

        # VALID / STALE
        if status == IndexStatus.VALID:
            index = load_index(root)
            return {"ok": True, "index": index, "note": "VALID"}
        # STALE：按 8.2 freshness 处理（小自动增量 / 大 REINDEX_REQUIRED）
        from agentx.index.sync import sync_index

        sync_result = sync_index(root, origin="agentx_human_index")
        if sync_result.get("action") == "reindex_required":
            return {
                "ok": False,
                "blocked": "reindex_required",
                "message": sync_result.get("message", "需完整重建 Index"),
                "index_freshness": sync_result.get("index_freshness"),
                "index_status": "STALE",
            }
        index = load_index(root)
        if index is None:
            return {
                "ok": False,
                "blocked": "missing",
                "message": "sync 后 Index 仍不可读",
                "index_status": "STALE",
            }
        return {"ok": True, "index": index, "note": f"synced: {sync_result.get('action')}"}

    # ---------- understanding 保障 ----------

    async def _ensure_understanding(self, index: Any) -> dict[str, Any]:
        """project_understanding 缺失/过期 → 尝试补齐（force=False，不偷跑 reindex）。

        返回 {"ok": bool, "status": str, "message": str}
        """
        from agentx.index.fingerprint import compute_fingerprint as _cf
        from agentx.index.index import index_exclude_name
        from agentx.understanding.understand import ensure_understanding, understanding_status

        current = _cf(self.root, extra_excludes={index_exclude_name(self.root)})
        usable, reason = understanding_status(index, current)
        if usable:
            return {"ok": True, "status": "reused", "message": reason}
        if self.app is None:
            return {
                "ok": False,
                "status": "no_llm",
                "message": (
                    "project_understanding 缺失且无 app（无法补齐），"
                    "文档将标注 Needs verification"
                ),
            }
        try:
            result = await ensure_understanding(
                self.app, self.root, force=False, scope_selections=None
            )
        except Exception as e:
            return {
                "ok": False,
                "status": "failed",
                "message": f"补齐 understanding 失败（不阻塞）: {type(e).__name__}: {e}",
            }
        return {"ok": result.get("status") not in ("failed", "skipped"), **result}

    # ---------- LLM 散文（可选，仅语言组织） ----------

    async def _prose_for(self, doc_name: str, bundle: Any) -> str:
        """用 LLM 为该文档生成"语言组织"散文；无 app / 失败 → 空字符串（确定性照常）。

        硬约束提示：只解释给定的节点与关系，不得新增事实/模块/符号/调用。
        生成后校验：散文出现允许集外的标识符（模块/符号名）→ 丢弃散文
        （绝不让 LLM 把 bundle 外的事实写进人类文档）。
        """
        if self.app is None:
            return ""
        if doc_name not in PROSE_DOCS:
            return ""
        try:
            from agentx.core.orchestrator import _env_hint
            from agentx.providers.messages import ChatMessage

            runtime = self.app.orchestrator.agents.get("plan")
            if runtime is None:
                return ""
            facts = _facts_for_prose(doc_name, bundle)
            if not facts:
                return ""
            instructions = (
                "你是 AgentX 的人类文档撰稿人。任务：把下方【事实】组织成工程师可读的"
                "中文散文段落。\n"
                "硬性约束：\n"
                "1. 只能解释下方出现的模块/符号/文件/关系，禁止新增不存在的模块、"
                "符号、调用关系、协议或工程约束\n"
                "2. 不确定的内容写 'Needs verification' 或省略\n"
                "3. 不要输出 Markdown 标题/表格，只输出 2~4 句散文\n"
                "4. 保持英文代码名/符号/文件名原样\n\n"
                "【事实】\n"
            )
            ctx = self.app.orchestrator._ctx(self.app._dummy_task())
            messages = [
                ChatMessage(role="user", content=instructions),
                ChatMessage(role="user", content=_env_hint()),
                ChatMessage(role="user", content=facts),
            ]
            result = await runtime.run(messages, ctx)
            content = (result.content or "").strip()
            allowed = _prose_allowed_tokens(doc_name, bundle)
            if not _prose_within_allowlist(content, allowed):
                return ""
            return content[:1500]
        except Exception:
            return ""

    # ---------- 生成 / 刷新 / 状态 ----------

    def _write_doc(
        self,
        doc_name: str,
        index: Any,
        bundle: Any,
        prose: str,
    ) -> list[str]:
        """渲染并写一份文档；返回其 knowledge_sources（文件清单）。"""
        hdir = human_dir(self.root)
        hdir.mkdir(parents=True, exist_ok=True)
        md = render_document(doc_name, bundle, index, self.root.name, prose=prose)
        target = hdir / doc_name
        target.write_text(md, encoding="utf-8")
        # 该文档覆盖的模块/文件（knowledge_sources）——供 manifest 记录
        sources = _doc_sources(doc_name, bundle)
        return sources

    async def generate(
        self,
        *,
        force_rebuild: bool = False,
        scope_selections: dict[str, Any] | None = None,
        with_prose: bool = True,
    ) -> dict[str, Any]:
        """生成全部 Human Index 文档。"""
        ready = self._ensure_index_ready(
            force_rebuild=force_rebuild, scope_selections=scope_selections
        )
        if not ready["ok"]:
            return {
                "status": "blocked",
                "human_index": {"status": "blocked", "documents": []},
                **{k: v for k, v in ready.items() if k != "ok"},
            }
        index = ready["index"]
        bundle = collect_bundle(self.root)
        understanding = await self._ensure_understanding(index)
        # 用补齐后的 index（ensure_understanding 会 save_index）
        from agentx.index.index import load_index

        index = load_index(self.root) or index
        bundle = collect_bundle(self.root)

        manifest = load_manifest(self.root)
        generated: list[str] = []
        per_doc: dict[str, Any] = {}
        for doc in ALL_DOCUMENTS:
            prose = await self._prose_for(doc, bundle) if with_prose else ""
            sources = self._write_doc(doc, index, bundle, prose)
            generated.append(doc)
            modules = _doc_modules(doc, bundle)
            per_doc[doc] = {"knowledge_sources": sources, "modules": modules}
            record_document(
                manifest,
                doc,
                modules=modules,
                knowledge_sources=sources,
                deps=_deps_for(doc, modules, index),
            )
        touch_manifest_baseline(
            manifest,
            source_fingerprint=index.source_fingerprint,
            build_scope_fingerprint=index.build_scope_fingerprint,
            scope_fingerprint=index.scope_fingerprint,
        )
        manifest["understanding_status"] = understanding.get("status")
        save_manifest(self.root, manifest)
        return {
            "status": "updated",
            "human_index": {
                "status": "updated",
                "documents": generated,
                "dir": str(human_dir(self.root)),
            },
            "understanding": understanding.get("status", "unknown"),
            "understanding_message": understanding.get("message", ""),
        }

    async def refresh(
        self,
        *,
        force_rebuild: bool = False,
        scope_selections: dict[str, Any] | None = None,
        changed_modules: list[str] | None = None,
        changed_relations: list[str] | None = None,
        force: bool = False,
        with_prose: bool = True,
    ) -> dict[str, Any]:
        """只刷新受影响的文档（依据 manifest）。force=True 全量。"""
        ready = self._ensure_index_ready(
            force_rebuild=force_rebuild, scope_selections=scope_selections
        )
        if not ready["ok"]:
            return {
                "status": "blocked",
                **{k: v for k, v in ready.items() if k != "ok"},
            }
        index = ready["index"]
        bundle = collect_bundle(self.root)
        understanding = await self._ensure_understanding(index)
        from agentx.index.index import load_index

        index = load_index(self.root) or index

        stale = stale_documents(
            self.root,
            index,
            changed_modules=changed_modules,
            changed_relations=changed_relations,
            force=force,
        )
        manifest = load_manifest(self.root)
        refreshed: list[str] = []
        for doc in stale:
            prose = await self._prose_for(doc, bundle) if with_prose else ""
            sources = self._write_doc(doc, index, bundle, prose)
            refreshed.append(doc)
            modules = _doc_modules(doc, bundle)
            record_document(
                manifest,
                doc,
                modules=modules,
                knowledge_sources=sources,
                deps=_deps_for(doc, modules, index),
            )
        if refreshed:
            touch_manifest_baseline(
                manifest,
                source_fingerprint=index.source_fingerprint,
                build_scope_fingerprint=index.build_scope_fingerprint,
                scope_fingerprint=index.scope_fingerprint,
            )
            manifest["understanding_status"] = understanding.get("status")
            save_manifest(self.root, manifest)
        return {
            "status": "updated" if refreshed else "no_change",
            "human_index": {
                "status": "updated" if refreshed else "no_change",
                "documents": refreshed,
                "dir": str(human_dir(self.root)),
                "skipped": [d for d in ALL_DOCUMENTS if d not in refreshed],
            },
        }

    def status(self) -> dict[str, Any]:
        from agentx.index.index import index_status, load_index

        status, reason = index_status(self.root)
        manifest = load_manifest(self.root)
        docs = manifest.get("documents", {})
        present = [d for d in ALL_DOCUMENTS if doc_generated(self.root, d)]
        stale = stale_documents(self.root, load_index(self.root) or object())
        return {
            "status": status.value if hasattr(status, "value") else str(status),
            "index_reason": reason,
            "human_index": {
                "status": "generated" if present else "missing",
                "present": present,
                "missing": [d for d in ALL_DOCUMENTS if d not in present],
                "stale": stale,
                "dir": str(human_dir(self.root)),
                "baseline": {
                    "source_fingerprint": manifest.get("source_fingerprint"),
                    "build_scope_fingerprint": manifest.get("build_scope_fingerprint"),
                },
                "documents": {
                    d: {
                        "modules": docs.get(d, {}).get("modules", []),
                        "generated_at": docs.get(d, {}).get("generated_at"),
                    }
                    for d in docs
                },
            },
        }


# ---------- helpers ----------


def _facts_for_prose(doc_name: str, bundle: Any) -> str:
    if doc_name == DOC_PROJECT_OVERVIEW:
        d = bundle.project_overview_data()
        lines = []
        if d["chip"] or d["cpu"]:
            lines.append(f"- MCU={d['cpu']} Chip={d['chip']} RTOS={d['rtos']}")
        lines.append(
            f"- files={d['file_count']} compiled={d['compiled_count']} "
            f"modules={d['module_count']} symbols={d['symbol_count']}"
        )
        lines.append(f"- target={d['target']} build={d['build_system']}")
        for a in d["core_areas"]:
            lines.append(f"- area:{a['module']}: {a['responsibility']}")
        return "\n".join(lines)
    if doc_name == DOC_ARCHITECTURE:
        d = bundle.architecture_data()
        lines = []
        for m in d.get("modules", [])[:40]:
            resp = str(m.get("responsibility") or "")
            lines.append(f"- module:{m['name']} files={m['file_count']}: {resp}")
        for dep in d.get("module_dependencies", [])[:40]:
            lines.append(f"- dep: {dep['from']} -> {dep['to']}")
        for m in d.get("non_build_modules", [])[:5]:
            lines.append(f"- non_build_module:{m}")
        return "\n".join(lines)
    return ""


def _prose_allowed_tokens(doc_name: str, bundle: Any) -> set[str]:
    """散文允许引用的标识符集合（模块名 + 符号 + 文件 stem + 运行时专名）。

    仅用于校验"散文不得引入 bundle 外实体"；中文自然语言不受限。
    """
    import re

    allowed: set[str] = set()
    if doc_name in PROSE_DOCS:
        for m in bundle.modules:
            allowed.add(str(m.get("name", "")))
            for s in m.get("symbols", []) or []:
                allowed.add(str(s))
            for f in m.get("files", []) or []:
                allowed.add(str(f).rsplit("/", 1)[-1])
    for dep in (bundle.index.dependencies or []):
        allowed.add(str(dep.get("from", "")))
        allowed.add(str(dep.get("to", "")))
    # 运行时专名（来自 Index 事实，允许散文引用）
    bi = bundle.index.build_info or {}
    bs = bi.get("build_scope") or {}
    for tok in [
        str(bs.get("target") or bi.get("target") or ""),
        str((bi.get("cpu") or "").split("(")[0].strip()),
        *[str(x) for x in (bi.get("defines") or [])],
        *[str(x) for x in (bi.get("targets") or [])],
    ]:
        if tok:
            for part in re.split(r"[^A-Za-z0-9_]+", tok):
                if part and len(part) >= 2:
                    allowed.add(part.upper())
    # RTOS
    from agentx.human.bundle import detect_rtos

    rtos = detect_rtos([str(s.get("name", "")) for s in bundle.index.symbols])
    if rtos:
        allowed.add(rtos.upper())
        if rtos == "FreeRTOS":
            allowed.add("FREERTOS")
    return {a.upper() for a in allowed if a and len(a) >= 2}


def _prose_within_allowlist(prose: str, allowed: set[str]) -> bool:
    """散文校验：含允许集外的大写标识符 → False（丢弃，防 bundle 外事实入文档）。

    allowed 已统一为大写。校验对象为含大写字母的 token（专名/代码标识符）：
    - token.upper() ∈ allowed → ok
    - 前缀族匹配（HMI_Service 被写为 HMISERVICE / HMI）→ ok
    - 小写普通英文词（service/manager 等）→ 不当作专名
    - 其余含大写 token（如编造的模块名）→ 拒绝
    """
    if not prose:
        return False
    import re

    tokens = re.findall(r"[A-Za-z_][A-Za-z0-9_]{2,}", prose)
    common_lower = {
        "service", "manager", "module", "data", "flow", "control", "layer",
        "rtos", "mcu", "firmware", "build", "target", "system",
        "hardware", "storage", "protocol", "communication", "the", "and",
        "for", "with", "via", "app", "hmi", "ui", "rtc", "comm",
        "application", "initialization", "registration", "responsibility",
        "overview", "architecture", "feature", "view", "widget", "store",
        "callback", "entry", "consumer", "dependency", "compiled", "files",
    }
    for tok in tokens:
        upper = tok.upper()
        if upper in allowed:
            continue
        # 纯小写普通英文词（非专名）不校验
        if tok.islower() and tok.lower() in common_lower:
            continue
        if tok.islower() and "_" not in tok and not any(ch.isupper() for ch in tok):
            continue
        # 前缀族：与某 allowed 标识符存在公共前缀（HMI / HMI_Service 一族）→ 视为引用
        if any(
            a.startswith(upper) or upper.startswith(a)
            for a in allowed
            if len(a) >= 3 and len(upper) >= 3
        ):
            continue
        # 含大写字母的 token 未在允许集 → 拒绝（防编造模块/符号）
        if any(ch.isupper() for ch in tok):
            return False
    return True


def _doc_sources(doc_name: str, bundle: Any) -> list[str]:
    """文档覆盖的源文件（Traceability / 刷新知识来源）。"""
    if doc_name == DOC_MODULES:
        out: list[str] = []
        for m in bundle.modules:
            out.extend(str(f) for f in (m.get("files") or [])[:3])
        return out
    if doc_name == DOC_ARCHITECTURE:
        d = bundle.architecture_data()
        out = []
        for m in d.get("modules", []):
            out.extend(str(f) for f in (m.get("files") or [])[:2])
        return out
    if doc_name == DOC_PROJECT_OVERVIEW:
        d = bundle.project_overview_data()
        out = []
        for a in d["core_areas"]:
            mod = bundle.module_by_name.get(a["module"])
            if mod:
                out.extend(str(f) for f in (mod.get("files") or [])[:2])
        return out
    return []


def _doc_modules(doc_name: str, bundle: Any) -> list[str]:
    """文档覆盖的模块名清单。"""
    if doc_name == DOC_MODULES:
        return [str(m.get("name", "")) for m in bundle.modules]
    if doc_name == DOC_ARCHITECTURE:
        d = bundle.architecture_data()
        return [str(m.get("name", "")) for m in d.get("modules", [])]
    if doc_name == DOC_PROJECT_OVERVIEW:
        return [str(a["module"]) for a in bundle.project_overview_data()["core_areas"]]
    return []


def _deps_for(doc_name: str, modules: list[str], index: Any) -> dict[str, Any]:
    from agentx.human.manifest import document_dependency_decl

    decl = document_dependency_decl(doc_name)
    decl["modules"] = sorted(modules)
    if index is not None:
        decl["build_scope"] = bool(
            decl.get("build_scope")
            and index.build_scope_fingerprint is not None
        )
    return decl


def infer_changed_modules_from_files(
    changed_files: list[str], bundle: Any
) -> list[str]:
    """增量：changed file → 其所在模块名集合。"""
    from agentx.query.module_query import module_of_file

    out: list[str] = []
    seen: set[str] = set()
    for f in changed_files:
        m = module_of_file(bundle.index, f)
        if m and str(m.get("name", "")) not in seen:
            seen.add(str(m.get("name", "")))
            out.append(str(m.get("name", "")))
    return out


__all__ = [
    "HumanKnowledgeService",
    "infer_changed_modules_from_files",
    "PROSE_DOCS",
]
