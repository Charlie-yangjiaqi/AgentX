"""Module Discovery：确定性模块发现（零 LLM、零新解析器，Phase 7.7）。

把 Index 的 files/symbols/call_graph/include_map/build_info 聚合成模块：
- 文件归属证据优先级：Keil/IAR Groups（人工真值）> 符号前缀族 > 叶子目录 > 文件名短名
- 第三方库识别并冻结（不参与跨目录合并）
- 跨目录合并：符号共享前缀 + 存在互引（保守，可回溯）

输出 modules 列表（name/type/files/symbols/third_party/confidence/evidence.basis），
dependencies/consumers/entry_points/build_status 由 infer.infer_module_relations 补充。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from agentx.index.index import ProjectIndex

# 泛目录名：太通用，模块名优先符号前缀族（App/ui_*.c → UI）
GENERIC_DIRS = {
    "app",
    "user",
    "src",
    "application",
    "core",
    "common",
    "hal",
    "bsp",
    "drivers",
    "driver",
    "middleware",
    "lib",
    "library",
    "utilities",
    "utils",
    "main",
}

# 第三方库目录特征（路径段匹配）
THIRD_PARTY_DIRS = {
    "middlewares",
    "thirdparty",
    "third_party",
    "cmsis",
    "stm32cube_fw",
    "freertos",
    "rtos",
    "fatfs",
    "lvgl",
    "lwip",
    "usb",
    "components",
    "external",
    "lib",
    "library",
    "libraries",
    "segger",
    "touchgfx",
    "emwin",
    "cubemx",
    "rtthread",
    "rt-thread",
}

# 第三方库符号前缀特征（模块名级匹配）
THIRD_PARTY_PREFIXES = (
    "lv_",
    "lvgl",
    "ff_",
    "cmsis_",
    "segger_",
    "usbd_",
    "usbh_",
    "tusb_",
    "os_",
    "stm32f",
    "stm32h",
    "stm32g",
    "stm32l",
    "tflite_",
    "nrf_",
)


def _leaf_dir(path: str) -> str | None:
    parts = path.replace("\\", "/").split("/")
    return parts[-2] if len(parts) > 1 else None


def _stem(path: str) -> str:
    return Path(path).stem


def _group_file_match(gf: str, file_paths: list[str]) -> str | None:
    """Keil group 文件条目 → Index 文件路径（全等 > 相对结尾 > 唯一 basename）。"""
    gf = gf.replace("\\", "/").lstrip("./")
    if gf in file_paths:
        return gf
    cands = [p for p in file_paths if p.endswith("/" + gf)]
    if len(cands) == 1:
        return cands[0]
    base = gf.rsplit("/", 1)[-1]
    same_base = [p for p in file_paths if p.rsplit("/", 1)[-1] == base]
    if len(same_base) == 1:
        return same_base[0]
    return None


def _valid_module_token(tok: str) -> bool:
    """模块名 token 合法性：字母数字下划线、非数字开头、长度 ≥2。

    拒绝函数指针类型（(*TASKFUNCTION）、typedef 字符串、含 ( * : 空格 等
    非法字符的随机 token——这类 token 不得成为模块名（污染防护）。
    """
    if len(tok) < 2 or tok[0].isdigit():
        return False
    return all(ch.isalnum() or ch == "_" for ch in tok)


def _symbol_prefixes(symbols: list[Any]) -> dict[str, int]:
    """符号首 token 统计：key_init/key_scan → {"key": 2}。接受 dict 或 name 字符串。

    非法 token（函数指针类型/typedef 字符串等）不计入（污染防护）。
    """
    counts: dict[str, int] = {}
    for s in symbols:
        name = s.get("name", "") if isinstance(s, dict) else str(s)
        tok = str(name).split("_", 1)[0].casefold()
        if _valid_module_token(tok):
            counts[tok] = counts.get(tok, 0) + 1
    return counts


def _strong_prefix(symbols: list[Any]) -> str | None:
    """有效前缀族（模块级）：≥2 符号共享首 token 且占比 ≥50%。"""
    counts = _symbol_prefixes(symbols)
    if not counts:
        return None
    total = len(symbols)
    for tok, n in sorted(counts.items(), key=lambda kv: -kv[1]):
        if n >= 2 and n / total >= 0.5:
            return tok
    return None


def _file_prefix(symbols: list[Any]) -> str | None:
    """文件级前缀：符号多数首 token（单符号也有效，≥50%）。
    ui_shelf.c → ui（符号 ui_shelf_refresh）；main.c → main。"""
    counts = _symbol_prefixes(symbols)
    if not counts:
        return None
    total = len(symbols)
    for tok, n in sorted(counts.items(), key=lambda kv: -kv[1]):
        if n / total >= 0.5:
            return tok
    return None


def _is_third_party(name: str, first_file: str) -> bool:
    n = name.casefold()
    if n.startswith(THIRD_PARTY_PREFIXES):
        return True
    parts = first_file.replace("\\", "/").split("/")
    return any(p.casefold() in THIRD_PARTY_DIRS for p in parts[:-1])


def _merge_unique(a: list[str], b: list[str]) -> list[str]:
    out = list(a)
    for x in b:
        if x not in out:
            out.append(x)
    return out


def _linked(
    index: ProjectIndex,
    key_a: str,
    mod_a: dict[str, Any],
    key_b: str,
    mod_b: dict[str, Any],
    file_to_key: dict[str, str],
    sym_to_key: dict[str, str],
) -> bool:
    """两模块存在互引（跨模块 include 或调用边）→ 可合并。"""
    for src, targets in index.include_map.items():
        m1 = file_to_key.get(src)
        if m1 not in (key_a, key_b):
            continue
        for t in targets:
            m2 = file_to_key.get(t)
            if m2 is not None and {m1, m2} == {key_a, key_b}:
                return True
    for e in index.call_graph:
        m1 = sym_to_key.get(str(e.get("caller", "")))
        m2 = sym_to_key.get(str(e.get("callee", "")))
        if m1 is not None and m2 is not None and {m1, m2} == {key_a, key_b}:
            return True
    return False


def _frozen_files(frozen: dict[str, dict[str, Any]]) -> set[str]:
    """third_party 冻结模块的全部文件（group 归属跳过这些文件）。"""
    out: set[str] = set()
    for mod in frozen.values():
        out.update(mod["files"])
    return out


def discover_modules(index: ProjectIndex) -> list[dict[str, Any]]:
    """从 Index 的 files/symbols/build_info 发现模块（确定性、可回溯）。

    Phase 7.8：Scope 配置声明的 third_party 文件 → 冻结模块（单模块，不拆分、
    不参与 prefix 聚类/merge）；project 文件走完整 discover 流程。

    Phase 7.10：non_build（自有但不在当前 Keil Target 编译）文件不进主 project
    模块发现——按顶层目录冻结为非主模块（scope_type=non_build），避免非编译
    代码污染主 Index 的模块主链。
    """
    file_metas = index.files
    symbols = index.symbols
    build_info = index.build_info or {}
    groups = build_info.get("groups") or []

    # Step 0：third_party / non_build 冻结模块（配置+Build Scope 驱动）
    frozen: dict[str, dict[str, Any]] = {}
    frozen_byname: dict[str, dict[str, Any]] = {}
    project_metas: list[Any] = []

    def _freeze(name: str, scope_type: str) -> dict[str, Any]:
        key = f"{scope_type}:{name.casefold()}"
        mod = frozen_byname.get(key)
        if mod is None:
            mod = {
                "name": name,
                "type": "unknown",
                "files": [],
                "symbols": [],
                "entry_points": [],
                "responsibilities": "",
                "dependencies": [],
                "consumers": [],
                "build_status": "unknown",
                "scope_type": scope_type,
                "third_party": scope_type == "third_party",
                "confidence": 0.9,
                "evidence": {"basis": []},
            }
            frozen_byname[key] = mod
        return mod

    for fmeta in file_metas:
        scope_type = str(getattr(fmeta, "scope_type", "project") or "project")
        if scope_type == "third_party":
            tp_name = str(fmeta.scope_name or fmeta.path.split("/")[0])
            mod = _freeze(tp_name, "third_party")
            mod["files"].append(fmeta.path)
        elif scope_type == "non_build":
            # 非编译自有代码：不参与主模块发现；按顶层目录冻结为独立非主模块
            top = fmeta.path.replace("\\", "/").split("/", 1)[0] or fmeta.path
            mod = _freeze(f"{top} (non-build)", "non_build")
            mod["files"].append(fmeta.path)
        else:
            project_metas.append(fmeta)
    frozen = frozen_byname

    file_paths = [f.path for f in file_metas]

    # Step 1：Keil/IAR Groups 文件归属（人工真值，最高优先级，仅 project 文件）
    group_of_file: dict[str, str] = {}
    for g in groups:
        gname = str(g.get("name", "")).strip()
        if not gname:
            continue
        for gf in g.get("files") or []:
            matched = _group_file_match(str(gf), file_paths)
            if matched is not None and matched not in _frozen_files(frozen):
                group_of_file[matched] = gname

    symbols_by_file: dict[str, list[dict[str, Any]]] = {}
    for s in symbols:
        f = str(s.get("file", ""))
        if f:
            symbols_by_file.setdefault(f, []).append(s)

    # Step 2：文件 → 模块候选（group > 叶子目录 > 前缀族/stem；头文件跟随源文件）
    modules: dict[str, dict[str, Any]] = {}
    _source_suffixes = {".c", ".cpp", ".cc", ".cxx"}
    _header_suffixes = {".h", ".hpp", ".hh"}

    def _new_module(name: str, group: str | None) -> dict[str, Any]:
        return {
            "name": name,
            "type": "unknown",
            "files": [],
            "symbols": [],
            "entry_points": [],
            "responsibilities": "",
            "dependencies": [],
            "consumers": [],
            "build_status": "unknown",
            "scope_type": "project",
            "third_party": False,
            "confidence": 0.8,
            "evidence": {"basis": []},
            "_group": group,
        }

    def _follow_header(stem: str) -> str | None:
        """头文件跟随同 stem 源文件所在模块（ui_shelf.h → UI）。"""
        for mod in modules.values():
            for f in mod["files"]:
                if Path(f).stem == stem and Path(f).suffix.casefold() in _source_suffixes:
                    return str(mod["name"])
        return None

    def _leaf_or_ancestor(path: str) -> str | None:
        """叶子目录名；叶子为泛目录时沿路径向上取第一个非泛目录段
        （Middlewares/LVGL/src/lv_obj.c → LVGL；App/lv_shelf.c → App）。"""
        parts = path.replace("\\", "/").split("/")
        if len(parts) < 2:
            return None
        leaf = parts[-2]
        if leaf.casefold() not in GENERIC_DIRS:
            return leaf
        for p in reversed(parts[:-2]):
            if p.casefold() not in GENERIC_DIRS:
                return p
        return None

    # 源文件先处理，头文件后处理（可跟随）
    ordered = sorted(
        project_metas,
        key=lambda f: Path(f.path).suffix.casefold() in _header_suffixes,
    )
    for fmeta in ordered:
        path = fmeta.path
        group = group_of_file.get(path)
        name = group
        if name is None:
            suffix = Path(path).suffix.casefold()
            leaf = _leaf_or_ancestor(path)
            if leaf is not None and suffix not in _header_suffixes:
                name = leaf
            else:
                # 泛目录/根文件：符号首 token（ui_shelf.c → UI）> stem
                prefix = _file_prefix(symbols_by_file.get(path, []))
                if prefix:
                    name = prefix.upper()
                elif suffix in _header_suffixes:
                    name = _follow_header(_stem(path)) or _stem(path)
                else:
                    name = _stem(path)
        key = name.casefold()
        proj_mod = modules.get(key)
        if proj_mod is None:
            proj_mod = _new_module(name, group)
            modules[key] = proj_mod
        proj_mod["files"].append(path)

    # Step 3：符号归属（冻结模块 > 精确文件 > 唯一 basename > 按路径兜底建模块）
    frozen_files: set[str] = set()
    for mod in frozen.values():
        frozen_files.update(mod["files"])
    file_to_key: dict[str, str] = {}
    for key, mod in modules.items():
        for f in mod["files"]:
            file_to_key[f] = key
    base_to_key: dict[str, str | None] = {}
    for key, mod in modules.items():
        for f in mod["files"]:
            base = f.rsplit("/", 1)[-1]
            if base not in base_to_key:
                base_to_key[base] = key
            elif base_to_key[base] != key:
                base_to_key[base] = None  # basename 冲突：不兜底
    for s in symbols:
        f = str(s.get("file", ""))
        name = str(s.get("name", ""))
        if f in frozen_files:
            for mod in frozen.values():
                if f in mod["files"]:
                    if name and name not in mod["symbols"]:
                        mod["symbols"].append(name)
                    break
            continue
        fk = file_to_key.get(f)
        if fk is None:
            fk = base_to_key.get(f.rsplit("/", 1)[-1])
        if fk is None and f:
            leaf = _leaf_dir(f)
            fk = (leaf or _stem(f)).casefold()
            if fk not in modules:
                modules[fk] = _new_module(leaf or _stem(f), None)
                file_to_key[f] = fk
        if fk and name and name not in modules[fk]["symbols"]:
            modules[fk]["symbols"].append(name)

    # Step 4：第三方识别（冻结：不参与重命名/合并）
    for mod in modules.values():
        if mod["files"] and _is_third_party(mod["name"], mod["files"][0]):
            mod["third_party"] = True

    # Step 5：符号前缀族重命名（App/ui_*.c → UI）+ 跨目录合并（保守）
    # 人工分组（_group）与第三方冻结：不重命名、不合并
    sym_to_key: dict[str, str] = {}
    for key, mod in modules.items():
        for s in mod["symbols"]:
            sym_to_key[s] = key
    renamed: dict[str, dict[str, Any]] = {}
    for key, mod in modules.items():
        if mod.get("_group"):
            renamed[key] = mod
            continue
        prefix = _strong_prefix(mod["symbols"]) if not mod["third_party"] else None
        new_key = prefix or key
        if new_key == key:
            renamed[key] = mod
            continue
        if new_key in renamed:
            target = renamed[new_key]
            if (
                not target["third_party"]
                and not target.get("_group")
                and _linked(index, key, mod, new_key, target, file_to_key, sym_to_key)
            ):
                target["files"] = _merge_unique(target["files"], mod["files"])
                target["symbols"] = _merge_unique(target["symbols"], mod["symbols"])
                target["confidence"] = max(target["confidence"], mod["confidence"])
                target["evidence"]["basis"].append(f"prefix_merge:{prefix}")
            else:
                renamed[key] = mod
        else:
            assert prefix is not None
            mod["name"] = prefix.upper()
            mod["evidence"]["basis"].append(f"prefix:{prefix}")
            renamed[new_key] = mod
    modules = renamed

    # Step 6：符号级再归属（泛目录模块的符号 → 首 token 同名的具体模块）
    # 如 ui_input.c 的 ui_shelf_input → UI 模块（文件一并移入）
    name_to_file = {str(s.get("name", "")): str(s.get("file", "")) for s in symbols}
    for mod in list(modules.values()):
        if mod.get("_group") or mod.get("third_party"):
            continue
        for s in list(mod["symbols"]):
            tok = s.split("_", 1)[0].casefold()
            tgt = modules.get(tok)
            if tgt is None or tgt is mod or tgt.get("_group") or tgt.get("third_party"):
                continue
            mod["symbols"].remove(s)
            tgt["symbols"] = _merge_unique(tgt["symbols"], [s])
            if not any(b.startswith("symbol:") for b in tgt["evidence"]["basis"]):
                tgt["evidence"]["basis"].append(f"symbol:{tok}")
            fpath = name_to_file.get(s)
            if fpath:
                if fpath not in tgt["files"]:
                    tgt["files"].append(fpath)
                if fpath in mod["files"]:
                    mod["files"].remove(fpath)
    # 清理空模块（文件与符号都被移走）
    modules = {k: m for k, m in modules.items() if m["files"] or m["symbols"]}

    # Step 6.5：合并 third_party 冻结模块（Phase 7.8，不参与任何聚类/合并）
    for mod in frozen.values():
        modules[mod["name"].casefold()] = mod

    # Step 7：类型 / 置信度 / 证据基础（可回溯），清理内部字段
    from agentx.module.classify import classify_module

    result: list[dict[str, Any]] = []
    for mod in modules.values():
        mod["type"] = classify_module(mod["name"], mod["files"])
        basis: list[str] = []
        conf = 0.8
        group = mod.pop("_group", None)
        if group:
            basis.append(f"keil_group:{group}")
            conf = 0.95
        elif mod.get("scope_type") == "third_party":
            basis.append(f"scope_config:{mod['name']}")
            conf = 0.9
        elif mod.get("scope_type") == "non_build":
            basis.append("build_scope:non_build")
            conf = 0.7
        elif mod["files"]:
            basis.append(f"path:{_leaf_dir(mod['files'][0]) or _stem(mod['files'][0])}")
        prefix = _strong_prefix(mod["symbols"])
        if prefix and not any(b.startswith("prefix:") for b in basis):
            basis.append(f"prefix:{prefix}")
            conf = min(0.95, conf + 0.05)
        if mod["third_party"] and not any(b.startswith("scope_config:") for b in basis):
            basis.append("third_party")
            conf = 0.9
        mod["confidence"] = round(conf, 2)
        mod["evidence"] = {"basis": basis}
        result.append(mod)
    result.sort(key=lambda m: m["name"].casefold())
    return result
