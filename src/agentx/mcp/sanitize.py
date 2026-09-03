"""MCP response 序列化防护层（Phase 7.9.1）。

问题：Windows filesystem 经 surrogateescape 读取的文件路径/文件名可能含
\\uDC80-\\uDCFF 低位代理字符；MCP SDK 返回时做 pydantic/JSON 序列化，
utf-8 编码 surrogate 抛 UnicodeEncodeError（PydanticSerializationError）
→ 整个 response 失败（即使 Index 已成功生成）。

原则（任务要求）：
- 只在 MCP 输出边界清洗；内部数据（index.json / semantic / filesystem）不动
- 任何进入 response 的字符串保证可 utf-8 编码
- 单字段清洗失败不影响整体（清洗本身绝不抛异常）

规则：
- surrogate 字符 → '?'（"\\udc80abc" → "?abc"）
- bytes → utf-8 errors=replace 解码后再清洗
- 容器递归清洗；非 str/bytes 原样保留
"""

from __future__ import annotations

from typing import Any, TypeVar, overload

T = TypeVar("T")


def sanitize_str(s: str) -> str:
    """编码安全清洗：所有非法 UTF-8 序列（含 surrogate）替换为 '?'。"""
    if not isinstance(s, str):
        return s
    try:
        # errors='replace'：surrogate/非法序列编码时输出 '?'
        return s.encode("utf-8", errors="replace").decode("utf-8")
    except Exception:
        return ""


@overload
def sanitize_value(value: dict[str, Any]) -> dict[str, Any]: ...


@overload
def sanitize_value[T](value: T) -> T: ...


def sanitize_value(value: Any) -> Any:
    """递归清洗任意值：str→编码安全；bytes→replace 解码；容器递归；其余原样。

    保证：返回值可通过任何 JSON / pydantic 序列化（无编码异常）。
    """
    if isinstance(value, str):
        return sanitize_str(value)
    if isinstance(value, bytes):
        return sanitize_str(value.decode("utf-8", errors="replace"))
    if isinstance(value, dict):
        return {
            sanitize_str(k) if isinstance(k, str) else k: sanitize_value(v)
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [sanitize_value(v) for v in value]
    if isinstance(value, tuple):
        return tuple(sanitize_value(v) for v in value)
    if isinstance(value, set):
        return {sanitize_value(v) for v in value}
    return value
