"""Build 数据模型（Phase 7.5）。"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class KeilFile:
    """Keil 工程中的一个源文件。"""

    path: str  # 工程内相对路径（/ 分隔）
    compiled: bool  # IncludeInBuild != 0
    group: str = ""


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
