"""Build 数据模型（Phase 7.5）。"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class KeilFile:
    """Keil 工程中的一个源文件。

    path: 规范化路径——提供 project_root 时解析为工程相对路径（正斜杠）；
          否则退回 FileName（Keil 可能只给裸文件名）。
    raw: 工程文件里 FilePath/FileName 原文（诊断/回溯用）。
    file_path: FilePath 原文（有则用，无则与 raw 同）；project_root 解析前的原始相对路径。
    compiled: IncludeInBuild != 0
    group: 所属 Group 名
    """

    path: str  # 规范化路径（工程相对或裸名）
    compiled: bool  # IncludeInBuild != 0
    group: str = ""
    raw: str = ""
    file_path: str = ""


@dataclass
class KeilGroup:
    """Keil Group（工程组织单元）。"""

    name: str
    files: list[KeilFile] = field(default_factory=list)


@dataclass
class KeilTarget:
    """一个 Keil Target（如 GD32F427_Debug / GD32F427_Release / Bootloader）。"""

    name: str
    cpu: str | None = None
    device: str | None = None
    defines: list[str] = field(default_factory=list)
    groups: list[KeilGroup] = field(default_factory=list)

    @property
    def compiled_files(self) -> list[KeilFile]:
        return [f for g in self.groups for f in g.files if f.compiled]

    @property
    def excluded_files(self) -> list[KeilFile]:
        return [f for g in self.groups for f in g.files if not f.compiled]


@dataclass
class KeilProject:
    """解析后的 Keil 工程。"""

    project_file: str
    targets: list[KeilTarget] = field(default_factory=list)
    active_target: KeilTarget | None = None

    @property
    def target_name(self) -> str:
        return self.active_target.name if self.active_target else ""

    @property
    def target_cpu(self) -> str | None:
        return self.active_target.cpu if self.active_target else None

    @property
    def defines(self) -> list[str]:
        return self.active_target.defines if self.active_target else []
