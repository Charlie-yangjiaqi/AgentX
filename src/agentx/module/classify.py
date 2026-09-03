"""Module 类型推断：基于模块名 + 文件路径（确定性，零 LLM）。"""

from __future__ import annotations

_LAYER_RULES: list[tuple[tuple[str, ...], str]] = [
    (("bsp",), "bsp"),
    (
        (
            "middleware",
            "middlewares",
            "thirdparty",
            "third_party",
            "freertos",
            "rtos",
            "lvgl",
            "fatfs",
            "lwip",
            "cmsis",
            "segger",
            "touchgfx",
            "emwin",
            "rtthread",
        ),
        "middleware",
    ),
    (("hal",), "hal"),
    (("driver", "drivers", "peripheral", "peripherals"), "driver"),
    (("app", "application", "user", "ui", "gui", "screen", "core", "main"), "app"),
    (("lib", "library", "libraries"), "lib"),
]


def classify_module(name: str, files: list[str]) -> str:
    """模块层类型：bsp > middleware > hal > driver > app > lib > unknown。"""
    path = (files[0] if files else "").replace("\\", "/").casefold()
    n = name.casefold()
    for keywords, layer in _LAYER_RULES:
        for kw in keywords:
            if kw in path or kw in n:
                return layer
    return "unknown"
