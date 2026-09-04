"""Phase 4/5：Build Reality（四态 + 构建系统解析）与 IndexSyncManager（L0-L4 分级）。"""

from __future__ import annotations

from pathlib import Path

from agentx.index.index import IndexStatus, index_status, load_index
from agentx.index.sync import (
    ChangeLevel,
    classify_diff,
    classify_file_changes,
    sync_index,
)
from agentx.plan.service import enrich_index
from agentx.understanding.graph import _detect_build_info


def _make_c_project(tmp_path: Path) -> None:
    (tmp_path / "main.c").write_text("int main(void) { return 0; }\n", encoding="utf-8")
    (tmp_path / "param.c").write_text("// TODO\n", encoding="utf-8")
    (tmp_path / "param.h").write_text("#ifndef P\n#define P\n#endif\n", encoding="utf-8")


# ---------- Build Reality：构建系统探测 ----------


def test_build_info_makefile(tmp_path: Path) -> None:
    (tmp_path / "Makefile").write_text("all:\n\tcc main.c param.c\n", encoding="utf-8")
    info = _detect_build_info(tmp_path)
    assert info["build_source"] == "make"
    assert info["has_build_config"] is True
    names = {e["file"] for e in info["compiled_files"]}
    assert "main.c" in names


def test_build_info_compile_commands_priority(tmp_path: Path) -> None:
    (tmp_path / "Makefile").write_text("all:\n", encoding="utf-8")
    (tmp_path / "compile_commands.json").write_text(
        '[{"file": "src/main.c", "command": "cc -c src/main.c"}]', encoding="utf-8"
    )
    info = _detect_build_info(tmp_path)
    assert info["build_source"] == "compile_commands"
    assert info["system"] == "compile_commands.json"


def test_build_info_keil_uvprojx(tmp_path: Path) -> None:
    (tmp_path / "app.uvprojx").write_text(
        "<?xml version='1.0'?><Project>"
        "<Targets><Target><Groups><Group><Files>"
        "<File><FileName>src/main.c</FileName></File>"
        "<File><FileName>test/test.c</FileName>"
        "<FileConfiguration><FileOption><CommonProperty>"
        "<IncludeInBuild>0</IncludeInBuild></CommonProperty></FileOption>"
        "</FileConfiguration></File>"
        "</Files></Group></Groups></Target></Targets></Project>",
        encoding="utf-8",
    )
    info = _detect_build_info(tmp_path)
    assert info["build_source"] == "keil"
    compiled = {e["file"] for e in info["compiled_files"]}
    excluded = {e["file"] for e in info["excluded_files"]}
    assert "src/main.c" in compiled
    assert "test/test.c" in excluded


def test_build_info_iar_ewp(tmp_path: Path) -> None:
    (tmp_path / "app.ewp").write_text(
        "<?xml version='1.0'?><project><configuration><file>"
        "<name>$PROJ_DIR$/src/main.c</name></file>"
        "<file><name>$PROJ_DIR$/src/skip.c</name><excluded>yes</excluded></file>"
        "</configuration></project>",
        encoding="utf-8",
    )
    info = _detect_build_info(tmp_path)
    assert info["build_source"] == "iar"
    compiled = {e["file"] for e in info["compiled_files"]}
    excluded = {e["file"] for e in info["excluded_files"]}
    assert "src/main.c" in compiled
    assert "src/skip.c" in excluded


def test_build_info_unknown_without_config(tmp_path: Path) -> None:
    info = _detect_build_info(tmp_path)
    assert info["has_build_config"] is False
    assert info["build_source"] is None


# ---------- compile_status 四态落库 ----------


def test_enrich_compile_status_four_states(tmp_path: Path) -> None:
    _make_c_project(tmp_path)
    (tmp_path / "Makefile").write_text("all:\n\tcc main.c\n", encoding="utf-8")
    index, _ = enrich_index(tmp_path)
    by_path = {f.path: f for f in index.files}
    # Makefile 参与编译 → compiled
    assert by_path["main.c"].compile_status == "compiled"
    assert by_path["main.c"].content_hash
    # 有构建配置但未收录 → not_compiled
    assert by_path["param.c"].compile_status == "not_compiled"
    # build_source 落库
    assert by_path["main.c"].build_source == "make"
    assert index.build_info["build_source"] == "make"


def test_enrich_compile_status_unknown_without_build(tmp_path: Path) -> None:
    _make_c_project(tmp_path)
    index, _ = enrich_index(tmp_path)
    by_path = {f.path: f for f in index.files}
    assert by_path["main.c"].compile_status == "unknown"
    assert by_path["param.c"].compile_status == "unknown"


# ---------- Impact Analyzer：diff 分级 ----------


def _index_with_symbols(tmp_path: Path) -> object:
    from datetime import UTC, datetime

    from agentx.index.index import ProjectIndex

    return ProjectIndex(
        project_fingerprint="fp",
        index_version="1.3",
        generated_at=datetime(2026, 1, 1, tzinfo=UTC),
        files=[{"path": "param.c"}, {"path": "param.h"}],
        symbols=[
            {
                "name": "param_init",
                "type": "function",
                "file": "param.c",
                "start_line": 16,
                "end_line": 19,
            },
            {
                "name": "param_set",
                "type": "function",
                "file": "param.c",
                "start_line": 21,
                "end_line": 52,
            },
        ],
    )


def test_classify_diff_no_change() -> None:
    assert classify_diff("", _index_with_symbols(Path(".")))[0] == ChangeLevel.L0


def test_classify_diff_function_internal_L1() -> None:
    index = _index_with_symbols(Path("."))
    diff = (
        "diff --git a/param.c b/param.c\n"
        "index 111..222 100644\n"
        "--- a/param.c\n"
        "+++ b/param.c\n"
        "@@ -17,2 +17,2 @@\n"
        "  内部逻辑\n"
        "-  return 0;\n"
        "+  return 1;\n"
    )
    level, changed = classify_diff(diff, index)
    assert level == ChangeLevel.L1
    assert changed == []


def test_classify_diff_signature_change_L2() -> None:
    index = _index_with_symbols(Path("."))
    diff = (
        "diff --git a/param.c b/param.c\n"
        "--- a/param.c\n"
        "+++ b/param.c\n"
        "@@ -16,3 +16,3 @@\n"
        "-void param_init(void) {\n"
        "+int param_init(void) {\n"
        "  body\n"
        "}\n"
    )
    level, _ = classify_diff(diff, index)
    assert level == ChangeLevel.L2


def test_classify_diff_new_file_L3() -> None:
    index = _index_with_symbols(Path("."))
    diff = (
        "diff --git a/sensor.c b/sensor.c\n"
        "new file mode 100644\n"
        "index 000..111\n"
        "--- /dev/null\n"
        "+++ b/sensor.c\n"
        "@@ -0,0 +1,2 @@\n"
        "+int sensor_read(void) { return 0; }\n"
    )
    level, changed = classify_diff(diff, index)
    assert level == ChangeLevel.L3
    assert "sensor.c" in changed


def test_classify_diff_include_change_L3() -> None:
    index = _index_with_symbols(Path("."))
    diff = (
        "diff --git a/param.c b/param.c\n"
        "--- a/param.c\n"
        "+++ b/param.c\n"
        "@@ -2,1 +2,1 @@\n"
        '-#include "param.h"\n'
        '+#include "sensor.h"\n'
    )
    level, _ = classify_diff(diff, index)
    assert level == ChangeLevel.L3


def test_classify_diff_build_config_L4() -> None:
    index = _index_with_symbols(Path("."))
    diff = (
        "diff --git a/Makefile b/Makefile\n"
        "--- a/Makefile\n"
        "+++ b/Makefile\n"
        "@@ -1,2 +1,3 @@\n"
        " all:\n"
        "-	cc main.c\n"
        "+	cc main.c sensor.c\n"
    )
    level, _ = classify_diff(diff, index)
    assert level == ChangeLevel.L4


# ---------- 无 git 降级：文件 hash 比对 ----------


def test_classify_file_changes(tmp_path: Path) -> None:
    _make_c_project(tmp_path)
    index, _ = enrich_index(tmp_path)
    assert classify_file_changes(tmp_path, index)[0] == ChangeLevel.L0
    (tmp_path / "param.c").write_text("int changed;\n", encoding="utf-8")
    level, changed, classified = classify_file_changes(tmp_path, index)
    assert level == ChangeLevel.L3  # 保守升级
    assert "param.c" in changed
    assert "param.c" in classified["modified"]


def test_classify_file_changes_build_config_L4(tmp_path: Path) -> None:
    _make_c_project(tmp_path)
    (tmp_path / "Makefile").write_text("all:\n\tcc main.c\n", encoding="utf-8")
    index, _ = enrich_index(tmp_path)
    (tmp_path / "Makefile").write_text("all:\n\tcc main.c sensor.c\n", encoding="utf-8")
    level, changed, _ = classify_file_changes(tmp_path, index)
    assert level == ChangeLevel.L4
    assert "Makefile" in changed


# ---------- IndexSyncManager 集成 ----------


def test_sync_index_l1_incremental_preserves_knowledge(tmp_path: Path) -> None:
    _make_c_project(tmp_path)
    index, _ = enrich_index(tmp_path)
    from agentx.index.index import save_index

    save_index(tmp_path, index)
    old_fingerprint = index.project_fingerprint
    old_symbols = index.symbols

    # Phase 8.2：纯注释修改 → L0 fingerprint_only（VALID），认知保留
    (tmp_path / "param.c").write_text("// 内部修改\n", encoding="utf-8")
    diff = (
        "diff --git a/param.c b/param.c\n"
        "--- a/param.c\n"
        "+++ b/param.c\n"
        "@@ -1,1 +1,1 @@\n"
        "-// TODO\n"
        "+// 内部修改\n"
    )
    result = sync_index(tmp_path, diff=diff)
    assert result["level"] == "L0"
    assert result["action"] == "fingerprint_only"

    after = load_index(tmp_path)
    assert after is not None
    # fingerprint 跟随事实更新
    assert after.project_fingerprint != old_fingerprint
    # 认知保留
    assert after.symbols == old_symbols
    status, _ = index_status(tmp_path)
    assert status == IndexStatus.VALID


def test_sync_index_l3_adds_file_incremental(tmp_path: Path) -> None:
    _make_c_project(tmp_path)
    index, _ = enrich_index(tmp_path)
    from agentx.index.index import save_index

    save_index(tmp_path, index)

    (tmp_path / "sensor.c").write_text("int sensor_read(void) { return 0; }\n", encoding="utf-8")
    diff = (
        "diff --git a/sensor.c b/sensor.c\n"
        "new file mode 100644\n"
        "--- /dev/null\n"
        "+++ b/sensor.c\n"
        "@@ -0,0 +1,2 @@\n"
        "+int sensor_read(void) { return 0; }\n"
    )
    # Phase 8.2：少量文件新增 → 文件级增量（不 full reindex）
    result = sync_index(tmp_path, diff=diff)
    assert result["action"] == "incremental"
    after = load_index(tmp_path)
    assert after is not None
    # 新文件进入 Index（filescan 降级：符号不新增，但文件清单更新）
    assert any(f.path == "sensor.c" for f in after.files)
    status, _ = index_status(tmp_path)
    assert status == IndexStatus.VALID


def test_sync_index_no_index(tmp_path: Path) -> None:
    """Phase 7.9.2：MISSING 不再 skip——统一 bootstrap 创建 Index。"""
    (tmp_path / "main.c").write_text("int main(void) { return 0; }\n", encoding="utf-8")
    result = sync_index(tmp_path)
    assert result["action"] == "created"
    after = load_index(tmp_path)
    assert after is not None
    assert any(f.path == "main.c" for f in after.files)
    status, _ = index_status(tmp_path)
    assert status == IndexStatus.VALID
