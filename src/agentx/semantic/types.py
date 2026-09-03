"""Semantic 提取结果的数据结构（纯数据，无解析逻辑）。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class ParamInfo:
    name: str
    type: str


@dataclass
class SignatureInfo:
    return_type: str
    parameters: list[ParamInfo]
    text: str


@dataclass
class MemberInfo:
    name: str
    type: str
    line: int
    # Phase 7.7.4：函数指针字段标记（declarator 含 function_declarator）
    is_function_pointer: bool = False


@dataclass
class EnumMemberInfo:
    name: str
    line: int
    value: str | None = None
    value_expr: str | None = None


@dataclass
class FunctionInfo:
    name: str
    file: str
    start_line: int
    end_line: int
    signature: SignatureInfo | None


@dataclass
class StructInfo:
    name: str
    file: str
    start_line: int
    end_line: int
    members: list[MemberInfo] = field(default_factory=list)


@dataclass
class EnumInfo:
    name: str
    file: str
    start_line: int
    end_line: int
    members: list[EnumMemberInfo] = field(default_factory=list)


@dataclass
class MacroInfo:
    name: str
    file: str
    line: int
    value: str | None = None
    value_expr: str | None = None
    # Phase 7.7.4：宏分类 constant | function | flag（条件编译宏）
    kind: str = "constant"


@dataclass
class FileSemantics:
    """单个文件的语义提取结果。"""

    functions: list[FunctionInfo] = field(default_factory=list)
    structs: list[StructInfo] = field(default_factory=list)
    enums: list[EnumInfo] = field(default_factory=list)
    macros: list[MacroInfo] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    # Phase 7.7.3：函数地址绑定（语法层事实——右值标识符赋值/注册调用），
    # 是否函数符号由 merge 层用 index.symbols 过滤（本层不猜身份）
    bindings: list[dict[str, Any]] = field(default_factory=list)
    # Phase 7.7.4：字段使用记录（field_expression → 谁读/谁写，语法层事实）
    field_usage: list[dict[str, Any]] = field(default_factory=list)
