"""CodeGraph vendored distribution 测试（A' 方案）。

覆盖：vendored 发现 / 外部 override / bootstrap 下载 / SHA512 校验 /
unsupported platform / 无网络 / telemetry / 版本锁定 / graph.py 兼容 / filescan fallback。
"""

from __future__ import annotations

import base64
import hashlib
import json
import tarfile
from pathlib import Path

import pytest

from agentx.understanding import graph as graph_mod
from agentx.vendor import bootstrap as bs
from agentx.vendor.manifest import CODEGRAPH_VERSION, NODE_BIN_REL, NODE_REL


@pytest.fixture()
def vendor_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """隔离 vendor 目录（AGENTX_VENDOR_DIR → tmp_path）。"""
    vendor = tmp_path / "vendor"
    vendor.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv(bs.VENDOR_DIR_ENV, str(vendor))
    return vendor


def _fake_httpx(monkeypatch: pytest.MonkeyPatch, tarball: bytes, integrity: str) -> None:
    """替换 httpx.Client 为假实现：registry 元数据 + 流式下载 bytes。"""

    class FakeRegistry:
        def __init__(self) -> None:
            self._integrity = integrity

        def get(self, url: str, timeout: int) -> FakeRegistry:
            return self

        def raise_for_status(self) -> None:
            pass

        def json(self) -> dict:
            return {"versions": {CODEGRAPH_VERSION: {"dist": {"integrity": self._integrity}}}}

    class FakeStream:
        status_code = 200

        def __enter__(self) -> FakeStream:
            return self

        def __exit__(self, *a) -> None:
            return None

        def raise_for_status(self) -> None:
            pass

        def iter_bytes(self, chunk_size: int):
            yield tarball

    class FakeClient:
        def __init__(self, **kw) -> None:
            self.registry = FakeRegistry()

        def __enter__(self) -> FakeClient:
            return self

        def __exit__(self, *a) -> None:
            return None

        def get(self, url: str, timeout: int):
            return self.registry.get(url, timeout)

        def stream(self, method: str, url: str, timeout: int, headers: dict | None = None):
            return FakeStream()

    import httpx

    monkeypatch.setattr(httpx, "Client", FakeClient)


def _install_fake_binary(vendor_root: Path) -> tuple[Path, Path]:
    """在 vendor 目录里伪造一个"已安装"的 CodeGraph。"""
    base = vendor_root / "codegraph" / CODEGRAPH_VERSION
    base.mkdir(parents=True)
    (base / NODE_REL).write_bytes(b"fake-node")
    (base / "lib" / "dist" / "bin").mkdir(parents=True)
    bin_path = base.joinpath(*NODE_BIN_REL)
    bin_path.write_bytes(b"fake-codegraph")
    (base / bs.VERSION_FILE).write_text(CODEGRAPH_VERSION, encoding="utf-8")
    return base / NODE_REL, bin_path


# ---------- vendored 发现 / 版本锁定 ----------


def test_vendored_detected(vendor_env: Path) -> None:
    node, bin_path = _install_fake_binary(vendor_env)
    assert bs.installed_node_bin() == (node, bin_path)


def test_vendored_version_mismatch_triggers_reinstall(vendor_env: Path) -> None:
    _install_fake_binary(vendor_env)
    (vendor_env / "codegraph" / CODEGRAPH_VERSION / bs.VERSION_FILE).write_text(
        "0.9.9", encoding="utf-8"
    )
    assert bs.installed_node_bin() is None  # 版本漂移 → 视为未安装


def test_vendored_incomplete_not_detected(vendor_env: Path) -> None:
    (vendor_env / "codegraph" / CODEGRAPH_VERSION).mkdir(parents=True)
    (vendor_env / "codegraph" / CODEGRAPH_VERSION / bs.VERSION_FILE).write_text(
        CODEGRAPH_VERSION, encoding="utf-8"
    )
    assert bs.installed_node_bin() is None


# ---------- 外部 override ----------


def test_external_override_wins(vendor_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fake_binary(vendor_env)
    ext_node = vendor_env / "ext-node.exe"
    ext_bin = vendor_env / "ext-codegraph.js"
    ext_node.write_bytes(b"n")
    ext_bin.write_bytes(b"b")
    monkeypatch.setenv("CODEGRAPH_BIN", str(ext_bin))
    monkeypatch.setenv("CODEGRAPH_NODE", str(ext_node))
    node, bin_path, reason = bs.ensure_codegraph()
    assert reason is None
    assert node == ext_node
    assert bin_path == ext_bin


def test_external_broken_no_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CODEGRAPH_BIN", "C:/nonexistent/codegraph.js")
    monkeypatch.setenv("CODEGRAPH_NODE", "C:/nonexistent/node.exe")
    node, bin_path, reason = bs.ensure_codegraph()
    assert node is None
    assert bin_path is None
    assert "不存在" in (reason or "")


# ---------- bootstrap 下载 + SHA512 ----------


def _make_tgz(package_root: Path, target: str) -> bytes:
    """构造与官方 npm 平台包同构的 tgz（package/node.exe + lib/dist/bin/codegraph.js）。"""
    import io

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as t:
        for rel in (NODE_REL, "lib/dist/bin/codegraph.js"):
            p = package_root / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(b"payload")
            info = t.gettarinfo(str(p), arcname=f"package/{rel}")
            with p.open("rb") as f:
                t.addfile(info, f)
    return buf.getvalue()


def _sha512_b64(data: bytes) -> str:
    return base64.b64encode(hashlib.sha512(data).digest()).decode()


def test_bootstrap_downloads_and_installs(
    vendor_env: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    tarball = _make_tgz(tmp_path / "pkg", "win32-x64")
    integrity = f"sha512-{_sha512_b64(tarball)}"
    _fake_httpx(monkeypatch, tarball, integrity)
    monkeypatch.setattr(bs, "EXPECTED_SHA512", {})  # 跳过固化比对（只测实时 integrity 路径）

    node, bin_path = bs.bootstrap_install()
    assert node.name == NODE_REL
    assert bin_path.name == "codegraph.js"
    assert (node.parent / bs.VERSION_FILE).read_text(encoding="utf-8").strip() == CODEGRAPH_VERSION
    assert (node.parent / bs.LICENSE_FILE).is_file()
    meta = json.loads((node.parent / bs.METADATA_FILE).read_text(encoding="utf-8"))
    assert meta["version"] == CODEGRAPH_VERSION
    assert meta["sha512"] == _sha512_b64(tarball)


def test_bootstrap_sha512_mismatch_rejected(
    vendor_env: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    tarball = _make_tgz(tmp_path / "pkg", "win32-x64")
    wrong = f"sha512-{_sha512_b64(b'other')}"
    _fake_httpx(monkeypatch, tarball, wrong)
    monkeypatch.setattr(bs, "EXPECTED_SHA512", {})

    with pytest.raises(bs.BootstrapError, match="校验失败"):
        bs.bootstrap_install()
    # 半成品必须清理
    assert not (vendor_env / "codegraph" / CODEGRAPH_VERSION).exists()


def test_bootstrap_no_network_falls_back(vendor_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    class Boom(Exception):
        pass

    class FakeClient:
        def __init__(self, **kw) -> None:
            pass

        def __enter__(self) -> FakeClient:
            return self

        def __exit__(self, *a) -> None:
            return None

        def get(self, url: str, timeout: int):
            raise Boom("network down")

        def stream(self, method: str, url: str, timeout: int):
            raise Boom("network down")

    import httpx

    monkeypatch.setattr(httpx, "Client", FakeClient)
    node, bin_path, reason = bs.ensure_codegraph()
    assert node is None
    assert bin_path is None
    assert reason is not None


# ---------- unsupported platform / fail-fast ----------


def test_unsupported_platform(vendor_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(bs, "current_target", lambda: None)
    with pytest.raises(bs.UnsupportedPlatformError):
        bs.bootstrap_install()


def test_required_fail_fast(vendor_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CODEGRAPH_BIN", raising=False)
    monkeypatch.delenv("CODEGRAPH_NODE", raising=False)

    class Boom(Exception):
        pass

    class FakeClient:
        def __init__(self, **kw) -> None:
            pass

        def __enter__(self) -> FakeClient:
            return self

        def __exit__(self, *a) -> None:
            return None

        def get(self, url: str, timeout: int):
            raise Boom("down")

        def stream(self, method: str, url: str, timeout: int):
            raise Boom("down")

    import httpx

    monkeypatch.setattr(httpx, "Client", FakeClient)
    monkeypatch.setenv(bs.REQUIRED_ENV, "1")
    with pytest.raises(bs.BootstrapError, match="REQUIRED"):
        bs.ensure_codegraph()


# ---------- graph.py 集成 ----------


def test_graph_env_uses_vendored(vendor_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CODEGRAPH_BIN", raising=False)
    monkeypatch.delenv("CODEGRAPH_NODE", raising=False)
    monkeypatch.setattr(graph_mod, "_bootstrap_failed", None)
    node, bin_path = _install_fake_binary(vendor_env)
    assert graph_mod.codegraph_env() == (node, bin_path)
    assert graph_mod.codegraph_available()


def test_graph_env_external(vendor_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ext_node = vendor_env / "n.exe"
    ext_bin = vendor_env / "cg.js"
    ext_node.write_bytes(b"n")
    ext_bin.write_bytes(b"b")
    monkeypatch.setenv("CODEGRAPH_BIN", str(ext_bin))
    monkeypatch.setenv("CODEGRAPH_NODE", str(ext_node))
    monkeypatch.setattr(graph_mod, "_bootstrap_failed", None)
    assert graph_mod.codegraph_env() == (ext_node, ext_bin)


def test_graph_unavailable_falls_back_filescan(
    vendor_env: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("CODEGRAPH_BIN", raising=False)
    monkeypatch.delenv("CODEGRAPH_NODE", raising=False)
    monkeypatch.setattr(graph_mod, "_bootstrap_failed", None)
    monkeypatch.setattr(bs, "current_target", lambda: None)  # 平台不支持 → 不下载
    (tmp_path / "a.c").write_text("int main(void) { return 0; }", encoding="utf-8")
    graph = graph_mod.analyze_project(tmp_path)
    assert graph.source == "filescan"
    assert any("CodeGraph" in e for e in graph.errors)


def test_telemetry_env_injected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CODEGRAPH_TELEMETRY", raising=False)
    monkeypatch.delenv("DO_NOT_TRACK", raising=False)
    captured: dict[str, str] = {}

    def fake_run(cmd, **kw):
        captured.update(kw["env"])
        raise RuntimeError("stop")

    import subprocess

    monkeypatch.setattr(subprocess, "run", fake_run)
    provider = graph_mod.CliCodeGraphProvider()
    with pytest.raises(RuntimeError):
        provider._run(Path("node"), Path("cg.js"), ["status", "x"])
    assert captured.get("CODEGRAPH_TELEMETRY") == "0"


def test_telemetry_respects_user_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CODEGRAPH_TELEMETRY", "1")
    captured: dict[str, str] = {}

    def fake_run(cmd, **kw):
        captured.update(kw["env"])
        raise RuntimeError("stop")

    import subprocess

    monkeypatch.setattr(subprocess, "run", fake_run)
    provider = graph_mod.CliCodeGraphProvider()
    with pytest.raises(RuntimeError):
        provider._run(Path("node"), Path("cg.js"), ["status", "x"])
    assert captured.get("CODEGRAPH_TELEMETRY") == "1"


def test_reindex_recommended_triggers_rebuild(
    vendor_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """旧版建的库（reindexRecommended=true）→ 明确 init 重建，不静默复用。"""
    monkeypatch.delenv("CODEGRAPH_BIN", raising=False)
    monkeypatch.delenv("CODEGRAPH_NODE", raising=False)
    monkeypatch.setattr(graph_mod, "_bootstrap_failed", None)
    node, bin_path = _install_fake_binary(vendor_env)
    project = vendor_env / "proj"
    project.mkdir()
    (project / ".codegraph").mkdir()
    calls: list[list[str]] = []
    outputs = [
        {
            "initialized": True,
            "pendingChanges": {},
            "index": {"reindexRecommended": True, "builtWithVersion": None},
        },
        "ok",
        {
            "initialized": True,
            "pendingChanges": {},
            "index": {"reindexRecommended": False, "builtWithVersion": "1.6.0"},
        },
        [],
        {"initialized": True, "pendingChanges": {}},
    ]

    def fake_run(self, n, b, args):
        calls.append(list(args))
        return outputs.pop(0)

    monkeypatch.setattr(graph_mod.CliCodeGraphProvider, "_run", fake_run)
    monkeypatch.setattr(graph_mod.CliCodeGraphProvider, "_run_json", fake_run)
    provider = graph_mod.CliCodeGraphProvider()
    provider.analyze_project(project)
    assert ["init", str(project)] in calls  # 旧库 → 明确全量重建
    assert not any("sync" in c for c in calls[:2])  # 重建后不再重复 sync
