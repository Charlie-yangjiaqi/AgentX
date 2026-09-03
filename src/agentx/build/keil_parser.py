"""Keil 工程解析（.uvprojx / .uvproj）——Build Reality 唯一真相源。

解析范围（Phase 7.5，只提供事实，不做预处理）：
- Targets：name / cpu / device / defines（宏列表，不推断代码分支）
- Groups：工程组织（group → files）
- Files：compiled（IncludeInBuild != 0）vs excluded
- active target 规则（锁死，不猜）：
    1. 工程内 SelectTargetNo（MDK 标记的当前 Target 索引）
    2. 第一个 Target
    3. 调用方显式指定 target_name 参数（优先级最高）
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path

from agentx.build.models import KeilFile, KeilGroup, KeilProject, KeilTarget  # noqa: F401

__all__ = ["KeilFile", "KeilGroup", "KeilProject", "KeilTarget", "parse_keil_project"]


def parse_keil_project(
    project_path: Path, target_name: str | None = None
) -> KeilProject:
    """解析 uvprojx/uvproj；解析失败返回空工程（不抛异常，不编造）。"""
    project = KeilProject(project_file=str(project_path))
    try:
        tree = ET.parse(project_path)
    except (ET.ParseError, OSError):
        return project
    root_el = tree.getroot()

    targets: list[KeilTarget] = []
    for target_el in root_el.iter("Target"):
        name_el = target_el.find("TargetName")
        name = name_el.text.strip() if name_el is not None and name_el.text else ""
        if not name:
            # 兼容无 TargetName 的简化/旧结构：默认名，仍解析其 Groups
            name = "default"
        target = _parse_target(name, target_el)
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


def _parse_target(name: str, target_el: ET.Element) -> KeilTarget:
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
            fname_el = file_el.find("FileName")
            if fname_el is None or not fname_el.text:
                continue
            fname = fname_el.text.strip().replace("\\", "/")
            if not fname:
                continue
            include_in_build = True
            for fo in file_el.iter("IncludeInBuild"):
                if fo.text and fo.text.strip() == "0":
                    include_in_build = False
            files.append(KeilFile(path=fname, compiled=include_in_build, group=gname))
        groups.append(KeilGroup(name=gname, files=files))

    return KeilTarget(
        name=name, cpu=cpu, device=device, defines=defines, groups=groups
    )


def _find_select_target_no(root_el: ET.Element) -> int | None:
    for el in root_el.iter("SelectTargetNo"):
        if el.text and el.text.strip().isdigit():
            return int(el.text.strip())
    return None
