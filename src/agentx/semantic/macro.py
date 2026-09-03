"""宏 / #define 提取（tree-sitter-c AST preproc_def 节点）。

保留原始表达式，不执行宏展开（依赖 include 顺序 / #ifdef / compiler define，
属于未来 Preprocessor Reality 阶段）。
"""

from __future__ import annotations

import re

from tree_sitter import Node

from agentx.semantic.types import MacroInfo

# 单一 token 判定：数字（含 0x/后缀）/ 字符串字面量 / 单标识符 → value
_VALUE_RE = re.compile(
    r"^\s*(?:0[xX][0-9a-fA-F]+|(?:\d+(?:\.\d+)?)(?:[uUlLfF]{0,3})?|\"(?:[^\"\\]|\\.)*\"|"
    r"[A-Za-z_][A-Za-z0-9_]*)\s*$"
)


def _child(node: Node, field: str) -> Node | None:
    return node.child_by_field_name(field)


def _text(node: Node, src: bytes) -> str:
    return src[node.start_byte : node.end_byte].decode(errors="replace").strip()


def extract_macro(node: Node, src: bytes, file: str) -> MacroInfo | None:
    """提取 #define 宏；按 Phase 7.7.4 分类收录全部宏（不跳过函数宏/空宏）。

    kind：
    - constant：对象宏（#define FOO 800 / #define FOO expr）——symbols 收录
    - function：函数式宏（#define FOO(x) ...，preproc_function_def 节点）
    - flag：空宏（#define FOO，配合 #ifdef 条件编译）

    宏内容必须来自源码原文，不允许任何推断。
    """
    if node.type not in ("preproc_def", "preproc_function_def"):
        return None
    name_node = _child(node, "name")
    if name_node is None:
        return None
    name = _text(name_node, src)
    value_node = _child(node, "value")
    raw = _text(value_node, src) if value_node is not None else ""
    line = node.start_point.row + 1

    if node.type == "preproc_function_def":
        return MacroInfo(name=name, file=file, line=line, value_expr=raw, kind="function")
    if value_node is None:
        # 空宏（#define FOO）：条件编译标志（#ifdef FOO）
        return MacroInfo(name=name, file=file, line=line, kind="flag")
    if _VALUE_RE.match(raw):
        return MacroInfo(name=name, file=file, line=line, value=raw, kind="constant")
    return MacroInfo(name=name, file=file, line=line, value_expr=raw, kind="constant")
