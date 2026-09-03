"""Build Reality：Keil 工程解析 + Build 数据模型 + 查询接口。

唯一真相源：所有 Keil 解析都在本包（graph.py / query / planner 均委托这里），
避免出现两套解析导致数据不一致。
"""

from agentx.build.context import (
    build_query,
    build_query_from_info,
    build_status,
    build_status_from_info,
    file_build,
    file_in_build,
)
from agentx.build.keil_parser import (
    KeilFile,
    KeilGroup,
    KeilProject,
    KeilTarget,
    parse_keil_project,
)

__all__ = [
    "KeilFile",
    "KeilGroup",
    "KeilProject",
    "KeilTarget",
    "build_query",
    "build_query_from_info",
    "build_status",
    "build_status_from_info",
    "file_build",
    "file_in_build",
    "parse_keil_project",
]