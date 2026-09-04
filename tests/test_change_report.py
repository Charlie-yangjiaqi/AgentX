"""Phase 4/5 增强：External Change Detection + Change Report。"""

from __future__ import annotations

from pathlib import Path

from agentx.index.index import save_index
from agentx.index.report import load_change_report
from agentx.index.sync import ensure_synced, sync_index
from agentx.plan.service import enrich_index


def _make_c_project(tmp_path: Path) -> None:
    (tmp_path / "main.c").write_text("int main(void) { return 0; }\n", encoding="utf-8")
    (tmp_path / "param.c").write_text("// TODO\n", encoding="utf-8")
    (tmp_path / "param.h").write_text("#ifndef P\n#define P\n#endif\n", encoding="utf-8")


def test_sync_external_origin_writes_report(tmp_path: Path) -> None:
    _make_c_project(tmp_path)
    index, _ = enrich_index(tmp_path)
    save_index(tmp_path, index)

    # 外部变化：用户手改 param.c
    (tmp_path / "param.c").write_text("int user_changed;\n", encoding="utf-8")
    result = sync_index(tmp_path, origin="external")
    assert result["origin"] == "external"
    assert result["changed_files"] == ["param.c"]
    assert result["report_dir"] is not None

    report = load_change_report(tmp_path)
    assert report is not None
    assert report["source"] == "external"
    assert report["modified"] == ["param.c"]
    assert report["fingerprint_before"] != report["fingerprint_after"]
    assert (tmp_path / ".agentx" / "change_report.md").exists()


def test_sync_agentx_execution_silent(tmp_path: Path) -> None:
    _make_c_project(tmp_path)
    index, _ = enrich_index(tmp_path)
    save_index(tmp_path, index)

    (tmp_path / "param.c").write_text("int agentx_changed;\n", encoding="utf-8")
    result = sync_index(tmp_path, origin="agentx_execution")
    assert result["origin"] == "agentx_execution"
    assert result["changed_files"] == ["param.c"]
    # agentx_execution：不产生用户提醒（无报告）
    assert "report_dir" not in result
    assert load_change_report(tmp_path) is None


def test_sync_unknown_origin_reports(tmp_path: Path) -> None:
    _make_c_project(tmp_path)
    index, _ = enrich_index(tmp_path)
    save_index(tmp_path, index)

    (tmp_path / "param.c").write_text("int mystery;\n", encoding="utf-8")
    result = sync_index(tmp_path, origin="unknown")
    assert "report_dir" in result
    report = load_change_report(tmp_path)
    assert report is not None and report["source"] == "unknown"


def test_sync_added_and_removed_files_in_report(tmp_path: Path) -> None:
    _make_c_project(tmp_path)
    index, _ = enrich_index(tmp_path)
    save_index(tmp_path, index)

    (tmp_path / "sensor.c").write_text("int sensor_read(void) { return 0; }\n", encoding="utf-8")
    (tmp_path / "old_test.c").unlink(missing_ok=False) if (
        tmp_path / "old_test.c"
    ).exists() else None
    # 删除一个已知文件
    (tmp_path / "param.h").unlink()
    sync_index(tmp_path, origin="external")
    report = load_change_report(tmp_path)
    assert report is not None
    assert "sensor.c" in report["added"]
    assert "param.h" in report["removed"]


def test_ensure_synced_valid_noop(tmp_path: Path) -> None:
    _make_c_project(tmp_path)
    index, _ = enrich_index(tmp_path)
    save_index(tmp_path, index)
    status, reason, sync_result = ensure_synced(tmp_path, origin="external")
    assert status == "VALID"
    assert sync_result is None


def test_ensure_synced_stale_syncs(tmp_path: Path) -> None:
    _make_c_project(tmp_path)
    index, _ = enrich_index(tmp_path)
    save_index(tmp_path, index)

    (tmp_path / "param.c").write_text("int changed;\n", encoding="utf-8")
    status, reason, sync_result = ensure_synced(tmp_path, origin="external")
    # Phase 8.2：小源码变化 → 自动增量更新后 Index 回到 VALID（工程知识库自维护）
    assert status == "VALID"
    assert "已同步" in reason or "incremental" in reason or "增量" in reason
    assert sync_result is not None
    assert sync_result["report_dir"] is not None
    assert sync_result["index_freshness"]["state"] in ("AUTO_UPDATED", "VALID")
