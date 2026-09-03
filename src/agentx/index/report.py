"""ChangeReportManager：外部变化感知的可见性。

当 IndexSync 检测到非 AgentX 执行产生的项目变化时，生成用户可见
变更报告（.agentx/change_report.md + change_report.json），让用户知道
AgentX 发现了什么变化、同步了什么。

报告是用户可见日志，不进入 index.json 认知层。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

REPORT_MD = "change_report.md"
REPORT_JSON = "change_report.json"


def write_change_report(
    project_root: Path,
    *,
    origin: str,
    level: str,
    action: str,
    modified: list[str],
    added: list[str],
    removed: list[str],
    fingerprint_before: str,
    fingerprint_after: str,
    message: str,
) -> Path:
    """生成变更报告并落盘 .agentx/。返回报告目录。"""
    root = project_root.resolve()
    report_dir = root / ".agentx"
    report_dir.mkdir(parents=True, exist_ok=True)
    now = datetime.now(UTC).isoformat(timespec="seconds")

    def _impact(path: str) -> str:
        base = path.split("/")[-1]
        if base in {
            "Makefile",
            "CMakeLists.txt",
            "build.ninja",
            "meson.build",
            "compile_commands.json",
        } or base.rsplit(".", 1)[-1].lower() in {".uvprojx", ".uvproj", ".ewp", ".ioc"}:
            return "L4"
        return "L3" if path in added or path in removed else "L1"

    md_lines = [
        "# AgentX Project Change Report",
        "",
        f"Time: {now}",
        f"Source: {origin}",
        "",
        "## Modified Files",
    ]
    if modified:
        md_lines.extend(f"- {p} (Impact: {_impact(p)})" for p in sorted(modified))
    else:
        md_lines.append("- (none)")
    md_lines += ["", "## Added Files"]
    if added:
        md_lines.extend(f"- {p} (Impact: {_impact(p)})" for p in sorted(added))
    else:
        md_lines.append("- (none)")
    md_lines += ["", "## Deleted Files"]
    if removed:
        md_lines.extend(f"- {p}" for p in sorted(removed))
    else:
        md_lines.append("- (none)")
    md_lines += [
        "",
        "## Index Update",
        f"- Change Level: {level}",
        f"- Action: {action}",
        f"- Fingerprint: {fingerprint_before} -> {fingerprint_after}",
        "",
        "Status: " + message,
    ]

    data: dict[str, Any] = {
        "time": now,
        "source": origin,
        "change_level": level,
        "action": action,
        "modified": sorted(modified),
        "added": sorted(added),
        "removed": sorted(removed),
        "fingerprint_before": fingerprint_before,
        "fingerprint_after": fingerprint_after,
        "message": message,
    }

    md_path = report_dir / REPORT_MD
    json_path = report_dir / REPORT_JSON
    md_path.write_text("\n".join(md_lines) + "\n", encoding="utf-8")
    json_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return report_dir


def load_change_report(project_root: Path) -> dict[str, Any] | None:
    """读取最近一次变更报告（JSON）。"""
    p = project_root.resolve() / ".agentx" / REPORT_JSON
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    return data if isinstance(data, dict) else None
