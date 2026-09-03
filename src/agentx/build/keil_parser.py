"""Keil 工程解析（.uvprojx / .uvproj）——Build Reality 唯一真相源。

解析范围（Phase 7.5，只提供事实，不做预处理）：
- Targets：name / cpu / device / defines（宏列表，不推断代码分支）
- Groups：工程组织（group → files）
- Files：compiled（IncludeInBuild != 0）vs excluded；读 FileName + FilePath，
  project_root 提供时把 FilePath 解析为工程相对路径（Build Scope 前提，
  Phase 7.10：Keil 实际编译文件优先）
- active target 规则（锁死，不猜）：
    1. 调用方显式指定 target_name 参数（优先级最高）
    2. 工程内 SelectTargetNo（MDK 标记的当前 Target 索引）
    3. 第一个 Target
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path

from agentx.build.models import KeilFile, KeilGroup, KeilProject, KeilTarget  # noqa: F401

__all__ = ["KeilFile", "KeilGroup", "KeilProject", "KeilTarget", "parse_keil_project"]


def parse_keil_project(
    project_path: Path, target_name: str | None = None, project_root: Path | None = None
) -> KeilProject:
    """解析 uvprojx/uvproj；解析失败返回空工程（不抛异常，不编造）。

    project_root：工程根目录。提供时把每个 File 的 FilePath（相对 .uvprojx）
    归一化为工程相对路径（正斜杠），供 Build Scope 精确匹配；无法归一时
    退回 FileName 原文。缺省时 path=FileName（兼容旧调用方/旧测试）。
    """
    project = KeilProject(project_file=str(project_path))
    try:
        tree = ET.parse(project_path)
    except (ET.ParseError, OSError):
        return project
    root_el = tree.getroot()

    proj_dir = project_path.resolve().parent
    root_resolved = project_root.resolve() if project_root is not None else None

    targets: list[KeilTarget] = []
    for target_el in root_el.iter("Target"):
        name_el = target_el.find("TargetName")
        name = name_el.text.strip() if name_el is not None and name_el.text else ""
        if not name:
            # 兼容无 TargetName 的简化/旧结构：默认名，仍解析其 Groups
            name = "default"
        target = _parse_target(name, target_el, proj_dir, root_resolved)
        targets.append(target)

    project.targets = targets
    if not targets:
        return project

    # active target 规则（锁死）：显式参数 > SelectTargetNo > 第一个
    active: KeilTarget | None = None
    if target_name:
        for t in targets:
            if t.name == target_name:
                active = t
                break
    if active is None:
        select_no = _find_select_target_no(root_el)
        if select_no is not None and 0 <= select_no < len(targets):
            active = targets[select_no]
    if active is None:
        active = targets[0]
    project.active_target = active
    return project


def _resolve_path(
    fp_raw: str, fn_raw: str, proj_dir: Path, root_resolved: Path | None
) -> str:
    """File 路径归一化 → 工程相对路径（正斜杠）；失败退回可用的原文。

    优先级：FilePath（相对 .uvprojx，可含 ..）→ 相对工程根；
    无 project_root → 相对 .uvprojx 目录；绝对盘符/UNC/越界 → FileName 兜底。
    """
    fp_raw = (fp_raw or "").strip().replace("\\", "/")
    fn_raw = (fn_raw or "").strip().replace("\\", "/")
    for marker in ("$PROJ_DIR$", "$PROJ$"):
        fp_raw = fp_raw.replace(marker, ".")
    candidate = fp_raw or fn_raw
    if not candidate:
        return ""
    # 绝对路径（/ 开头或盘符/UNC）→ 无法相对化，退回 FileName
    if candidate.startswith("/") or (len(candidate) > 1 and candidate[1] == ":"):
        return fn_raw or candidate
    try:
        abs_p = (proj_dir / candidate).resolve()
    except OSError:
        return fn_raw or candidate
    if root_resolved is not None:
        try:
            return str(abs_p.relative_to(root_resolved)).replace("\\", "/")
        except ValueError:
            return fn_raw or candidate  # 越出工程根：退回 FileName
    try:
        return str(abs_p.relative_to(proj_dir)).replace("\\", "/")
    except ValueError:
        return fn_raw or candidate


def _parse_target(
    name: str,
    target_el: ET.Element,
    proj_dir: Path,
    root_resolved: Path | None,
) -> KeilTarget:
    cpu: str | None = None
    device: str | None = None
    defines: list[str] = []

    cpu_el = target_el.find(".//Cpu")
    if cpu_el is not None and cpu_el.text and cpu_el.text.strip():
        cpu = cpu_el.text.strip()
    device_el = target_el.find(".//Device")
    if device_el is not None and device_el.text and device_el.text.strip():
        device = device_el.text.strip()
    # Defines：<VariousControls><Define>A,B,C</Define></VariousControls>
    define_el = target_el.find(".//VariousControls/Define")
    if define_el is not None and define_el.text:
        defines = [
            d.strip() for d in define_el.text.split(",") if d.strip()
        ]

    groups: list[KeilGroup] = []
    for group_el in target_el.iter("Group"):
        gname_el = group_el.find("GroupName")
        gname = gname_el.text.strip() if gname_el is not None and gname_el.text else ""
        files: list[KeilFile] = []
        for file_el in group_el.iter("File"):
            fn_el = file_el.find("FileName")
            fn_raw = fn_el.text.strip() if fn_el is not None and fn_el.text else ""
            fp_el = file_el.find("FilePath")
            fp_raw = fp_el.text.strip() if fp_el is not None and fp_el.text else ""
            if not fn_raw and not fp_raw:
                continue
            include_in_build = True
            for fo in file_el.iter("IncludeInBuild"):
                if fo.text and fo.text.strip() == "0":
                    include_in_build = False
            path = _resolve_path(fp_raw, fn_raw, proj_dir, root_resolved)
            raw = fn_raw
            file_path = fp_raw or fn_raw
            files.append(
                KeilFile(
                    path=path, compiled=include_in_build, group=gname, raw=raw, file_path=file_path
                )
            )
        groups.append(KeilGroup(name=gname, files=files))

    return KeilTarget(
        name=name, cpu=cpu, device=device, defines=defines, groups=groups
    )


def _find_select_target_no(root_el: ET.Element) -> int | None:
    for el in root_el.iter("SelectTargetNo"):
        if el.text and el.text.strip().isdigit():
            return int(el.text.strip())
    return None
