"""HumanKnowledgeBundle：从 ProjectIndex 收集人类文档需要的结构化知识。

收集层零 LLM——只裁剪 index.json / module_responsibilities.json 的事实，按
文档需要组装。LLM 输入绝不直接使用整个 index.json（Phase 8.3 原则）。

Bundle 分类（贯穿所有文档）：
- Fact：来自 Build Reality / symbols / call_graph / include_map 的确定事实
- Inference：来自依赖/调用/命名/职责(confidence=medium) 的推断
- Recommendation：工程建议（文档为工程师可读而写，不进入知识事实）
- Unknown：缺失 / 无证据

来源 Assets：
- ProjectIndex：files/modules/symbols/call_graph/indirect_calls/include_map/
  type_semantics/build_info/project_understanding/三指纹/capabilities/errors
- module_responsibilities.json：responsibility + confidence + generated_by
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from agentx.human.manifest import ALL_DOCUMENTS
from agentx.module.responsibility import load_responsibilities
from agentx.module.scorer import score_module

# 运行时环境探测关键词（符号名，Fact）
_RTOS_SYMBOLS = {
    "freertos": "FreeRTOS",
    "vtaskstartscheduler": "FreeRTOS",
    "xtaskcreate": "FreeRTOS",
    "osthreadnew": "CMSIS-RTOS2",
    "oskernelstart": "CMSIS-RTOS2",
    "rtthread": "RT-Thread",
    "threadcreate": "RT-Thread",
}
_FW_KEYWORDS = {  # 符号前缀 → 框架/中间件（Fact，来自 scope 第三方）
    "lv_": "LVGL",
    "lvgl": "LVGL",
}


def detect_rtos(symbol_names: list[str]) -> str | None:
    """从符号推断 RTOS（Fact：符号存在才声明）。"""
    lowered = {n.casefold() for n in symbol_names}
    for probe, name in _RTOS_SYMBOLS.items():
        if any(probe in s for s in lowered):
            return name
    return None


def _cpu_from_build_info(build_info: dict[str, Any]) -> dict[str, str | None]:
    """从 build_info.cpu（Keil 原始串）或 defines 解析 MCU 事实。"""
    cpu_raw = str(build_info.get("cpu") or "")
    mcu: str | None = None
    import re

    m = re.search(r'CPUTYPE\("([^"]+)"\)', cpu_raw)
    if m:
        mcu = m.group(1)
    elif cpu_raw:
        # 取第一段（如 Cortex-M4 前的 IRAM 信息不在此）
        mcu = cpu_raw.strip().split()[0] if cpu_raw.strip() else None
    # 从 defines 找芯片型号（GD32F427）
    chip: str | None = None
    for d in build_info.get("defines", []) or []:
        ds = str(d)
        if ds.lower().startswith(("gd32", "stm32", "at32", "hc32", "apm32", "n32")):
            chip = ds
            break
    return {"cpu": mcu, "chip": chip}


class HumanKnowledgeBundle:
    """一份独立的人类文档知识包（可序列化为文本给 LLM 或确定性生成器）。"""

    def __init__(self, index: Any, responsibilities: dict[str, dict[str, Any]]) -> None:
        self.index = index
        self.responsibilities = responsibilities
        self.modules = list(index.modules or [])
        self.module_by_name = {str(m.get("name", "")): m for m in self.modules}
        self._symbol_name_set: set[str] | None = None

    @property
    def symbol_names(self) -> set[str]:
        if self._symbol_name_set is None:
            self._symbol_name_set = {str(s.get("name", "")) for s in self.index.symbols}
        return self._symbol_name_set

    # ---------- project overview ----------

    def project_overview_data(self) -> dict[str, Any]:
        bi = self.index.build_info or {}
        cpu = _cpu_from_build_info(bi)
        bs = bi.get("build_scope") or {}
        understanding = self.index.project_understanding or {}
        # 核心功能领域：只取 LLM 生成的 high 职责（有真实业务含义），
        # fallback 描述（"包含 N 个符号"）是事实构成不是职责判断，不充当领域
        core_areas: list[dict[str, str]] = []
        seen_area: set[str] = set()
        for m in self.modules:
            if m.get("scope_type") != "project":
                continue
            if m.get("third_party"):
                continue
            entry = self.responsibilities.get(str(m.get("name", ""))) or {}
            conf = entry.get("confidence") or score_module(m)["confidence"]
            gen_by = entry.get("generated_by")
            resp = str(entry.get("responsibility", "")).strip()
            if conf == "high" and gen_by == "llm" and resp:
                top = _top_file_dir(m)
                area = top or str(m.get("name", ""))
                if area not in seen_area:
                    seen_area.add(area)
                    core_areas.append(
                        {
                            "area": area,
                            "module": str(m.get("name", "")),
                            "responsibility": resp,
                            "confidence": conf,
                            "generated_by": gen_by,
                        }
                    )
        understanding_areas = [
            str(c) for c in understanding.get("core_modules") or [] if isinstance(c, str)
        ]
        # 规则化入口（Fact，来自 index，不依赖 understanding）
        entry_candidates = []
        try:
            from agentx.understanding.understand import discover_entry_candidates

            entry_candidates = [
                {
                    "file": e.file,
                    "symbol": e.symbol,
                    "confidence": e.confidence,
                    "reason": e.reason,
                }
                for e in discover_entry_candidates(self.index)
            ]
        except Exception:
            entry_candidates = []
        return {
            "project_fingerprint": self.index.project_fingerprint,
            "source_fingerprint": self.index.source_fingerprint,
            "scope_fingerprint": self.index.scope_fingerprint,
            "build_scope_fingerprint": self.index.build_scope_fingerprint,
            "generated_at": (
                self.index.last_index_time.isoformat()
                if self.index.last_index_time
                else self.index.generated_at.isoformat()
            ),
            "build_system": bi.get("system"),
            "build_source": bi.get("build_source"),
            "target": bs.get("target") or bi.get("target"),
            "targets": bi.get("targets") or bs.get("targets") or [],
            "cpu": cpu["cpu"],
            "chip": cpu["chip"],
            "defines": bi.get("defines") or [],
            "has_build_config": bi.get("has_build_config"),
            "file_count": self.index.file_count,
            "compiled_count": len(bi.get("compiled_files") or []),
            "module_count": len(self.modules),
            "symbol_count": len(self.index.symbols),
            "call_edge_count": len(self.index.call_graph),
            "indirect_call_count": len(self.index.indirect_calls),
            "rtos": detect_rtos(list(self.symbol_names)),
            "understanding_summary": understanding.get("architecture_summary"),
            "core_areas": core_areas[:12],
            "understanding_core_modules": understanding_areas[:10],
            "entry_points": entry_candidates[:10],
            "capabilities": self.index.capabilities or {},
            "errors": list(self.index.errors or [])[:10],
        }

    # ---------- architecture ----------

    def architecture_data(self) -> dict[str, Any]:
        bi = self.index.build_info or {}
        understanding = self.index.project_understanding or {}
        # runtime layers：基于 build_scope 分类
        project_modules = [m for m in self.modules if m.get("scope_type") == "project"]
        third_party_names = sorted(
            {
                str(f.scope_name or f.path.split("/")[0])
                for f in self.index.files
                if str(getattr(f, "scope_type", "")) == "third_party"
            }
        )
        non_build_files = [
            str(f.path)
            for f in self.index.files
            if str(getattr(f, "scope_type", "")) == "non_build"
        ]
        non_build_modules = [m for m in self.modules if m.get("scope_type") == "non_build"]
        return {
            "modules": [
                self._module_card(m, include_low=False) for m in project_modules
            ],
            "non_build_modules": [
                str(m.get("name", "")) for m in non_build_modules
            ],
            "module_dependencies": self._module_dependencies(),
            "call_graph_top": self._call_edges_summary(),
            "indirect_areas": self._indirect_areas(),
            "include_map": dict(
                list(self.index.include_map.items())[:120]
            ),
            "third_party": third_party_names,
            "non_build_files": non_build_files[:20],
            "build": {
                "system": bi.get("system"),
                "target": bi.get("target"),
                "targets": bi.get("targets"),
                "compiled_count": len(bi.get("compiled_files") or []),
                "project_files": self.index.file_count,
            },
            "startup_flow": understanding.get("startup_flow") or [],
            "architecture_summary": understanding.get("architecture_summary"),
            "entry_points": understanding.get("entry_points") or [],
            "type_structs": (self.index.type_semantics or {}).get("structs", [])[:60],
            "uncertain": self._uncertainty(),
        }

    def _module_card(self, mod: dict[str, Any], include_low: bool) -> dict[str, Any]:
        name = str(mod.get("name", ""))
        entry = self.responsibilities.get(name) or {}
        conf = entry.get("confidence") or score_module(mod)["confidence"]
        resp = str(entry.get("responsibility", "")).strip()
        gen_by = entry.get("generated_by")
        card = {
            "name": name,
            "type": mod.get("type"),
            "files": [str(f) for f in mod.get("files", [])[:12]],
            "file_count": len(mod.get("files", [])),
            "symbols": [str(s) for s in mod.get("symbols", [])[:16]],
            "symbol_count": len(mod.get("symbols", [])),
            "entry_points": [str(e) for e in mod.get("entry_points", [])[:8]],
            "consumers": [str(c) for c in mod.get("consumers", [])[:12]],
            "dependencies": [str(d) for d in mod.get("dependencies", [])[:12]],
            "build_status": mod.get("build_status"),
            "build_stats": mod.get("build_stats"),
            "scope_type": mod.get("scope_type"),
            "third_party": bool(mod.get("third_party")),
            "responsibility": resp,
            "confidence": conf,
            "generated_by": gen_by,
            "evidence_basis": (mod.get("evidence") or {}).get("basis", [])[:8],
            "uncertain": conf == "low" and not resp,
        }
        return card

    def _module_dependencies(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for d in self.index.dependencies or []:
            frm = str(d.get("from", ""))
            to = str(d.get("to", ""))
            if frm and to and frm != to:
                out.append({"from": frm, "to": to, "weight": int(d.get("weight", 0))})
        return sorted(out, key=lambda x: -int(x["weight"]))[:120]

    def _call_edges_summary(self) -> list[dict[str, Any]]:
        """call_graph 按模块聚合的高影响边（供 Mermaid / 执行路径）。"""
        # 返回按调用文件 top 分布的边（截断控制）
        return [dict(e) for e in (self.index.call_graph or [])[:200]]

    def _indirect_areas(self) -> list[dict[str, str]]:
        out: list[dict[str, str]] = []
        seen: set[tuple[str, str]] = set()
        for e in self.index.indirect_calls or []:
            f = str(e.get("file", ""))
            via = str(e.get("via", ""))
            callee = str(e.get("callee", ""))
            key = (f, via)
            if key in seen:
                continue
            seen.add(key)
            out.append({"file": f, "via": via, "callee": callee})
        return out[:30]

    def _uncertainty(self) -> list[str]:
        out: list[str] = []
        understanding = self.index.project_understanding or {}
        if not understanding:
            out.append("project_understanding 未建立（Needs verification）")
        elif not understanding.get("architecture_summary"):
            out.append("project_understanding 无架构摘要（Needs verification）")
        for m in self.modules:
            if m.get("scope_type") == "non_build":
                out.append(
                    f"模块 '{m.get('name')}' 在仓库中但不在当前 build target"
                )
        for f in self.index.files:
            if str(getattr(f, "scope_type", "")) == "non_build":
                break
        if not (self.index.capabilities or {}).get("semantic", {}).get("enabled"):
            out.append("semantic 层不可用（signature/type 信息不完整）")
        return out[:12]

    # ---------- modules ----------

    def module_knowledge(self, include_low_in_map: bool = True) -> dict[str, Any]:
        """MODULES.md 数据：map（全部模块）+ 展开（仅 project 的 high/medium）。"""
        map_rows: list[dict[str, Any]] = []
        detail: list[dict[str, Any]] = []
        for m in self.modules:
            name = str(m.get("name", ""))
            entry = self.responsibilities.get(name) or {}
            conf = entry.get("confidence") or score_module(m)["confidence"]
            resp = str(entry.get("responsibility", "")).strip()
            scope_type = m.get("scope_type")
            row = {
                "name": name,
                "responsibility": resp,
                "confidence": conf,
                "generated_by": entry.get("generated_by"),
                "build_status": m.get("build_status"),
                "files": len(m.get("files", [])),
                "symbols": len(m.get("symbols", [])),
                "scope_type": scope_type,
                "third_party": bool(m.get("third_party")),
            }
            map_rows.append(row)
            # 展开条件：project 自有代码 且 high/medium（third_party/non_build 不展开，
            # 只进 Map 并显式标注——绝不当作当前固件模块）
            if conf in ("high", "medium") and scope_type == "project":
                detail.append(self._module_card(m, include_low=False))
        map_rows.sort(key=lambda r: (_conf_rank(r["confidence"]), r["name"]))
        return {"map": map_rows, "detail": detail}

    # ---------- evidence & traceability ----------

    def evidence_lines(self, mod: dict[str, Any]) -> list[str]:
        return list((mod.get("evidence") or {}).get("basis", []) or [])


def _top_file_dir(mod: dict[str, Any]) -> str:
    files = mod.get("files", []) or []
    if not files:
        return ""
    first = str(files[0]).replace("\\", "/")
    parts = first.split("/")
    if len(parts) >= 2:
        return parts[-2]
    return parts[-1] if parts else ""


def _conf_rank(conf: str) -> int:
    return {"high": 0, "medium": 1, "low": 2}.get(conf, 3)


def collect_bundle(project_root: Path) -> HumanKnowledgeBundle:
    """加载当前 Index + responsibilities → Bundle（Index 不存在返回 None? 由调用方把关）。"""
    from agentx.index.index import load_index

    index = load_index(project_root)
    if index is None:
        raise ValueError("Project Index 不存在，无法构建 Human Knowledge Bundle")
    responsibilities = load_responsibilities(project_root)
    return HumanKnowledgeBundle(index, responsibilities)


def format_bundle_for_llm(bundle: HumanKnowledgeBundle, doc: str) -> str:
    """把 bundle 裁剪成该文档的 LLM 提示文本（禁传整个 index.json）。"""
    lines: list[str] = []
    if doc == ALL_DOCUMENTS[0]:  # PROJECT_OVERVIEW
        d = bundle.project_overview_data()
        lines.append("## Project Facts")
        lines.append(
            f"- Build: {d['build_system']}({d['build_source']}) target={d['target']} "
            f"targets={d['targets']}"
        )
        lines.append(
            f"- MCU: {d['cpu'] or 'Unknown'} chip={d['chip'] or 'Unknown'} "
            f"RTOS={d['rtos'] or 'Unknown/None'}"
        )
        lines.append(
            f"- counts: files={d['file_count']} compiled={d['compiled_count']} "
            f"modules={d['module_count']} symbols={d['symbol_count']} "
            f"calls={d['call_edge_count']} indirect={d['indirect_call_count']}"
        )
        if d["understanding_summary"]:
            lines.append(f"- Understanding summary: {d['understanding_summary']}")
        lines.append("- Core areas (high-confidence LLM responsibility):")
        for a in d["core_areas"]:
            lines.append(
                f"  - {a['area']} [{a['module']}] ({a['confidence']}): {a['responsibility']}"
            )
    return "\n".join(lines)
