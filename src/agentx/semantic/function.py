"""函数签名提取（tree-sitter-c AST，确定性，不猜测）。

支持：返回类型 / 参数名 / 参数类型 / const / pointer / array / 函数指针。
解析失败 → signature=None（semantic 仍标记 true：已尝试但语法复杂）。
"""

from __future__ import annotations

from tree_sitter import Node

from agentx.semantic.types import FunctionInfo, ParamInfo, SignatureInfo

_FUNCTION_NODES = {"function_definition", "declaration"}
_SKIP_RETURN_PREFIX = {"static", "extern", "inline", "const"}


def _child(node: Node, field: str) -> Node | None:
    return node.child_by_field_name(field)


def _text(node: Node, src: bytes) -> str:
    return src[node.start_byte : node.end_byte].decode(errors="replace").strip()


def _end_line(node: Node) -> int:
    """节点结束行：结束于行首（尾换行）时取上一行。"""
    row, col = node.end_point
    return row if col == 0 else row + 1


def _find_identifier(node: Node | None) -> Node | None:
    """递归找第一个 identifier/field_identifier 叶子。"""
    if node is None:
        return None
    if node.type in ("identifier", "field_identifier"):
        return node
    for child in node.children:
        found = _find_identifier(child)
        if found is not None:
            return found
    return None


def _array_suffix(node: Node, src: bytes) -> str:
    """从 declarator 中提取数组后缀（[N]），如 arr[8] → '[8]'。"""
    stack = [node]
    while stack:
        cur = stack.pop()
        if cur.type == "array_declarator":
            text = src[cur.start_byte : cur.end_byte].decode(errors="replace")
            idx = text.find("[")
            return text[idx:] if idx >= 0 else ""
        stack.extend(cur.children)
    return ""


def _param_info(pd: Node, src: bytes) -> ParamInfo | None:
    """提取单个参数；无名参数（如 void）返回 None。"""
    decl = pd.child_by_field_name("declarator")
    name_node = _find_identifier(decl) if decl is not None else None
    if name_node is None:
        return None
    name = _text(name_node, src)
    # 函数指针参数（含参数列表）：类型保留完整声明文本（原名信息保留）
    if pd.child_by_field_name("type") is not None and any(
        c.type == "function_declarator" for c in pd.children
    ):
        return ParamInfo(name=name, type=_text(pd, src))
    head = src[pd.start_byte : name_node.start_byte].decode(errors="replace").strip()
    ptype = head + _array_suffix(pd, src)
    if not ptype:
        ptype = _text(pd, src)  # 无类型信息（极罕见）：保留原文
    return ParamInfo(name=name, type=ptype)


def _return_type(node: Node, src: bytes) -> str | None:
    """返回类型 = 函数节点的 type 字段文本；缺失（旧式声明）→ None。"""
    type_node = _child(node, "type")
    if type_node is None:
        return None
    return _text(type_node, src)


def extract_function(node: Node, src: bytes, file: str) -> FunctionInfo | None:
    """从 function_definition / declaration（含 function_declarator）提取函数。

    Phase 7.9 降级收录：function_definition（有函数体，必然是函数）即使
    declarator 结构异常（宏干扰/语法残缺），只要名字可提取就收录
    （signature=None 降级，不丢失）；declaration 保持严格——非函数声明
    （如 `int x;` 变量声明）不收录，避免污染函数列表。
    """
    if node.type not in _FUNCTION_NODES:
        return None
    declarator = _child(node, "declarator")
    if declarator is None:
        return None
    if declarator.type != "function_declarator":
        if node.type == "function_definition":
            name_node = _find_identifier(declarator)
            if name_node is None:
                return None
            return FunctionInfo(
                name=_text(name_node, src),
                file=file,
                start_line=node.start_point.row + 1,
                end_line=_end_line(node),
                signature=None,
            )
        return None
    name_node = _find_identifier(declarator)
    if name_node is None:
        return None
    name = _text(name_node, src)

    return_type = _return_type(node, src)
    signature: SignatureInfo | None = None
    if return_type is not None:
        parameters: list[ParamInfo] = []
        params = _child(declarator, "parameters")
        if params is not None:
            for child in params.children:
                if child.type == "parameter_declaration":
                    info = _param_info(child, src)
                    if info is not None:
                        parameters.append(info)
        decl_text = _text(declarator, src)
        signature = SignatureInfo(
            return_type=return_type,
            parameters=parameters,
            text=f"{return_type} {decl_text}",
        )
    return FunctionInfo(
        name=name,
        file=file,
        start_line=node.start_point.row + 1,
        end_line=_end_line(node),
        signature=signature,
    )
