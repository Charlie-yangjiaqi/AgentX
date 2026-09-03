"""Semantic Extractor 统一入口（Phase 7.6 / 7.8.1 稳定性加固 / 7.9 容错）。

C/H Source → Tree-sitter C Parser → AST → AgentX Semantic Extractor
→ FileSemantics（functions/structs/enums/macros）。

零 LLM、确定性、不存源码；单文件解析失败只记录 errors，不影响整体。

Phase 7.8.1 稳定性：
- 每个文件独立创建 Parser（不跨文件复用——批量解析状态污染修复）
- 大文件保护：超过 max_file_size_mb 跳过 AST 提取（保留 CodeGraph symbol，
  写入 semantic_skip error），防超大字体/图片数组阻塞解析

Phase 7.9 容错：
- tree-sitter>=0.26 版本门禁：0.26.0 Python binding 有 native 内存 bug
  （累积堆损坏 → SIGSEGV），解析前检测并跳过，输出结构化错误
  （recoverable=true，修复环境后重建），禁止进入有 bug 的 native parse
- 单节点提取隔离：每个 struct/enum/function/macro 节点独立 try/except，
  单个节点异常只记录 semantic_partial error，不中断整文件剩余提取
"""

from __future__ import annotations

import re
from importlib import metadata
from typing import Any

from tree_sitter import Node

from agentx.semantic.enum import extract_enum
from agentx.semantic.function import extract_function
from agentx.semantic.macro import extract_macro
from agentx.semantic.runtime import SemanticUnavailableError
from agentx.semantic.struct import extract_struct
from agentx.semantic.types import FileSemantics
from agentx.semantic.worker import structured_error

try:
    import tree_sitter_c
    from tree_sitter import Language, Parser

    _C_LANGUAGE = Language(tree_sitter_c.language())

    def _make_parser() -> Any:
        return Parser(_C_LANGUAGE)

    _PARSER_SOURCE = "tree_sitter_c"
except ImportError:
    # 兼容路径：环境缺少 tree-sitter-c 时，尝试 tree-sitter-language-pack
    # （系统 Python 常见，内含 C grammar）。
    try:
        from tree_sitter_language_pack import (  # type: ignore[import-not-found]
            get_parser as _pack_get_parser,
        )

        def _make_parser() -> Any:
            return _pack_get_parser("c")

        _PARSER_SOURCE = "tree_sitter_language_pack"
    except ImportError:
        # 全部 parser 不可用：明确失败（调用方写入 Index capabilities/errors），
        # 禁止静默 fallback 生成"看似成功"的 Index。
        raise SemanticUnavailableError(
            "Tree-sitter 不可用：需要 tree-sitter + tree-sitter-c"
            "（或 tree-sitter-language-pack）。运行 agentx doctor 查看诊断"
        ) from None

# tree-sitter 0.26.0 Python binding 有 native 内存 bug（累积堆损坏 → SIGSEGV）。
# pyproject 锁定 >=0.25.2,<0.26；运行环境若被装成 0.26+，parse 前拦截，
# 输出可恢复的结构化错误而不是让 worker 进程 native crash。
_TREE_SITTER_INCOMPATIBLE = False
_TREE_SITTER_VERSION: str | None = None
try:
    _TREE_SITTER_VERSION = metadata.version("tree-sitter")
    parts = tuple(int(p) for p in _TREE_SITTER_VERSION.split(".")[:2] if p.isdigit())
    _TREE_SITTER_INCOMPATIBLE = len(parts) == 2 and parts >= (0, 26)
except metadata.PackageNotFoundError:
    pass


def max_file_size_bytes() -> int:
    """当前生效的大文件限制（字节）。配置：config.semantic > env > 默认 5MB。"""
    from agentx.config.config import load_config, resolve_semantic_config

    try:
        cfg = load_config()
        sem_cfg = resolve_semantic_config(cfg)
    except Exception:
        sem_cfg = resolve_semantic_config()
    return int(sem_cfg["max_file_size_mb"] * 1024 * 1024)


def extract_file_semantics(file: str, source: str) -> FileSemantics:
    """解析单个文件并提取语义（单遍；解析失败 → errors，不抛异常）。

    每个文件独立创建 Parser，parse 后释放——不跨文件复用（Phase 7.8.1）。

    Phase 7.9：tree-sitter>=0.26 时禁止进入 native parse（SIGSEGV 风险），
    输出结构化可恢复错误。
    """
    result = FileSemantics()
    if _TREE_SITTER_INCOMPATIBLE:
        result.errors.append(
            structured_error(
                "semantic_skip",
                file,
                "parser",
                "tree_sitter_version_incompatible",
                True,
                version=_TREE_SITTER_VERSION,
                hint=(
                    "tree-sitter 0.26.x Python binding 有 native 内存 bug；"
                    "请降级到 0.25.x 后重新生成 Index（uv tool upgrade agentx "
                    "--reinstall 或 uv pip install 'tree-sitter>=0.25.2,<0.26'）"
                ),
            )
        )
        return result
    src = source.encode("utf-8")
    limit = max_file_size_bytes()
    if len(src) > limit:
        result.errors.append(
            structured_error(
                "semantic_skip",
                file,
                "read",
                "source_too_large",
                False,
                size=len(src),
                limit_bytes=limit,
            )
        )
        return result
    parser = _make_parser()
    try:
        tree = parser.parse(src)
    except Exception as e:  # tree-sitter 解析异常（极少见）
        result.errors.append(
            structured_error(
                "semantic_skip",
                file,
                "parser",
                f"{type(e).__name__}: {e}"[:200],
                True,
            )
        )
        return result
    root = tree.root_node
    if root.has_error and _has_real_error(root):
        # has_error 保守标志：preproc 结构（如 extern "C" 与 #ifdef 配对）会置位
        # 但解析结果完整。仅在存在真实 ERROR/missing 节点时记录，避免误报刷屏。
        result.errors.append(
            structured_error(
                "semantic_partial",
                file,
                "parser",
                "syntax_error",
                False,
            )
        )

    # 单遍遍历顶层节点；type_definition 内的 struct/enum 由 _walk 处理
    _walk_children(root, src, file, result)
    # Phase 7.7.3/7.7.4：函数地址绑定 + 字段使用（同一 AST，零额外 parse）
    result.bindings, result.field_usage = _extract_type_refs(root, src, file)
    return result


def _has_real_error(root: Node) -> bool:
    """全树扫描真实解析错误：ERROR 节点或 missing 终结符。"""
    stack = [root]
    while stack:
        node = stack.pop()
        if node.type == "ERROR" or node.is_missing:
            return True
        stack.extend(node.children)
    return False


def _walk_children(node: Node, src: bytes, file: str, result: FileSemantics) -> None:
    for child in node.children:
        try:
            _walk_child(child, src, file, result)
        except Exception as e:  # 单节点提取隔离：不中断整文件剩余提取
            result.errors.append(
                structured_error(
                    "semantic_partial",
                    file,
                    "extract",
                    f"{type(e).__name__}: {e}"[:200],
                    True,
                )
            )


def _walk_child(child: Node, src: bytes, file: str, result: FileSemantics) -> None:
    if child.type in ("preproc_ifdef", "preproc_if", "preproc_else", "preproc_elif"):
        # 预处理器容器（如 #ifndef 头文件守卫）：内容仍是 C 语义，递归遍历
        _walk_children(child, src, file, result)
    elif child.type == "type_definition":
        # typedef struct/enum：提取其体内的 struct/enum（带 typedef 别名）
        for inner in child.children:
            if inner.type in ("struct_specifier", "union_specifier"):
                struct_info = extract_struct(inner, src, file)
                if struct_info is not None:
                    result.structs.append(struct_info)
            elif inner.type == "enum_specifier":
                enum_info = extract_enum(inner, src, file)
                if enum_info is not None:
                    result.enums.append(enum_info)
    elif child.type in ("struct_specifier", "union_specifier"):
        struct_info = extract_struct(child, src, file)
        if struct_info is not None:
            result.structs.append(struct_info)
    elif child.type == "enum_specifier":
        enum_info = extract_enum(child, src, file)
        if enum_info is not None:
            result.enums.append(enum_info)
    elif child.type in ("preproc_def", "preproc_function_def"):
        macro_info = extract_macro(child, src, file)
        if macro_info is not None:
            result.macros.append(macro_info)
    elif child.type in ("function_definition", "declaration"):
        fn_info = extract_function(child, src, file)
        if fn_info is not None:
            result.functions.append(fn_info)
        else:
            # declaration 内可能内联 struct/enum（如 `struct Foo {...} var;`）
            _walk_children(child, src, file, result)


# ---------- Phase 7.7.3：函数地址绑定提取（语法层事实） ----------

# 注册类调用名模式：register_xxx / Register( / attach / Attach( / bind / hook ...
# 排除 set/add/init（太泛，set(obj, x) 等普通调用易误报——merge 层仍按
# 函数符号过滤，但减少 bindings 层噪音）
_REGISTER_CALL_RE = re.compile(
    r"^(?:register|attach|bind|hook|install|assign|map)(?:_|\(|$)",
    re.IGNORECASE,
)


# 语句容器节点：可能包含函数地址绑定的递归深入集合（性能剪枝——其余跳过）
_BINDING_CONTAINERS = {
    "translation_unit",
    "function_definition",
    "compound_statement",
    "expression_statement",
    "declaration",
    "init_declarator",
    "initializer_list",
    "initializer_pair",
    "assignment_expression",
    "call_expression",
    "if_statement",
    "else_clause",
    "for_statement",
    "while_statement",
    "do_statement",
    "switch_statement",
    "case_statement",
    "return_statement",
    "conditional_expression",
    "labeled_statement",
    "expression_list",
    "argument_list",
    "parenthesized_expression",
    "cast_expression",
    "pointer_expression",
    "field_expression",
    "subscript_expression",
    "binary_expression",
    "comma_expression",
    "struct_specifier",
    "union_specifier",
    "preproc_ifdef",
    "preproc_if",
    "preproc_else",
    "preproc_elif",
    # ERROR 节点：语法残缺处内部仍可能有有效绑定（防子树截断丢失）
    "ERROR",
}


def _text_of(node: Node, src: bytes) -> str:
    return src[node.start_byte : node.end_byte].decode(errors="replace").strip()


def _binding_target(left: Node, src: bytes) -> tuple[str, str] | None:
    """赋值左侧 → (via, 描述)。field_expression → field_assign（带字段名）；
    下标 → table_assign；标识符 → var_assign。"""
    if left.type == "field_expression":
        field = left.child_by_field_name("field")
        fname = _text_of(field, src) if field is not None else ""
        return "field_assign", fname
    if left.type == "subscript_expression":
        return "table_assign", ""
    if left.type == "identifier":
        return "var_assign", ""
    return None


def _right_value_identifier(right: Node, src: bytes) -> str | None:
    """赋值右侧的函数地址：identifier 或 &identifier（pointer_expression）。"""
    if right.type == "identifier":
        return _text_of(right, src) or ""
    if right.type == "pointer_expression":
        arg = right.child_by_field_name("argument")
        if arg is not None and arg.type == "identifier":
            return _text_of(arg, src) or ""
    return None


def _extract_type_refs(
    root: Node, src: bytes, file: str
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """提取函数地址绑定 + 字段使用记录（同一遍遍历，零额外 parse）。

    绑定（Phase 7.7.3）三种 via：
    - field_assign：  .handler = Func / .cb = &Func（带字段名）
    - table_assign：  table[i] = Func（回调表项绑定）
    - register_call： register_xxx(Func)（注册 API 参数绑定）

    字段使用（Phase 7.7.4）：field_expression 谁读/谁写（access=read|write）
    + 所在函数。字段名级聚合（不做类型推断，保守——见 type_extractor）。

    语义：绑定是"函数被注册/绑定到此处"的事实，不承诺"此处会调用它"。
    性能：只深入语句容器节点（函数体/控制流/初始化器），跳过叶子表达式。
    """
    bindings: list[dict[str, Any]] = []
    usage: list[dict[str, Any]] = []

    def _walk(node: Node, caller_hint: str | None) -> None:
        for child in node.children:
            if child.type == "assignment_expression":
                left = child.child_by_field_name("left")
                right = child.child_by_field_name("right")
                if left is not None and right is not None:
                    target = _binding_target(left, src)
                    fn = _right_value_identifier(right, src)
                    if target is not None and fn is not None:
                        bindings.append(
                            {
                                "name": fn,
                                "via": target[0],
                                "file": file,
                                "line": child.start_point.row + 1,
                                "caller_hint": caller_hint,
                                "field": target[1],
                            }
                        )
                    # 赋值左侧字段 = 写
                    if left.type == "field_expression":
                        fld = left.child_by_field_name("field")
                        if fld is not None:
                            usage.append(
                                {
                                    "field": _text_of(fld, src) or "",
                                    "access": "write",
                                    "file": file,
                                    "line": child.start_point.row + 1,
                                    "function": caller_hint,
                                }
                            )
                _walk(child, caller_hint)  # 嵌套赋值（如 结构体嵌套初始化后赋值）
                continue
            if child.type == "field_expression":
                # 非赋值左侧的字段访问 = 读（Node wrapper 每次新建，用字节位置判左值）
                fld = child.child_by_field_name("field")
                parent = child.parent
                is_left = False
                if (
                    fld is not None
                    and parent is not None
                    and parent.type == "assignment_expression"
                ):
                    left = parent.child_by_field_name("left")
                    if (
                        left is not None
                        and left.start_byte == child.start_byte
                        and left.end_byte == child.end_byte
                    ):
                        is_left = True
                if fld is not None and not is_left:
                    usage.append(
                        {
                            "field": _text_of(fld, src) or "",
                            "access": "read",
                            "file": file,
                            "line": child.start_point.row + 1,
                            "function": caller_hint,
                        }
                    )
                _walk(child, caller_hint)
                continue
            if child.type == "call_expression":
                fn_name = _extract_call_name(child, src)
                args = child.child_by_field_name("arguments")
                if args is None:
                    args = child.child_by_field_name("argument_list")
                if fn_name is not None and args is not None and _REGISTER_CALL_RE.match(fn_name):
                    for arg in args.children:
                        # 注册 API 参数：Func 或 &Func
                        fn_arg = _right_value_identifier(arg, src)
                        if fn_arg is not None:
                            bindings.append(
                                {
                                    "name": fn_arg,
                                    "via": "register_call",
                                    "file": file,
                                    "line": child.start_point.row + 1,
                                    "caller_hint": caller_hint,
                                    "field": "",
                                }
                            )
                _walk(child, caller_hint)
                continue
            if child.type == "initializer_list":
                # 位置初始化 {fn1, fn2}（回调表常用）：直接标识符 → table_assign
                for c in child.children:
                    fn_pos = _right_value_identifier(c, src)
                    if fn_pos is not None:
                        bindings.append(
                            {
                                "name": fn_pos,
                                "via": "table_assign",
                                "file": file,
                                "line": child.start_point.row + 1,
                                "caller_hint": caller_hint,
                                "field": "",
                            }
                        )
                _walk(child, caller_hint)
                continue
            if child.type == "initializer_pair":
                # designated initializer（.handler = fn / [i] = fn）：回调表初始化
                value = child.child_by_field_name("value")
                if value is not None:
                    fn_init = _right_value_identifier(value, src)
                    if fn_init is not None:
                        bindings.append(
                            {
                                "name": fn_init,
                                "via": "field_assign",
                                "file": file,
                                "line": child.start_point.row + 1,
                                "caller_hint": caller_hint,
                                "field": "",
                            }
                        )
                _walk(child, caller_hint)
                continue
            # 语句容器（函数体/控制流/声明/初始化器）：可能包含绑定，深入；
            # 其余节点（标识符/字面量/运算符等）不递归（性能剪枝）
            if child.type in _BINDING_CONTAINERS:
                if child.type == "function_definition":
                    _walk(child, _function_name(child, src))
                else:
                    _walk(child, caller_hint)

    _walk(root, None)
    return bindings, usage


def _function_name(node: Node, src: bytes) -> str | None:
    """function_definition 的函数名（function_declarator 的 declarator 字段）。"""
    decl = node.child_by_field_name("declarator")
    if decl is None or decl.type != "function_declarator":
        return None
    name = decl.child_by_field_name("declarator")
    if name is not None and name.type == "identifier":
        return _text_of(name, src) or ""
    return None


def _extract_call_name(node: Node, src: bytes) -> str | None:
    """call_expression 的函数名（函数名/字段访问的最后一个标识符）。"""
    fn = node.child_by_field_name("function")
    if fn is None:
        return None
    if fn.type == "identifier":
        return _text_of(fn, src) or ""
    if fn.type == "field_expression":
        field = fn.child_by_field_name("field")
        if field is not None:
            return _text_of(field, src) or ""
    stack = [fn]
    while stack:
        cur = stack.pop()
        if cur.type in ("identifier", "field_identifier"):
            return _text_of(cur, src) or ""
        stack.extend(cur.children)
    return None
