"""P1 测试：Project Index 基础设施（fingerprint / 状态机）。"""

from __future__ import annotations

from pathlib import Path

from agentx.index.fingerprint import compute_fingerprint, relevant_files
from agentx.index.index import (
    IndexStatus,
    create_index,
    index_path,
    index_status,
    load_index,
    refresh_index,
    save_index,
)


def _make_c_project(tmp_path: Path) -> None:
    (tmp_path / "main.c").write_text("int main(void) { return 0; }\n", encoding="utf-8")
    (tmp_path / "param.c").write_text("int tx;\n", encoding="utf-8")
    (tmp_path / "param.h").write_text("#ifndef P\n#define P\n#endif\n", encoding="utf-8")
    (tmp_path / "Makefile").write_text("all:\n\tcc main.c\n", encoding="utf-8")
    (tmp_path / ".agentx").mkdir()
    (tmp_path / ".git").mkdir()


def test_fingerprint_stable_and_sensitive(tmp_path: Path) -> None:
    _make_c_project(tmp_path)
    fp1 = compute_fingerprint(tmp_path)
    fp2 = compute_fingerprint(tmp_path)
    assert fp1 == fp2
    assert len(fp1) == 8

    # 内容变化 → 指纹变化
    (tmp_path / "param.c").write_text("int tx = 1;\n", encoding="utf-8")
    fp3 = compute_fingerprint(tmp_path)
    assert fp3 != fp1

    # 新文件 → 指纹变化
    (tmp_path / "extra.c").write_text("int e;\n", encoding="utf-8")
    assert compute_fingerprint(tmp_path) != fp3


def test_fingerprint_ignores_agentx_and_git(tmp_path: Path) -> None:
    _make_c_project(tmp_path)
    fp1 = compute_fingerprint(tmp_path)
    # .agentx 和 .git 内部变化不影响指纹
    (tmp_path / ".agentx" / "index.json").write_text("{}", encoding="utf-8")
    (tmp_path / ".git" / "HEAD").write_text("ref\n", encoding="utf-8")
    assert compute_fingerprint(tmp_path) == fp1


def test_index_status_missing(tmp_path: Path) -> None:
    status, reason = index_status(tmp_path)
    assert status == IndexStatus.MISSING
    assert "没有 Index" in reason


def test_index_valid_after_create(tmp_path: Path) -> None:
    _make_c_project(tmp_path)
    index = create_index(tmp_path)
    save_index(tmp_path, index)

    status, reason = index_status(tmp_path)
    assert status == IndexStatus.VALID
    assert index.file_count == 4  # main.c param.c param.h Makefile


def test_index_stale_after_project_change(tmp_path: Path) -> None:
    _make_c_project(tmp_path)
    index = create_index(tmp_path)
    save_index(tmp_path, index)

    (tmp_path / "param.c").write_text("int tx = 99;\n", encoding="utf-8")
    status, reason = index_status(tmp_path)
    assert status == IndexStatus.STALE
    assert "不一致" in reason


def test_index_corrupted(tmp_path: Path) -> None:
    _make_c_project(tmp_path)
    p = index_path(tmp_path)
    p.parent.mkdir(exist_ok=True)
    p.write_text("{not valid json", encoding="utf-8")
    status, _ = index_status(tmp_path)
    assert status == IndexStatus.CORRUPTED


def test_refresh_index_keeps_cognition(tmp_path: Path) -> None:
    _make_c_project(tmp_path)
    index = create_index(tmp_path)
    index.symbols = [{"name": "tx", "file": "param.c", "kind": "variable"}]
    save_index(tmp_path, index)

    (tmp_path / "param.c").write_text("int tx = 2;\n", encoding="utf-8")
    refreshed = refresh_index(tmp_path, load_index(tmp_path))
    assert refreshed.symbols == index.symbols  # 认知保留
    status, _ = index_status(tmp_path)
    assert status == IndexStatus.STALE  # 刷新后未保存仍是旧文件
    save_index(tmp_path, refreshed)
    status, _ = index_status(tmp_path)
    assert status == IndexStatus.VALID


def test_relevant_files_excludes_generated(tmp_path: Path) -> None:
    _make_c_project(tmp_path)
    (tmp_path / "main.exe").write_bytes(b"MZ")
    (tmp_path / "out.txt").write_text("x", encoding="utf-8")
    files = relevant_files(tmp_path)
    assert "main.c" in files
    assert "main.exe" not in files
    assert "out.txt" not in files
