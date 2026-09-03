"""Project Understanding Layer：CodeGraph + Build Info + File Analysis。

CodeGraph 是 AgentX 获取项目事实的结构化来源之一，但不是唯一真相。
ProjectGraph 是融合后的项目图数据，用于生成/更新 Project Index。
CodeGraph 不可用时必须降级为文件扫描，AgentX 不能失败。
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Protocol

from agentx.state.models import AgentXModel
from agentx.vendor.bootstrap import (
    BootstrapError,
    ensure_codegraph,
)
from agentx.vendor.manifest import DO_NOT_TRACK_ENV, TELEMETRY_ENV

# CodeGraph 程序位置（可被环境变量覆盖）
CODEGRAPH_BIN_ENV = "CODEGRAPH_BIN"
CODEGRAPH_NODE_ENV = "CODEGRAPH_NODE"

# 进程内 bootstrap 失败缓存：避免每次 analyze 重复下载尝试（重试走 CLI 命令）
_bootstrap_failed: str | None = None


class ProjectGraph(AgentXModel):
    """融合后的项目图数据（CodeGraph + Build + File Analysis 的归一化结果）。"""

    source: str = "codegraph"  # codegraph | filescan
    files: list[dict[str, Any]] = []
    symbols: list[dict[str, Any]] = []
    dependencies: list[dict[str, Any]] = []
    call_graph: list[dict[str, Any]] = []
    build_info: dict[str, Any] = {}
    include_map: dict[str, list[str]] = {}
    errors: list[str] = []


class CodeGraphProvider(Protocol):
    """CodeGraph 抽象：未来可接内置 / MCP / LSP / Tree-sitter。"""

    def analyze_project(self, path: Path) -> ProjectGraph: ...


def codegraph_env() -> tuple[Path, Path]:
    """CodeGraph 程序解析（A' 优先级）：

    显式 env CODEGRAPH_BIN/CODEGRAPH_NODE → vendored → bootstrap 自动安装。
    不可用时抛 RuntimeError（由 analyze_project 降级 filescan）。
    """
    global _bootstrap_failed
    if _bootstrap_failed is not None:
        raise RuntimeError(f"CodeGraph 不可用: {_bootstrap_failed}")
    node, bin_path, reason = ensure_codegraph()
    if node is None or bin_path is None:
        _bootstrap_failed = reason or "未知错误"
        raise RuntimeError(f"CodeGraph 不可用: {_bootstrap_failed}")
    return node, bin_path


def codegraph_available() -> bool:
    try:
        codegraph_env()
        return True
    except RuntimeError:
        return False


def reset_codegraph_cache() -> None:
    """清除进程内 bootstrap 失败缓存（agentx codegraph install 成功后调用）。"""
    global _bootstrap_failed
    _bootstrap_failed = None


# ---------- CodeGraph 知识库（.codegraph/codegraph.db，只读） ----------

_SYMBOL_KINDS = {"file", "import"}
_CALL_EDGE_KINDS = {"calls"}
_IMPORT_EDGE_KINDS = {"imports"}


def _confidence_level(score: float | None) -> str:
    """把数字置信度映射为等级：>=0.8 high，>=0.5 medium，否则 low。"""
    if score is None:
        return "medium"
    if score >= 0.8:
        return "high"
    if score >= 0.5:
        return "medium"
    return "low"


def _read_codegraph_db(project_root: Path) -> dict[str, Any]:
    """只读 .codegraph/codegraph.db，提取 symbols / call_graph。

    返回 {"symbols": [...], "call_graph": [...], "include_map": {...}}。
    CodeGraph 静态分析对宏/函数指针/回调不可靠，调用边带 confidence
    等级，避免 Planner 把推测当事实。db 缺失或损坏时抛异常（由调用方降级）。

    Scope（Phase 7.8）：
    - ignored 文件完全过滤
    - 调用边：任一端 third_party → external=true（业务↔库的边界保留）；
      两端都 third_party（库内部实现）→ 删除，不淹没业务影响分析
    """
    import sqlite3

    from agentx.scope.config import scope_of_file
    from agentx.scope.resolver import ScopeResolver

    root = project_root.resolve()
    resolver = ScopeResolver(root)
    db_path = root / ".codegraph" / "codegraph.db"
    if not db_path.exists():
        raise RuntimeError(f"CodeGraph 知识库不存在: {db_path}")

    def _scope(p: str) -> tuple[str, str | None] | None:
        """返回 (scope_type, scope_name)；ignored 返回 None。"""
        if resolver.is_ignored(str(p)):
            return None
        return scope_of_file(str(p), resolver.config)

    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        nodes: dict[str, sqlite3.Row] = {}
        try:
            for r in con.execute(
                "SELECT id, kind, name, qualified_name, file_path, start_line, end_line, "
                "signature FROM nodes"
            ):
                nodes[r["id"]] = r
        except sqlite3.OperationalError as e:
            raise RuntimeError("CodeGraph 知识库 schema 不兼容（nodes 表不可用）") from e

        # 符号：代码实体（排除 file/import 节点 + scope 外文件）
        symbols: list[dict[str, Any]] = []
        for r in nodes.values():
            if r["kind"] in _SYMBOL_KINDS:
                continue
            if _scope(r["file_path"]) is None:
                continue
            symbols.append(
                {
                    "name": r["name"],
                    "qualified_name": r["qualified_name"],
                    "type": r["kind"],
                    "file": r["file_path"],
                    "start_line": r["start_line"],
                    "end_line": r["end_line"],
                    "signature": r["signature"],
                }
            )
        symbols.sort(key=lambda s: (s["file"], s.get("start_line") or 0))

        # 调用关系：calls 边 + 置信度 + Scope 边界处理
        call_graph: list[dict[str, Any]] = []
        include_map: dict[str, list[str]] = {}
        try:
            for r in con.execute("SELECT source, target, kind, metadata, line, col FROM edges"):
                kind = r["kind"]
                if kind in _CALL_EDGE_KINDS:
                    src, dst = nodes.get(r["source"]), nodes.get(r["target"])
                    if src is None or dst is None:
                        continue
                    src_scope = _scope(src["file_path"])
                    dst_scope = _scope(dst["file_path"])
                    if src_scope is None or dst_scope is None:
                        continue
                    if src_scope[0] == "third_party" and dst_scope[0] == "third_party":
                        continue  # 库内部实现：不进业务影响分析
                    meta: dict[str, Any] = {}
                    if r["metadata"]:
                        try:
                            meta = json.loads(r["metadata"])
                        except json.JSONDecodeError:
                            meta = {}
                    score = meta.get("confidence")
                    edge: dict[str, Any] = {
                        "caller": src["name"],
                        "callee": dst["name"],
                        "confidence": _confidence_level(score),
                        "confidence_score": score,
                        "file": src["file_path"],
                        "line": r["line"],
                    }
                    if "third_party" in (src_scope[0], dst_scope[0]):
                        edge["external"] = True  # 业务 ↔ 库边界
                    call_graph.append(edge)
                elif kind in _IMPORT_EDGE_KINDS:
                    src, dst = nodes.get(r["source"]), nodes.get(r["target"])
                    if src is None or dst is None:
                        continue
                    if src["kind"] != "file" or dst["kind"] != "file":
                        continue
                    if _scope(src["file_path"]) is None or _scope(dst["file_path"]) is None:
                        continue
                    include_map.setdefault(src["file_path"], [])
                    if dst["file_path"] not in include_map[src["file_path"]]:
                        include_map[src["file_path"]].append(dst["file_path"])
        except sqlite3.OperationalError as e:
            raise RuntimeError("CodeGraph 知识库 schema 不兼容（edges 表不可用）") from e

        call_graph.sort(key=lambda e: (e.get("file", ""), e.get("line") or 0))
        return {"symbols": symbols, "call_graph": call_graph, "include_map": include_map}
    finally:
        con.close()


class CliCodeGraphProvider:
    """内置 CodeGraph CLI 实现：包装 node.exe + codegraph.js。

    符号/调用/包含关系来自 .codegraph/codegraph.db（只读 sqlite）。
    数据库损坏/缺失 → 明确降级 filescan（source=filescan + errors 记录原因），
    不返回“codegraph 但空知识”的误导结果（Phase 7.9）。
    """

    name = "codegraph-cli"

    def analyze_project(self, path: Path) -> ProjectGraph:
        node, bin_path = codegraph_env()

        root = path.resolve()
        # 初始化/增量同步：status 显示无待处理变更时跳过（省 node 启动+扫描）
        status = self._run_json(node, bin_path, ["status", str(root), "-j"]) or {}
        pending = status.get("pendingChanges") or {}
        needs_sync = any(pending.values())
        # 旧版（如 0.9.9）建的库：CodeGraph 官方标记 reindexRecommended=true，
        # 不得静默复用旧库 —— 明确触发全量重建，避免错误 Index。
        index_status = status.get("index") or {}
        reindex_needed = bool(index_status.get("reindexRecommended"))
        if reindex_needed:
            self._run(node, bin_path, ["init", str(root)])
            status = self._run_json(node, bin_path, ["status", str(root), "-j"]) or {}
            pending = status.get("pendingChanges") or {}
            needs_sync = any(pending.values())
        if status.get("initialized") and not needs_sync:
            pass  # 索引已是最新，直接使用
        elif (root / ".codegraph").exists():
            self._run(node, bin_path, ["sync", str(root)])
        else:
            self._run(node, bin_path, ["init", str(root)])

        files = self._run_json(node, bin_path, ["files", "-p", str(root), "-j"])
        status = self._run_json(node, bin_path, ["status", str(root), "-j"]) or {}
        raw_files: list[dict[str, Any]] = (
            [f for f in files if isinstance(f, dict)] if isinstance(files, list) else []
        )

        # 知识库读取失败（schema 不兼容/损坏/lock）→ 明确降级：返回 filescan 图并
        # 记录原因。绝不能吞掉异常后仍返回 source=codegraph 的空知识——那会让 enrich
        # 以为 CodeGraph 成功，产出“codegraph 但符号全空”的误导性 Index
        # （Phase 7.9 修复：CodeGraph 不可用 → 明确 degraded，不静默降级）。
        try:
            knowledge = _read_codegraph_db(root)
        except Exception as e:
            graph = FileScanProvider().analyze_project(root)
            graph.errors.append(
                f"CodeGraph 知识库读取失败({type(e).__name__})，已降级为文件扫描: {e}"
            )
            return graph

        include_map = _scan_includes(root)
        errors: list[str] = []
        if status.get("error"):
            errors.append(str(status["error"]))
        # Scope（Phase 7.8）：ignored 过滤 + scope_type/scope_name 标注
        files_out = _annotate_scope(root, raw_files)
        return ProjectGraph(
            source="codegraph",
            files=files_out,
            symbols=knowledge.get("symbols", []),
            call_graph=knowledge.get("call_graph", []),
            build_info=_detect_build_info(root),
            include_map=include_map,
            errors=errors,
        )

    def _run(self, node: Path, bin_path: Path, args: list[str]) -> str:
        import os
        import subprocess

        # 内置 CodeGraph 默认关闭遥测（用户显式设置时尊重用户）
        env = dict(os.environ)
        if TELEMETRY_ENV not in env and DO_NOT_TRACK_ENV not in env:
            env[TELEMETRY_ENV] = "0"

        proc = subprocess.run(
            [str(node), str(bin_path), *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=300,
            env=env,
        )
        if proc.returncode != 0:
            raise RuntimeError(
                f"CodeGraph 失败 ({' '.join(args[:2])}): {(proc.stderr or '')[:300]}"
            )
        return proc.stdout

    def _run_json(self, node: Path, bin_path: Path, args: list[str]) -> Any:
        out = self._run(node, bin_path, args)
        try:
            return json.loads(out)
        except json.JSONDecodeError:
            return None


class FileScanProvider:
    """降级实现：无 CodeGraph 时的基础文件扫描（不失败）。"""

    name = "filescan"

    def analyze_project(self, path: Path) -> ProjectGraph:
        root = path.resolve()
        files: list[dict[str, Any]] = []
        for rel, size in _source_files(root):
            files.append({"path": rel, "language": _language_of(rel), "nodeCount": 0, "size": size})
        include_map = _scan_includes(root)
        return ProjectGraph(
            source="filescan",
            files=_annotate_scope(root, files),
            build_info=_detect_build_info(root),
            include_map=include_map,
            errors=["CodeGraph 不可用，已降级为文件扫描"],
        )


def _annotate_scope(root: Path, files: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """文件清单 Scope 标注：ignored 过滤 + scope_type/scope_name（Phase 7.8）。"""
    from agentx.scope.config import LEGACY_IGNORE_FILENAME, SCOPE_CONFIG_FILENAME, scope_of_file
    from agentx.scope.resolver import ScopeResolver

    _config_files = {SCOPE_CONFIG_FILENAME, LEGACY_IGNORE_FILENAME}
    resolver = ScopeResolver(root)
    out: list[dict[str, Any]] = []
    for f in files:
        path = str(f.get("path", ""))
        if Path(path).name in _config_files:
            continue  # Scope 配置文件不进 Index files
        if resolver.is_ignored(path):
            continue
        scope_type, scope_name = scope_of_file(path, resolver.config)
        entry = dict(f)
        entry["scope_type"] = scope_type
        entry["scope_name"] = scope_name
        out.append(entry)
    return out


def analyze_project(path: Path) -> ProjectGraph:
    """统一入口：CodeGraph 可用 → 增强；不可用/失败 → 文件扫描降级（明确标记）。

    任何降级都会在 graph.source=filescan + graph.errors 中留下可回溯原因；
    不会静默返回“看起来完整”的空结果（Phase 7.9）。
    """
    path = Path(path) if not isinstance(path, Path) else path  # 容忍 str 调用
    try:
        if codegraph_available():
            return CliCodeGraphProvider().analyze_project(path)
    except BootstrapError:
        raise  # AGENTX_CODEGRAPH_REQUIRED=1：fail-fast，不降级
    except Exception as e:
        graph = FileScanProvider().analyze_project(path)
        graph.errors.append(f"CodeGraph 失败({type(e).__name__})，已降级为文件扫描: {e}")
        return graph
    return FileScanProvider().analyze_project(path)


# ---------- File Analysis（include / 源文件清单） ----------

_INCLUDE_RE = re.compile(r'^\s*#\s*include\s*[<"]([^>"]+)[>"]')


def _scan_includes(root: Path) -> dict[str, list[str]]:
    """扫描 #include 关系（轻量，不依赖外部工具）；src/target 均按 Scope 过滤。"""
    from agentx.scope.resolver import ScopeResolver

    resolver = ScopeResolver(root)
    result: dict[str, list[str]] = {}
    for p in root.rglob("*"):
        if not p.is_file() or p.suffix not in {".c", ".h", ".cpp", ".hpp", ".cc"}:
            continue
        rel = str(p.relative_to(root)).replace("\\", "/")
        if resolver.is_ignored(rel):
            continue
        includes: list[str] = []
        try:
            with p.open("r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    m = _INCLUDE_RE.match(line)
                    if m:
                        includes.append(m.group(1))
        except OSError:
            pass
        if includes:
            result[rel] = includes
    return result


def _source_files(root: Path) -> list[tuple[str, int]]:
    from agentx.index.fingerprint import EXCLUDE_DIRS, SOURCE_EXTS
    from agentx.scope.config import LEGACY_IGNORE_FILENAME, SCOPE_CONFIG_FILENAME
    from agentx.scope.resolver import ScopeResolver

    _config_files = {SCOPE_CONFIG_FILENAME, LEGACY_IGNORE_FILENAME}
    resolver = ScopeResolver(root)
    out: list[tuple[str, int]] = []
    for p in root.rglob("*"):
        if not p.is_file():
            continue
        if any(part in EXCLUDE_DIRS for part in p.parts):
            continue
        if p.name in _config_files:
            continue  # Scope 配置文件不进 Index files（指纹仍参与）
        if p.suffix in SOURCE_EXTS:
            rel = str(p.relative_to(root)).replace("\\", "/")
            if resolver.is_ignored(rel):
                continue
            try:
                size = p.stat().st_size
            except OSError:
                size = 0
            out.append((rel, size))
    out.sort()
    return out


def _language_of(path: str) -> str:
    ext = Path(path).suffix.lower()
    mapping = {
        ".c": "c",
        ".h": "c",
        ".cpp": "cpp",
        ".hpp": "cpp",
        ".cc": "cpp",
        ".py": "python",
        ".ts": "typescript",
        ".js": "javascript",
        ".rs": "rust",
        ".go": "go",
        ".java": "java",
        ".toml": "toml",
        ".yaml": "yaml",
        ".yml": "yaml",
    }
    return mapping.get(ext, ext.lstrip(".") or "unknown")


# ---------- Build Reality ----------

_KEIL_EXTS = {".uvprojx", ".uvproj"}
_IAR_EXTS = {".ewp"}


def _detect_build_info(root: Path) -> dict[str, Any]:
    """探测真实构建信息（Build Reality）。

    优先级：compile_commands > CMake/Ninja > Makefile > Keil > IAR > STM32 .ioc。
    真实工程配置优先于文件扫描。返回：
    {system, build_source, compiled_files, excluded_files, has_build_config}
    - compiled_files：确认参与构建的源文件
    - excluded_files：工程配置明确排除的文件（Keil IncludeInBuild=0 / IAR excluded=yes）
    - has_build_config：是否存在构建配置（决定 not_compiled vs unknown）
    """
    info: dict[str, Any] = {
        "system": "unknown",
        "build_source": None,
        "compiled_files": [],
        "excluded_files": [],
        "has_build_config": False,
        "target": None,
        "cpu": None,
        "defines": [],
        "project_file": None,
        "groups": [],
    }

    # 1. compile_commands.json（最真实）
    compile_db = root / "compile_commands.json"
    if compile_db.exists():
        try:
            entries = json.loads(compile_db.read_text(encoding="utf-8"))
            files = []
            for e in entries:
                f = str(e.get("file", "")).replace("\\", "/")
                if f:
                    files.append({"file": f, "compiled": True})
            if files:
                info.update(
                    system="compile_commands.json",
                    build_source="compile_commands",
                    compiled_files=files,
                    has_build_config=True,
                )
                return info
        except Exception:
            pass

    # 2. CMake / Ninja
    cmake = root / "CMakeLists.txt"
    if cmake.exists():
        info["system"] = "cmake"
        info["build_source"] = "cmake"
        info["has_build_config"] = True
        try:
            text = cmake.read_text(encoding="utf-8", errors="replace")
            for m in re.finditer(r"([\w./-]+\.(?:c|cpp|cc))", text):
                info["compiled_files"].append({"file": m.group(1), "compiled": True})
        except OSError:
            pass
        return info
    ninja = root / "build.ninja"
    if ninja.exists():
        info["system"] = "ninja"
        info["build_source"] = "ninja"
        info["has_build_config"] = True
        try:
            text = ninja.read_text(encoding="utf-8", errors="replace")
            for m in re.finditer(r"([\w./-]+\.(?:c|cpp|cc))", text):
                info["compiled_files"].append({"file": m.group(1), "compiled": True})
        except OSError:
            pass
        return info

    # 3. Makefile
    makefile = root / "Makefile"
    if makefile.exists():
        info["system"] = "makefile"
        info["build_source"] = "make"
        info["has_build_config"] = True
        try:
            text = makefile.read_text(encoding="utf-8", errors="replace")
            for m in re.finditer(r"([\w./-]+\.(?:c|cpp|cc))", text):
                info["compiled_files"].append({"file": m.group(1), "compiled": True})
        except OSError:
            pass
        return info

    # 4. Keil（.uvprojx / .uvproj，XML）——解析唯一真相源：build/keil_parser
    from agentx.scope.build_scope import find_keil_project

    keil = find_keil_project(root)
    if keil is not None:
        from agentx.build import parse_keil_project

        # Phase 7.10：传 project_root，FilePath 归一化为工程相对路径（Build Scope 依据）
        project = parse_keil_project(keil, project_root=root)
        info["system"] = "keil"
        info["build_source"] = "keil"
        info["has_build_config"] = True
        info["project_file"] = str(keil)
        info["targets"] = [t.name for t in project.targets]
        if project.active_target is not None:
            info["target"] = project.target_name or None
            info["cpu"] = project.target_cpu
            info["defines"] = project.defines
            info["compiled_files"] = [
                {"file": f.path, "compiled": True} for f in project.active_target.compiled_files
            ]
            info["excluded_files"] = [
                {"file": f.path, "compiled": False} for f in project.active_target.excluded_files
            ]
            info["groups"] = [
                {"name": g.name, "files": [f.path for f in g.files]}
                for g in (project.active_target.groups or [])
            ]
        return info

    # 5. IAR（.ewp，XML）
    iar = _first_existing(root, _IAR_EXTS)
    if iar is not None:
        info["system"] = "iar"
        info["build_source"] = "iar"
        info["has_build_config"] = True
        compiled, excluded, target = _parse_iar(iar)
        info["compiled_files"] = compiled
        info["excluded_files"] = excluded
        info["target"] = target
        return info

    # 6. STM32 CubeMX（.ioc：配置标记，不含源码清单）
    if (root / "*.ioc").parent.exists() and list(root.glob("*.ioc")):
        info["system"] = "stm32_ioc"
        info["build_source"] = "stm32_ioc"
        info["has_build_config"] = True
        return info

    return info


def _first_existing(root: Path, exts: set[str]) -> Path | None:
    """查找构建配置文件：顶层优先，其次常见工程子目录，最后递归（浅优先）。"""
    for p in root.iterdir():
        if p.is_file() and p.suffix.lower() in exts:
            return p
    for sub in ("Projects", "project", "prj", "MDK-ARM", "Keil", "EWARM", "build"):
        d = root / sub
        if d.is_dir():
            for p in d.iterdir():
                if p.is_file() and p.suffix.lower() in exts:
                    return p
    # 递归兜底（如 Projects/MDK-ARM/，深度优先取浅层）
    best: Path | None = None
    best_depth = 10**9
    for p in root.rglob("*"):
        if p.is_file() and p.suffix.lower() in exts:
            depth = len(p.relative_to(root).parts)
            if depth < best_depth:
                best, best_depth = p, depth
    return best


def _parse_keil(path: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]], str | None]:
    """兼容入口：委托 build/keil_parser（Build Reality 唯一真相源）。"""
    from agentx.build import parse_keil_project

    project = parse_keil_project(path)
    if project.active_target is None:
        return [], [], None
    compiled = [{"file": f.path, "compiled": True} for f in project.active_target.compiled_files]
    excluded = [{"file": f.path, "compiled": False} for f in project.active_target.excluded_files]
    return compiled, excluded, project.target_name or None


def _parse_iar(path: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]], str | None]:
    """解析 IAR 工程文件：<file><name>x.c</name> + <excluded>yes</excluded> → excluded。"""
    import xml.etree.ElementTree as ET

    compiled: list[dict[str, Any]] = []
    excluded: list[dict[str, Any]] = []
    target: str | None = None
    try:
        tree = ET.parse(path)
    except (ET.ParseError, OSError):
        return compiled, excluded, target
    root_el = tree.getroot()
    # IAR：<Cpu><Name>（芯片型号）
    cpu = root_el.find(".//Cpu/Name")
    if cpu is not None and cpu.text and cpu.text.strip():
        target = cpu.text.strip()
    for f in root_el.iter("file"):
        name_el = f.find("name")
        if name_el is None or not name_el.text:
            continue
        fname = name_el.text.strip().replace("$PROJ_DIR$", "").replace("\\", "/")
        fname = fname.lstrip("/")
        if not fname:
            continue
        excluded_flag = False
        excl = f.find("excluded")
        if excl is not None and excl.text and excl.text.strip() == "yes":
            excluded_flag = True
        entry = {"file": fname, "compiled": not excluded_flag}
        (compiled if not excluded_flag else excluded).append(entry)
    return compiled, excluded, target
