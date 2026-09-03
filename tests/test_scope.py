"""Scope Control 测试（Index Pipeline Reliability）。

Case 6: ignore 目录生效（index 只含 src）
Case 7: 跨工程自动建议（GD32 主工程 + STM32 demo → 建议 ignore）
+ 解析 / 匹配 / .py 比例 / 目录名规则
"""

from __future__ import annotations

from agentx.plan.service import enrich_index
from agentx.scope.ignore import (
    is_ignored,
    load_ignore_patterns,
    parse_ignore_file,
    scope_filter,
)
from agentx.scope.suggest import suggest_ignores


def _uvprojx(cpu: str, name: str) -> str:
    return f"""<Project><Targets><Target>
<TargetName>{name}</TargetName>
<TargetOption><TargetCommonOption><Cpu>{cpu}</Cpu><Device>{cpu}</Device></TargetCommonOption></TargetOption>
<Groups></Groups>
</Target></Targets><SelectTargetNo>0</SelectTargetNo></Project>"""


# ---------- 解析 ----------


def test_parse_ignore_file() -> None:
    text = """# AgentX Scope
ignore:
  - LT758_DEMO/**
  - tools/**

# 注释行
other:
  - not_used
"""
    patterns = parse_ignore_file(text)
    assert patterns == ["LT758_DEMO/**", "tools/**"]


def test_parse_ignore_empty_and_missing(tmp_path) -> None:
    assert load_ignore_patterns(tmp_path) == []
    (tmp_path / ".agentxignore").write_text("ignore:\n", encoding="utf-8")
    assert load_ignore_patterns(tmp_path) == []


# ---------- 匹配 ----------


def test_is_ignored_patterns() -> None:
    pats = ["LT758_DEMO/**", "tools/**", "*.py", "Documents"]
    assert is_ignored("LT758_DEMO/demo.c", pats)
    assert is_ignored("LT758_DEMO/sub/demo.c", pats)
    assert is_ignored("tools/scan.py", pats)
    assert is_ignored("main.py", pats)
    assert is_ignored("Documents/doc.c", pats)
    assert is_ignored("Documents", pats)
    assert not is_ignored("Drivers/BSP/KEY/key.c", pats)
    assert not is_ignored("toolsx/main.c", pats)  # 非前缀命中


def test_scope_filter() -> None:
    (tmp := __import__("tempfile").mkdtemp())
    from pathlib import Path

    root = Path(tmp)
    (root / ".agentxignore").write_text("ignore:\n  - demo/**\n  - tools/**\n", encoding="utf-8")
    paths = ["src/main.c", "demo/x.c", "tools/t.py", "Drivers/key.c"]
    assert scope_filter(root, paths) == ["src/main.c", "Drivers/key.c"]


# ---------- Case 6: ignore 目录生效（enrich 全链路） ----------


def _make_project(root, dirs: list[str], files: dict[str, str]) -> None:
    for d in dirs:
        (root / d).mkdir(parents=True, exist_ok=True)
    for rel, content in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")


def test_enrich_respects_scope(tmp_path) -> None:
    _make_project(
        tmp_path,
        ["src", "demo", "tools"],
        {
            "src/main.c": "int main(void) { return 0; }\n",
            "src/key.c": "int key_scan(void) { return 1; }\n",
            "demo/led_demo.c": "int led_demo(void) { return 0; }\n",
            "tools/build.py": "print('tool')\n",
        },
    )
    (tmp_path / ".agentxignore").write_text(
        "ignore:\n  - demo/**\n  - tools/**\n", encoding="utf-8"
    )
    index, _ = enrich_index(tmp_path)  # filescan（测试隔离 CodeGraph）
    paths = [f.path for f in index.files]
    assert any(p.endswith("src/main.c") for p in paths)
    assert any(p.endswith("src/key.c") for p in paths)
    assert not any("demo" in p for p in paths)
    assert not any("tools" in p for p in paths)
    # 符号也只来自 scope 内
    assert all("demo" not in str(s.get("file", "")) for s in index.symbols)


# ---------- Case 7: 跨工程自动建议 ----------


def test_suggest_ignores_keil_cpu_difference(tmp_path) -> None:
    (tmp_path / "GD32F427.uvprojx").write_text(
        _uvprojx("GD32F427VET6", "GD32F427"), encoding="utf-8"
    )
    demo = tmp_path / "LT758_DEMO"
    demo.mkdir()
    (demo / "GD32F103.uvprojx").write_text(_uvprojx("GD32F103C8T6", "GD32F103"), encoding="utf-8")
    (demo / "main.c").write_text("int main(void) { return 0; }\n", encoding="utf-8")
    suggestions = suggest_ignores(tmp_path)
    hit = [s for s in suggestions if s["path"] == "LT758_DEMO"]
    assert hit
    assert "CPU" in hit[0]["reason"]


def test_suggest_ignores_py_tool_dir(tmp_path) -> None:
    tools = tmp_path / "tools"
    tools.mkdir()
    for i in range(3):
        (tools / f"tool{i}.py").write_text("print('x')\n", encoding="utf-8")
    (tools / "readme.md").write_text("doc\n", encoding="utf-8")
    suggestions = suggest_ignores(tmp_path)
    hit = [s for s in suggestions if s["path"] == "tools"]
    assert hit and "Python" in hit[0]["reason"]


def test_suggest_ignores_name_hint(tmp_path) -> None:
    for name in ("demo", "Documents", "examples"):
        d = tmp_path / name
        d.mkdir()
        (d / "x.c").write_text("int x(void) { return 0; }\n", encoding="utf-8")
    suggestions = suggest_ignores(tmp_path)
    paths = {s["path"] for s in suggestions}
    assert paths == {"demo", "Documents", "examples"}


def test_suggest_no_false_positive_on_real_dirs(tmp_path) -> None:
    (tmp_path / "User").mkdir()
    (tmp_path / "User" / "main.c").write_text("int main(void) { return 0; }\n", encoding="utf-8")
    (tmp_path / "Drivers").mkdir()
    (tmp_path / "Drivers" / "key.c").write_text(
        "int key_scan(void) { return 0; }\n", encoding="utf-8"
    )
    assert suggest_ignores(tmp_path) == []
