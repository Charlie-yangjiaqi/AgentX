"""Semantic runtime 诊断与错误类型（Index Pipeline Reliability）。

semantic_runtime_status()：供 enrich/doctor 输出运行时状态：
    [semantic] tree_sitter=0.26.0 grammar=c parser=tree_sitter_c status=enabled

SemanticUnavailableError：所有 parser 均不可用时的明确错误
（禁止静默 fallback 生成"看似成功"的 Index）。
"""

from __future__ import annotations

from typing import Any


class SemanticUnavailableError(RuntimeError):
    """Tree-sitter 语义运行时不可用（无任何可用 C parser）。"""


def semantic_runtime_status() -> dict[str, Any]:
    """检测 semantic runtime：tree_sitter / grammar / parser 来源 / 状态。

    返回：
    {"tree_sitter": "0.26.0"|None, "tree_sitter_c": bool, "language_pack": bool,
     "parser": "tree_sitter_c"|"tree_sitter_language_pack"|None,
     "grammar": "c", "status": "enabled"|"disabled",
     "python_version": "3.14.x", "worker_mode": bool,
     "max_file_size_mb": float, "timeout_seconds": float}
    """
    import importlib.util
    import platform
    from importlib import metadata

    from agentx.config.config import load_config, resolve_semantic_config

    ts_version: str | None = None
    ts_ok = importlib.util.find_spec("tree_sitter") is not None
    if ts_ok:
        try:
            ts_version = metadata.version("tree-sitter")
        except metadata.PackageNotFoundError:
            ts_version = None

    # tree-sitter 0.26.x Python binding 有 native 内存 bug（SIGSEGV）；
    # 版本门禁见 semantic/extractor.py（parse 前拦截输出结构化错误）
    ts_compatible = True
    if ts_version:
        try:
            parts = tuple(int(p) for p in ts_version.split(".")[:2] if p.isdigit())
            ts_compatible = not (len(parts) == 2 and parts >= (0, 26))
        except ValueError:
            ts_compatible = True

    c_ok = importlib.util.find_spec("tree_sitter_c") is not None
    pack_ok = importlib.util.find_spec("tree_sitter_language_pack") is not None

    parser: str | None = None
    try:
        from agentx.semantic.extractor import _PARSER_SOURCE

        parser = _PARSER_SOURCE
    except Exception:
        parser = None

    try:
        sem_cfg = resolve_semantic_config(load_config())
    except Exception:
        sem_cfg = resolve_semantic_config()

    return {
        "tree_sitter": ts_version,
        "tree_sitter_c": c_ok,
        "language_pack": pack_ok,
        "grammar": "c",
        "parser": parser,
        "status": "enabled" if parser else "disabled",
        "tree_sitter_compatible": ts_compatible,
        "python_version": platform.python_version(),
        "worker_mode": bool(sem_cfg["worker_mode"]),
        "isolation": "subprocess" if sem_cfg["worker_mode"] else "in_process",
        "worker_timeout_seconds": sem_cfg["worker_timeout_seconds"],
        "max_file_size_mb": sem_cfg["max_file_size_mb"],
    }


def format_runtime_status(status: dict[str, Any]) -> str:
    """诊断输出文本（ASCII 安全）。"""
    parser = status.get("parser") or "none"
    compat = status.get("tree_sitter_compatible")
    warn = ""
    if compat is False:
        warn = (
            " WARN: tree-sitter>=0.26 has native memory bug (SIGSEGV), "
            "semantic extraction disabled until downgraded to 0.25.x"
        )
    return (
        f"tree_sitter={status.get('tree_sitter') or 'not installed'} "
        f"grammar={status.get('grammar', 'c')} parser={parser} "
        f"status={status.get('status')} python={status.get('python_version', '?')} "
        f"worker_mode={status.get('worker_mode')} "
        f"isolation={status.get('isolation', '?')} "
        f"timeout={status.get('worker_timeout_seconds')}s "
        f"max_file_size={status.get('max_file_size_mb')}MB"
        f"{warn}"
    )
