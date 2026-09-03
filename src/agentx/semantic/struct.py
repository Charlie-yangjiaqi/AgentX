"""struct 成员提取（tree-sitter-c AST，确定性，不猜测）。"""

from __future__ import annotations

from tree_sitter import Node

from agentx.semantic.types import MemberInfo, StructInfo

_STRUCT_TYPES = {"struct_specifier", "union_specifier"}


def _child(node: Node, field: str) -> Node | None:
    return node.child_by_field_name(field)


def _text(node: Node, src: bytes) -> str:
    return src[node.start_byte : node.end_byte].decode(errors="replace").strip()


def _end_line(node: Node) -> int:
    """节点结束行：结束于行首（尾换行）时取上一行。"""
    row, col = node.end_point
    return row if col == 0 else row + 1


def _find_identifier(node: Node | None) -> Node | None:
    if node is None:
        return None
    if node.type in ("identifier", "field_identifier"):
        return node
    for child in node.children:
        found = _find_identifier(child)
        if found is not None:
            return found
    return None


def _declarator_name(fd: Node) -> Node | None:
    """成员名 = declarator 字段内的标识符（避免钻进嵌套类型内部）。"""
    decl = fd.child_by_field_name("declarator")
    return _find_identifier(decl)


def _is_function_pointer(fd: Node) -> bool:
    """函数指针字段判定：declarator 子树含 function_declarator
    （如 `void (*callback)(int)` / `int (*handler)(void)`）。"""
    decl = fd.child_by_field_name("declarator")
    if decl is None:
        return False
    stack = [decl]
    while stack:
        cur = stack.pop()
        if cur.type == "function_declarator":
            return True
        stack.extend(cur.children)
    return False


def _type_text(fd: Node, src: bytes, name_node: Node) -> str:
    """成员类型 = 声明头部（到成员名，含指针）+ 数组后缀。

    函数指针字段：完整声明文本去掉成员名（`void (*callback)(int)` →
    `void (*)(int)`），保留参数列表（类型语义完整）。
    """
    if _is_function_pointer(fd):
        text = _text(fd, src).rstrip(";").strip()
        name = _text(name_node, src)
        return text.replace(name, "", 1).strip() if name else text
    head = src[fd.start_byte : name_node.start_byte].decode(errors="replace").strip()
    suffix = _array_suffix(fd, src)
    if head:
        return head + suffix
    return _text(fd, src)


def _array_suffix(node: Node, src: bytes) -> str:
    stack = [node]
    while stack:
        cur = stack.pop()
        if cur.type == "array_declarator":
            text = src[cur.start_byte : cur.end_byte].decode(errors="replace")
            idx = text.find("[")
            return text[idx:] if idx >= 0 else ""
        stack.extend(cur.children)
    return ""


def extract_struct(node: Node, src: bytes, file: str) -> StructInfo | None:
    """提取 struct/union 定义（typedef 与非 typedef 均支持）。"""
    if node.type not in _STRUCT_TYPES:
        return None
    body = _child(node, "body")
    if body is None:
        return None  # 前向声明（struct Foo;）无成员

    # 名字优先级：typedef 别名 > struct 标签名
    name: str | None = None
    type_def = _find_typedef(node)
    if type_def is not None:
        alias = _child(type_def, "declarator")
        if alias is not None and alias.type == "type_identifier":
            name = _text(alias, src)
    if name is None:
        tag = _child(node, "name")
        if tag is not None:
            name = _text(tag, src)
    if name is None:
        return None  # 匿名 struct 不收录

    members: list[MemberInfo] = []
    for child in body.children:
        if child.type == "field_declaration":
            name_node = _declarator_name(child)
            if name_node is None:
                continue
            members.append(
                MemberInfo(
                    name=_text(name_node, src),
                    type=_type_text(child, src, name_node),
                    line=child.start_point.row + 1,
                    is_function_pointer=_is_function_pointer(child),
                )
            )
    return StructInfo(
        name=name,
        file=file,
        start_line=node.start_point.row + 1,
        end_line=_end_line(node),
        members=members,
    )


def _find_typedef(node: Node) -> Node | None:
    """向上查找包裹的 type_definition（若有）。"""
    parent = node.parent
    while parent is not None:
        if parent.type == "type_definition":
            return parent
        parent = parent.parent
    return None
