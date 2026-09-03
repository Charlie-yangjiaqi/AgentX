"""Phase 7.10 Build Scope：主 Index 以 Keil Active Target 编译文件为工程边界。

验收：
1. 单 Target：当前 Target 编译文件全部进入 project
2. 多 Target：不允许自动 union（build_target_required）
3. 非 Target 自有源码：不进入主 project（scope_type=non_build）
4. third_party：即使出现在 Target source list，仍保持 third_party
5. ignored：即使出现在 Target source list，仍保持 ignored
6. CodeGraph 包含额外源码：主 Index 仍按 Build Scope
7. Build 解析失败：degraded / build_scope_unknown，不伪装完整成功
8. Scope Wizard：首次初始化展示 Target/build/non_build/third_party/ignore
9. Build 边界文件 = Active Target 编译文件（含传递 include 头文件跟随）
10. enrich_index 落库 scope_type + semantic 只覆盖 project 边界
"""

from __future__ import annotations

from pathlib import Path

from agentx.scope.build_scope import (
    build_boundary_files,
    build_scope_summary,
    classify_build_scope,
    find_keil_project,
    resolve_keil_build,
)

# ---------- uvprojx 构造 ----------


def _uvprojx_xml() -> str:
    """单 Target + FilePath 真实 Keil 结构。"""
    return """<Project><Targets><Target>
<TargetName>LVGL</TargetName>
<TargetOption><TargetArmAds><Cads><VariousControls><Define>USE_LVGL</Define></VariousControls></Cads></TargetArmAds></TargetOption>
<Groups>
<Group><GroupName>App</GroupName><Files>
<File><FileName>main.c</FileName><FileType>1</FileType><FilePath>User\\main.c</FilePath></File>
<File><FileName>hmi_app.c</FileName><FileType>1</FileType><FilePath>User\\hmi\\hmi_app.c</FilePath></File>
</Files></Group>
<Group><GroupName>LVGL</GroupName><Files>
<File><FileName>lv_core.c</FileName><FileType>1</FileType><FilePath>Middlewares\\LVGL\\lv_core.c</FilePath></File>
<File><FileName>lv_conf.h</FileName><FileType>5</FileType><FilePath>Middlewares\\LVGL\\lv_conf.h</FilePath></File>
</Files></Group>
</Groups>
</Target></Targets></Project>"""


def _uvprojx_multi_xml() -> str:
    """多 Target（LVGL / Debug / Demo），无 SelectTargetNo → 不可自动判定。"""
    def _t(name: str) -> str:
        return f"""<Target><TargetName>{name}</TargetName><Groups>
<Group><GroupName>App</GroupName><Files>
<File><FileName>main.c</FileName><FileType>1</FileType><FilePath>User\\main.c</FilePath></File>
</Files></Group></Groups></Target>"""

    return "<Project><Targets>" + _t("LVGL") + _t("Debug") + _t("Demo") + "</Targets></Project>"


def _write_project(root: Path, xml: str, rel: str = "GD32F427.uvprojx") -> Path:
    """默认 uvprojx 放工程根（FilePath 相对根解析）；MDK-ARM 嵌套用 .. 前缀测。"""
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(xml, encoding="utf-8")
    return p


def _make_tree(root: Path, files: dict[str, str]) -> None:
    for rel, content in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")


# ---------- 真实验证：uvprojx 在子目录 + ../.. FilePath ----------


def test_mdk_arm_nested_project_dotdot_resolution(tmp_path: Path) -> None:
    """真实 Keil 布局：uvprojx 在 Projects/MDK-ARM/，FilePath 用 ..\\.. 到工程根。"""
    xml = """<Project><Targets><Target>
<TargetName>LVGL</TargetName>
<Groups><Group><GroupName>App</GroupName><Files>
<File><FileName>main.c</FileName><FileType>1</FileType><FilePath>..\\..\\User\\main.c</FilePath></File>
<File><FileName>hmi_app.c</FileName><FileType>1</FileType><FilePath>..\\..\\User\\hmi\\hmi_app.c</FilePath></File>
</Files></Group></Groups></Target></Targets></Project>"""
    _write_project(tmp_path, xml, "Projects/MDK-ARM/GD32F427.uvprojx")
    _make_tree(
        tmp_path,
        {
            "User/main.c": "int main(void){return 0;}\n",
            "User/hmi/hmi_app.c": "int hmi_run(void){return 0;}\n",
        },
    )
    view = resolve_keil_build(tmp_path)
    assert view.resolved is True
    assert set(view.build_files) == {"User/main.c", "User/hmi/hmi_app.c"}


# ---------- 1. 单 Target：编译文件 → project ----------


def test_single_target_build_files_project(tmp_path: Path) -> None:
    _write_project(tmp_path, _uvprojx_xml())
    _make_tree(
        tmp_path,
        {
            "User/main.c": "#include \"hmi.h\"\nint main(void){return 0;}\n",
            "User/hmi/hmi_app.c": "int hmi_run(void){return 0;}\n",
            "Middlewares/LVGL/lv_core.c": "int lv_init(void){return 0;}\n",
            "Middlewares/LVGL/lv_conf.h": "#define LV_CONF\n",
        },
    )
    # scope config: Middlewares third_party；无 ignore
    (tmp_path / ".agentxscope.yaml").write_text(
        "third_party:\n  - path: Middlewares/LVGL\n    name: LVGL\n", encoding="utf-8"
    )
    view = resolve_keil_build(tmp_path)
    assert view.resolved is True
    assert view.target == "LVGL"
    assert set(view.build_files) == {
        "User/main.c",
        "User/hmi/hmi_app.c",
        "Middlewares/LVGL/lv_core.c",
        "Middlewares/LVGL/lv_conf.h",
    }
    paths = list(view.build_files) + ["User/old_feature.c", "User/hmi/hmi_app.h"]
    # 造 hmi_app.h 让同 stem 头跟随
    _make_tree(tmp_path, {"User/hmi/hmi_app.h": "#pragma once\n"})
    classified = classify_build_scope(
        tmp_path, paths, view, include_map={"User/main.c": ["User/hmi/hmi_app.h"]}
    )
    assert classified["User/main.c"]["scope_type"] == "project"
    assert classified["User/hmi/hmi_app.c"]["scope_type"] == "project"
    # 编译源 include 的头跟随进 project
    assert classified["User/hmi/hmi_app.h"]["scope_type"] == "project"
    # third_party（即使编译）仍 third_party
    assert classified["Middlewares/LVGL/lv_core.c"]["scope_type"] == "third_party"
    assert classified["Middlewares/LVGL/lv_conf.h"]["scope_type"] == "third_party"
    # 非编译自有源码 → non_build
    assert classified["User/old_feature.c"]["scope_type"] == "non_build"


# ---------- 2. 多 Target：不允许自动 union ----------


def test_multi_target_not_auto_union(tmp_path: Path) -> None:
    _write_project(tmp_path, _uvprojx_multi_xml())
    _make_tree(tmp_path, {"User/main.c": "int main(void){return 0;}\n"})
    view = resolve_keil_build(tmp_path)
    assert view.resolved is False
    assert view.ambiguity is True
    assert view.targets == ["LVGL", "Debug", "Demo"]
    # 不自动 union：classify 不启用 build 边界（resolved=False → 退回 scope 规则）
    classified = classify_build_scope(tmp_path, ["User/main.c", "User/other.c"], view, {})
    assert classified["User/main.c"]["scope_type"] == "project"


def test_multi_target_explicit_target_resolves(tmp_path: Path) -> None:
    _write_project(tmp_path, _uvprojx_multi_xml())
    _make_tree(tmp_path, {"User/main.c": "int main(void){return 0;}\n"})
    view = resolve_keil_build(tmp_path, target_name="Debug")
    assert view.resolved is True
    assert view.target == "Debug"
    assert set(view.build_files) == {"User/main.c"}


# ---------- 3. 非 Target 自有源码 → non_build ----------


def test_non_target_code_is_non_build(tmp_path: Path) -> None:
    _write_project(tmp_path, _uvprojx_xml())
    _make_tree(
        tmp_path,
        {
            "User/main.c": "int main(void){return 0;}\n",
            "User/old_feature.c": "int old(void){return 0;}\n",
            "User/hmi/backup_module.c": "int backup(void){return 0;}\n",
        },
    )
    (tmp_path / ".agentxscope.yaml").write_text(
        "third_party:\n  - path: Middlewares/LVGL\n    name: LVGL\n"
    )
    view = resolve_keil_build(tmp_path)
    paths = ["User/main.c", "User/old_feature.c", "User/hmi/backup_module.c"]
    classified = classify_build_scope(tmp_path, paths, view, {})
    assert classified["User/main.c"]["scope_type"] == "project"
    assert classified["User/old_feature.c"]["scope_type"] == "non_build"
    assert classified["User/hmi/backup_module.c"]["scope_type"] == "non_build"


# ---------- 4. ignored 即使在 Target 也保持 ignored ----------


def test_ignored_stays_ignored_even_in_target(tmp_path: Path) -> None:
    _write_project(tmp_path, _uvprojx_xml())
    _make_tree(tmp_path, {"User/main.c": "int main(void){return 0;}\n"})
    # ignore 命中 User/main.c 本身（*main.c）
    (tmp_path / ".agentxscope.yaml").write_text(
        "ignore:\n  - \"*main.c\"\n", encoding="utf-8"
    )
    view = resolve_keil_build(tmp_path)
    classified = classify_build_scope(tmp_path, ["User/main.c"], view, {})
    assert "User/main.c" not in classified  # ignored → 不进入 Index


# ---------- 6. build 边界包含传递 include 的头文件 ----------


def test_boundary_includes_transitive_headers(tmp_path: Path) -> None:
    _write_project(tmp_path, _uvprojx_xml())
    _make_tree(
        tmp_path,
        {
            "User/main.c": "int main(void){return 0;}\n",
            "User/hmi/hmi_app.c": "int hmi_run(void){return 0;}\n",
            "User/hmi/hmi_app.h": "#pragma once\n",
            "User/hmi/config.h": "#pragma once\n",
        },
    )
    include_map = {
        "User/main.c": ["User/hmi/hmi_app.h"],
        "User/hmi/hmi_app.h": ["User/hmi/config.h"],
    }
    available = {
        "User/main.c",
        "User/hmi/hmi_app.c",
        "User/hmi/hmi_app.h",
        "User/hmi/config.h",
    }
    view = resolve_keil_build(tmp_path)
    boundary = build_boundary_files(view, include_map, available)
    assert "User/main.c" in boundary
    assert "User/hmi/hmi_app.h" in boundary  # 同 stem + include
    assert "User/hmi/config.h" in boundary  # 传递 include


# ---------- 7. Build 解析失败：不伪装 ----------


def test_no_keil_project_unresolved(tmp_path: Path) -> None:
    _make_tree(tmp_path, {"User/main.c": "int main(void){return 0;}\n"})
    assert find_keil_project(tmp_path) is None
    view = resolve_keil_build(tmp_path)
    assert view.resolved is False
    assert view.project_file is None
    # classify 退回 scope 目录规则（project 默认）
    classified = classify_build_scope(tmp_path, ["User/main.c", "User/x.c"], view, {})
    assert classified["User/main.c"]["scope_type"] == "project"


def test_build_parse_failure_degraded(tmp_path: Path) -> None:
    # 工程存在但无法解析（非 XML）
    _write_project(tmp_path, "not xml", "bad.uvprojx")
    _make_tree(tmp_path, {"User/main.c": "int main(void){return 0;}\n"})
    view = resolve_keil_build(tmp_path)
    assert view.resolved is False
    assert view.targets == []
    assert view.ambiguity is False  # 不是多 Target，是解析失败
    summary = build_scope_summary(view, {}, 0)
    assert summary["resolved"] is False


# ---------- scope 汇总（第 8 节 Wizard 数据源） ----------


def test_build_scope_summary_counts(tmp_path: Path) -> None:
    _write_project(tmp_path, _uvprojx_xml())
    _make_tree(
        tmp_path,
        {
            "User/main.c": "x",
            "User/hmi/hmi_app.c": "x",
            "User/hmi/hmi_app.h": "x",
            "User/old.c": "x",
            "Middlewares/LVGL/lv_core.c": "x",
            "Middlewares/LVGL/lv_conf.h": "x",
        },
    )
    (tmp_path / ".agentxscope.yaml").write_text(
        "third_party:\n  - path: Middlewares/LVGL\n    name: LVGL\n"
        "ignore:\n  - \"*.md\"\n"
    )
    view = resolve_keil_build(tmp_path)
    paths = [
        "User/main.c",
        "User/hmi/hmi_app.c",
        "User/hmi/hmi_app.h",
        "User/old.c",
        "Middlewares/LVGL/lv_core.c",
        "Middlewares/LVGL/lv_conf.h",
        "README.md",
    ]
    classified = classify_build_scope(tmp_path, paths, view, {})
    summary = build_scope_summary(view, classified, 1)
    assert summary["target"] == "LVGL"
    assert summary["build_files"] == 4
    assert summary["project"] == 3  # main.c, hmi_app.c, hmi_app.h(跟随)
    assert summary["non_build"] == 1  # old.c
    assert summary["third_party"] == 2
    assert summary["ignored"] == 1  # README.md


# ---------- scope matcher 修复：/** glob 形式 ----------


def test_third_party_glob_form_matches(tmp_path: Path) -> None:
    from agentx.scope.config import load_scope_config, scope_of_file

    (tmp_path / ".agentxscope.yaml").write_text(
        "third_party:\n  - path: Middlewares/**\n    name: **\n"
        "  - path: Drivers/CMSIS/**\n    name: **\n",
        encoding="utf-8",
    )
    cfg = load_scope_config(tmp_path)
    # 归一化：路径去 /**，name 从路径推导（不保留 **）
    assert cfg["third_party"] == [
        {"path": "Middlewares", "name": "Middlewares"},
        {"path": "Drivers/CMSIS", "name": "CMSIS"},
    ]
    assert scope_of_file("Middlewares/LVGL/lv_core.c", cfg) == ("third_party", "Middlewares")
    assert scope_of_file("Drivers/CMSIS/core_cm4.h", cfg) == ("third_party", "CMSIS")
    assert scope_of_file("User/main.c", cfg) == ("project", None)
