"""enum 成员/值提取（tree-sitter-c AST，确定性）。

隐式递增按规则计算（0 起 +1）；显式数字保留原文；
表达式保留 value_expr；无法确定时不填 value。不执行表达式。
"""

from __future__ import annotations

import re

from tree_sitter import Node

from agentx.semantic.types import EnumInfo, EnumMemberInfo

_NUMBER_RE = re.compile(r"^\s*(0[xX][0-9a-fA-F]+|\d+)([uUlL]{0,3})\s*$")


def _child(node: Node, field: str) -> Node | None:
    return node.child_by_field_name(field)


def _text(node: Node, src: bytes) -> str:
    return src[node.start_byte : node.end_byte].decode(errors="replace").strip()


def _end_line(node: Node) -> int:
    """节点结束行：结束于行首（尾换行）时取上一行。"""
    row, col = node.end_point
    return row if col == 0 else row + 1


def _parse_int(raw: str) -> int | None:
    """解析 C 整数字面量（十进制/十六进制，忽略 U/L 后缀）。"""
    m = _NUMBER_RE.match(raw)
    if m is None:
        return None
    try:
        return int(m.group(1), 16 if m.group(1).lower().startswith("0x") else 10)
    except ValueError:
        return None


def extract_enum(node: Node, src: bytes, file: str) -> EnumInfo | None:
    if node.type != "enum_specifier":
        return None
    body = _child(node, "body")
    if body is None:
        return None  # enum X; 前向声明

    name: str | None = None
    parent = node.parent
    if parent is not None and parent.type == "type_definition":
        alias = _child(parent, "declarator")
        if alias is not None and alias.type == "type_identifier":
            name = _text(alias, src)
    if name is None:
        tag = _child(node, "name")
        if tag is not None:
            name = _text(tag, src)
    if name is None:
        return None  # 匿名 enum 不收录

    members: list[EnumMemberInfo] = []
    prev_value: int | None = None  # 上一个可解析的数值（隐式递增基准）
    for child in body.children:
        if child.type != "enumerator":
            continue
        name_node = _child(child, "name")
        if name_node is None:
            continue
        member_name = _text(name_node, src)
        line = child.start_point.row + 1
        value_node = _child(child, "value")
        if value_node is None:
            # 隐式递增：首个成员 = 0（C 标准），之后 = 前值 + 1；
            # 前值是表达式（无法确定）→ 后续隐式成员值不确定（null，保守）
            if prev_value is None and members:
                members.append(EnumMemberInfo(name=member_name, line=line, value=None))
            elif prev_value is None:
                members.append(EnumMemberInfo(name=member_name, line=line, value="0"))
                prev_value = 0
            else:
                members.append(
                    EnumMemberInfo(name=member_name, line=line, value=str(prev_value + 1))
                )
                prev_value += 1
            continue
        raw = _text(value_node, src)
        num = _parse_int(raw)
        if num is not None:
            members.append(EnumMemberInfo(name=member_name, line=line, value=raw))
            prev_value = num
        else:
            # 表达式/标识符引用：保留原始表达式，不执行。
            # 保守（Phase 7.7.4）：前值无法确定 → 后续隐式成员值也不确定（null）
            members.append(EnumMemberInfo(name=member_name, line=line, value_expr=raw))
            prev_value = None
    return EnumInfo(
        name=name,
        file=file,
        start_line=node.start_point.row + 1,
        end_line=_end_line(node),
        members=members,
    )
