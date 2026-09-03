"""Phase 7.6：Code Semantic Detail Index（Tree-sitter 版）。

CodeGraph 负责项目级关系，Tree-sitter 负责文件级语法语义，
AgentX 负责融合成工程知识。零 LLM、不存源码、不执行宏。
"""

from agentx.semantic.extractor import extract_file_semantics
from agentx.semantic.types import (
    EnumInfo,
    EnumMemberInfo,
    FileSemantics,
    FunctionInfo,
    MacroInfo,
    MemberInfo,
    ParamInfo,
    SignatureInfo,
    StructInfo,
)

__all__ = [
    "EnumInfo",
    "EnumMemberInfo",
    "FileSemantics",
    "FunctionInfo",
    "MacroInfo",
    "MemberInfo",
    "ParamInfo",
    "SignatureInfo",
    "StructInfo",
    "extract_file_semantics",
]
