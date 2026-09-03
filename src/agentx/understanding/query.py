"""Index Query Layer：任务 → 项目认知图子集。

不是搜索代码文本，而是查询 Index 的结构化认知（符号/调用/包含/构建），
返回与任务相关的影响范围子图。第一版规则化：关键词提取 + 直接命中 +
1-hop 图扩展，无 LLM、无 embedding。

匹配策略：
1. direct hit：文件名 / symbol 名 / path 片段（大小写归一化，下划线/驼峰拆分）
2. 1-hop expansion：命中符号的 callers/callees；命中文件的 include/被 include
输出机器可消费结构，reason 记录每项命中依据（summary 只是辅助展示）。
"""

from __future__ import annotations

import re
from typing import Any

from agentx.index.index import ProjectIndex

_STOPWORDS = {
    "the",
    "a",
    "an",
    "and",
    "or",
    "of",
    "to",
    "in",
    "on",
    "for",
    "with",
    "at",
    "by",
    "from",
    "is",
    "are",
    "it",
    "this",
    "that",
    "as",
    "be",
    "can",
    "could",
    "should",
    "would",
    "will",
    "do",
    "does",
    "did",
    "not",
    "but",
    "if",
    "then",
    "else",
    "all",
    "any",
    "please",
    "implement",
    "implementing",
    "review",
    "verify",
    "plan",
    "task",
    "goal",
    "add",
    "adds",
    "adding",
    "fix",
    "fixes",
    "fixing",
    "check",
    "checks",
    "need",
    "needs",
    "new",
    "use",
    "uses",
    "using",
    "make",
    "makes",
    "making",
    "project",
    "code",
    "function",
    "functions",
    "module",
    "modules",
}

_CAMEL_SPLIT_RE = re.compile(r"([a-z0-9])([A-Z])")
_TOKEN_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_SYMBOL_TOKEN_RE = re.compile(r"[A-Za-z0-9_]+")

# 中文工程术语 → 英文别名（零 LLM 基础映射；项目记忆/alias 后续扩展）
_ZH_ALIASES: dict[str, list[str]] = {
    "按键": ["key", "button"],
    "按钮": ["button", "key"],
    "键": ["key"],
    "串口": ["uart", "serial", "usart"],
    "屏幕": ["lcd", "display"],
    "显示": ["lcd", "display"],
    "蓝牙": ["bluetooth"],
    "协议": ["protocol"],
    "初始化": ["init"],
    "驱动": ["driver"],
    "传感器": ["sensor"],
    "温度": ["temp"],
    "电压": ["volt"],
    "电流": ["current"],
    "电机": ["motor"],
    "内存": ["memory"],
    "参数": ["param"],
    "定时器": ["timer"],
    "时钟": ["clock"],
    "网络": ["net", "wifi"],
    "通信": ["comm"],
    "通讯": ["comm"],
    "无线": ["rf", "wireless"],
    "触摸": ["touch"],
    "音频": ["audio"],
    "电源": ["power"],
    "日志": ["log"],
    "配置": ["config"],
    "固件": ["firmware"],
    "头文件": ["header"],
    "功能": [],
    "模块": [],
    "实现": [],
    "作用": [],
    "流程": [],
    "哪里": [],
    "什么": [],
    "找": [],
    "一下": [],
}


def extract_keywords(task: str) -> list[str]:
    """从任务描述提取查询词：token 化 + 驼峰/下划线拆分 + 去停用词。

    "LCD初始化并修复 param_init 的 bug" → ["lcd", "init", "param", "init",
    "bug"]（初始化中的英文 token 会被拆分命中 LCD_Init / param_init）。
    """
    raw = _TOKEN_RE.findall(task)
    out: list[str] = []
    for tok in raw:
        lower = tok.lower()
        if lower in _STOPWORDS:
            continue
        parts = _CAMEL_SPLIT_RE.sub(r"\1 \2", tok).split("_")
        for part in parts:
            part = part.lower()
            if len(part) >= 2 and part not in _STOPWORDS:
                out.append(part)
    # 去重保序
    seen: set[str] = set()
    out2: list[str] = []
    for k in out:
        if k not in seen:
            seen.add(k)
            out2.append(k)
    # 中文术语映射（零 LLM 基础别名；项目记忆后续扩展）
    for zh, en in _ZH_ALIASES.items():
        if zh in task:
            for w in en:
                if w and w not in seen:
                    seen.add(w)
                    out2.append(w)
    return out2


def _symbol_tokens(name: str) -> list[str]:
    """符号名拆 token：LCD_Init → [lcd, init]；ParamCtx → [param, ctx]。"""
    parts = _CAMEL_SPLIT_RE.sub(r"\1 \2", name).split("_")
    return [p.lower() for p in parts if p]


def _path_hit(path: str, keywords: list[str]) -> str | None:
    """path 命中：路径各段（文件名/stem/目录）归一化后包含关键词。"""
    lower = path.lower().replace("\\", "/")
    parts = lower.split("/")
    stem = parts[-1].rsplit(".", 1)[0] if "." in parts[-1] else parts[-1]
    for kw in keywords:
        if kw in lower or kw in stem:
            return kw
    for kw in keywords:
        if any(kw in p for p in parts):
            return kw
    return None


def _symbol_hit(sym: dict[str, Any], keywords: list[str]) -> str | None:
    """symbol 命中：name/qualified_name 或其拆分 token 与关键词匹配。"""
    name = str(sym.get("name", "")).lower()
    qname = str(sym.get("qualified_name", "")).lower()
    tokens = _symbol_tokens(str(sym.get("name", "")))
    for kw in keywords:
        if kw in name or kw in qname:
            return kw
        if kw in tokens:
            return kw
    return None


def query_index(
    index: ProjectIndex,
    task: str,
    *,
    max_files: int = 15,
    max_symbols: int = 25,
    max_edges: int = 40,
) -> dict[str, Any]:
    """查询 Index 返回任务相关认知子图。

    返回 {"files": [...], "symbols": [...], "call_graph": [...],
    "include_map": {...}, "build": {...}, "reason": [...]}。
    无命中时返回空结构（调用方决定兜底策略）。
    """
    keywords = extract_keywords(task)
    if not keywords:
        return _empty_result()

    # 1. direct hit：文件与符号
    files: dict[str, str] = {}  # path → 命中关键词
    for f in index.files:
        kw = _path_hit(f.path, keywords)
        if kw:
            files[f.path] = kw

    symbols: dict[str, dict[str, Any]] = {}
    for s in index.symbols:
        kw = _symbol_hit(s, keywords)
        if kw:
            hit = dict(s)
            hit["hit"] = kw
            symbols[str(s.get("name", ""))] = hit

    reasons: list[str] = []
    if not files and not symbols:
        return _empty_result()

    # 2. 1-hop expansion：调用边（命中符号的出/入边）与包含关系
    hit_sym_names = set(symbols)
    edges: list[dict[str, Any]] = []
    for e in index.call_graph:
        caller, callee = str(e.get("caller", "")), str(e.get("callee", ""))
        if caller in hit_sym_names or callee in hit_sym_names:
            edges.append(e)

    include_map: dict[str, list[str]] = {}
    hit_files = set(files)
    for src, targets in index.include_map.items():
        if src in hit_files:
            include_map[src] = [t for t in targets if t in hit_files or t not in hit_files]
        else:
            in_hit = [t for t in targets if t in hit_files]
            if in_hit:
                include_map[src] = in_hit

    # 3. 命中符号所在文件并入 files（符号命中但文件名没命中时）
    for sym in symbols.values():
        fpath = str(sym.get("file", ""))
        if fpath and fpath not in files:
            files[fpath] = f"symbol:{sym.get('name', '')}"
    # 调用边另一端符号的文件也带上
    for e in edges:
        for side in ("caller", "callee"):
            name = str(e.get(side, ""))
            if name in hit_sym_names:
                continue
            for sym in index.symbols:
                if sym.get("name") == name:
                    fpath = str(sym.get("file", ""))
                    if fpath and fpath not in files:
                        files[fpath] = f"via:{side}"
                    break

    # 4. reason 与截断
    for path, kw in list(files.items())[:max_files]:
        reasons.append(f"{path} ← {kw}")
    for name, sym in list(symbols.items())[:max_symbols]:
        reasons.append(f"{name} ({sym.get('type')}) ← {sym.get('hit')}")

    build: dict[str, Any] = {}
    if index.build_info:
        build = index.build_info

    return {
        "files": [{"path": p, "hit": k} for p, k in files.items()],
        "symbols": list(symbols.values())[:max_symbols],
        "call_graph": edges[:max_edges],
        "include_map": include_map,
        "build": build,
        "reason": reasons[:40],
    }


def _empty_result() -> dict[str, Any]:
    return {
        "files": [],
        "symbols": [],
        "call_graph": [],
        "include_map": {},
        "build": {},
        "reason": [],
    }


def format_query_result(result: dict[str, Any]) -> str:
    """把 Query 结果转成 Planner 可读文本（结构数据为主，summary 辅助）。"""
    lines: list[str] = []
    files = result.get("files", [])
    symbols = result.get("symbols", [])
    edges = result.get("call_graph", [])
    include_map = result.get("include_map", {})
    build = result.get("build", {})

    if files:
        lines.append("相关文件:")
        for f in files[:15]:
            lines.append(f"  {f['path']}（{f.get('hit', '')}）")
    if symbols:
        lines.append("相关符号:")
        for s in symbols[:25]:
            lines.append(
                f"  {s.get('name')} ({s.get('type')}) @ {s.get('file')} "
                f"L{s.get('start_line')}-{s.get('end_line')}"
            )
    if edges:
        lines.append("相关调用:")
        for e in edges[:40]:
            lines.append(
                f"  {e.get('caller')} -> {e.get('callee')} "
                f"[{e.get('confidence')}] @ {e.get('file')}:{e.get('line')}"
            )
    if include_map:
        lines.append("相关包含:")
        for src, targets in list(include_map.items())[:10]:
            lines.append(f"  {src} -> {', '.join(targets[:5])}")
    if build:
        n_compiled = len(build.get("compiled_files", []))
        lines.append(f"构建真相: {build.get('system', 'unknown')}（{n_compiled} 个编译目标）")
    return "\n".join(lines)
