"""CodeGraph bootstrap：下载 → SHA512 双重校验 → 原子安装 → 版本锁定。

调用优先级（graph.py resolver 使用）：
1. 显式 env CODEGRAPH_BIN/CODEGRAPH_NODE（用户接管，不 bootstrap）
2. vendored（~/.agentx/vendor/codegraph/<version>/，VERSION 匹配）
3. bootstrap 自动下载安装（版本锁定 + 校验，失败不产生半成品）
4. 调用方降级 filescan（本模块不抛阻断异常，返回原因）
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import json
import os
import platform
import shutil
import sys
import tarfile
import tempfile
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from agentx.vendor.manifest import (
    CODEGRAPH_LICENSE_TEXT,
    CODEGRAPH_VERSION,
    DOWNLOAD_TIMEOUT_S,
    EXPECTED_SHA512,
    GH_RELEASE,
    MIRROR_ENV,
    NODE_BIN_REL,
    NODE_REL,
    NPM_TARBALL,
    REQUIRED_ENV,
    VENDOR_DIR_ENV,
    current_target,
)

VERSION_FILE = "VERSION"
METADATA_FILE = "metadata.json"
LICENSE_FILE = "LICENSE"

# 断点续传最大尝试次数（网络中断后按 Range 续传，避免 50MB 重头下载）
_MAX_DOWNLOAD_ATTEMPTS = 5


class BootstrapError(Exception):
    """CodeGraph bootstrap 失败（带用户可读原因）。"""


class UnsupportedPlatformError(BootstrapError):
    """当前平台没有官方 binary。"""


def vendor_root() -> Path:
    env = os.environ.get(VENDOR_DIR_ENV)
    if env:
        return Path(env)
    return Path.home() / ".agentx" / "vendor"


def install_dir() -> Path:
    return vendor_root() / "codegraph" / CODEGRAPH_VERSION


def installed_node_bin() -> tuple[Path, Path] | None:
    """返回 (node, codegraph.js)；已安装且版本匹配才返回。"""
    base = install_dir()
    node = base / NODE_REL
    bin_path = base.joinpath(*NODE_BIN_REL)
    version_file = base / VERSION_FILE
    if not (node.is_file() and bin_path.is_file()):
        return None
    try:
        installed = version_file.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if installed != CODEGRAPH_VERSION:
        return None  # 版本漂移 → 视为未安装（调用方会重新 bootstrap）
    return node, bin_path


def external_node_bin() -> tuple[Path, Path] | None:
    """显式 env CODEGRAPH_BIN/CODEGRAPH_NODE（用户接管，最高优先）。"""
    bin_env = os.environ.get("CODEGRAPH_BIN")
    if not bin_env:
        return None
    bin_path = Path(bin_env)
    node_env = os.environ.get("CODEGRAPH_NODE")
    node = Path(node_env) if node_env else None
    if node is not None and not node.is_file():
        raise BootstrapError(f"CODEGRAPH_NODE 指向的文件不存在: {node}")
    if not bin_path.is_file():
        raise BootstrapError(f"CODEGRAPH_BIN 指向的文件不存在: {bin_path}")
    if node is None:
        raise BootstrapError(
            "设置了 CODEGRAPH_BIN 但未设置 CODEGRAPH_NODE（旧版 JS 架构需要 node）"
        )
    return node, bin_path


def _sha512_b64(data: bytes) -> str:
    return base64.b64encode(hashlib.sha512(data).digest()).decode()


def _npm_integrity(client: httpx.Client, target: str) -> str:
    """实时从 npm registry 元数据读取 dist.integrity（sha512）。"""
    url = f"https://registry.npmjs.org/@colbymchenry/codegraph-{target}"
    resp = client.get(url, timeout=DOWNLOAD_TIMEOUT_S)
    resp.raise_for_status()
    try:
        integrity = str(resp.json()["versions"][CODEGRAPH_VERSION]["dist"]["integrity"])
    except (KeyError, ValueError) as e:
        raise BootstrapError(f"npm registry 元数据缺少 {CODEGRAPH_VERSION} 的 integrity") from e
    if not integrity.startswith("sha512-"):
        raise BootstrapError(f"npm integrity 格式异常: {integrity[:32]}...")
    return integrity


def _verify_expected(integrity: str) -> None:
    """manifest 固化值与 registry 实时值必须一致（双重校验）。"""
    expected = EXPECTED_SHA512.get(current_target() or "")
    if not expected:
        return  # 未固化平台（后续版本补充），仍执行实时校验
    if integrity != f"sha512-{expected}":
        raise BootstrapError(
            "npm integrity 与 AgentX 固化的 SHA512 不一致，可能供应链异常，已拒绝安装"
        )


def _download(client: httpx.Client, url: str, dest: Path, expected_b64: str) -> None:
    """下载并校验：Range 断点续传（最多 _MAX_DOWNLOAD_ATTEMPTS 次），失败清理。

    网络中断时从头重试成本高（~50MB），续传按已接收字节继续；
    服务器不支持 Range（返回 200）则重头下载。
    """
    for attempt in range(1, _MAX_DOWNLOAD_ATTEMPTS + 1):
        offset = dest.stat().st_size if dest.exists() else 0
        headers = {"Range": f"bytes={offset}-"} if offset else {}
        digest = hashlib.sha512()
        if offset:
            # 续传时对已接收部分先喂进摘要
            with dest.open("rb") as f:
                for chunk in iter(lambda: f.read(1 << 16), b""):
                    digest.update(chunk)
        try:
            with client.stream("GET", url, timeout=DOWNLOAD_TIMEOUT_S, headers=headers) as resp:
                if resp.status_code == 206:
                    mode = "ab"
                elif resp.status_code == 200:
                    mode = "wb"  # 服务器不支持 Range：重头下载
                    offset = 0
                    digest = hashlib.sha512()
                else:
                    resp.raise_for_status()
                    return
                with dest.open(mode) as f:
                    for chunk in resp.iter_bytes(chunk_size=1 << 16):
                        f.write(chunk)
                        digest.update(chunk)
            break  # 完整下载成功
        except Exception as e:
            if attempt >= _MAX_DOWNLOAD_ATTEMPTS:
                dest.unlink(missing_ok=True)
                raise BootstrapError(f"下载失败（已重试 {attempt} 次）: {e}") from e
            # 保留已接收字节，下一轮续传
            continue
    actual = base64.b64encode(digest.digest()).decode()
    if actual != expected_b64:
        dest.unlink(missing_ok=True)
        raise BootstrapError("下载内容 SHA512 校验失败，已删除并拒绝执行")


def _extract_archive(archive: Path, dest: Path, url: str) -> None:
    """解压 npm tgz / GitHub zip 到 dest（纯 Python，无外部依赖）。"""
    if url.endswith(".zip"):
        with zipfile.ZipFile(archive) as z:
            z.extractall(dest)
    else:
        with tarfile.open(archive, "r:*") as t:
            t.extractall(dest, filter="data")  # filter=data：拒绝路径穿越/特殊文件


def _write_metadata(installed: Path, target: str, source: str, sha512: str) -> None:
    meta = {
        "version": CODEGRAPH_VERSION,
        "target": target,
        "source": source,
        "downloaded_at": datetime.now(UTC).isoformat(),
        "sha512": sha512,
    }
    (installed / METADATA_FILE).write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def bootstrap_install(*, force: bool = False) -> tuple[Path, Path]:
    """下载并安装锁定版本的 CodeGraph（幂等）。

    失败抛 BootstrapError（不产生半成品：临时目录清理 + 原子 rename）。
    force=True 时跳过已安装检查（agentx codegraph install/upgrade）。
    """
    target = current_target()
    if target is None:
        raise UnsupportedPlatformError(
            f"CodeGraph 不支持当前平台 ({sys.platform}/{platform.machine()})"
        )
    installed = installed_node_bin()
    if installed is not None and not force:
        return installed

    mirror = os.environ.get(MIRROR_ENV)
    urls: list[str] = []
    if mirror:
        urls.append(mirror.format(target=target, version=CODEGRAPH_VERSION))
    urls.append(NPM_TARBALL.format(target=target, version=CODEGRAPH_VERSION))
    urls.append(GH_RELEASE.format(target=target, version=CODEGRAPH_VERSION))

    errors: list[str] = []
    with httpx.Client(follow_redirects=True) as client:
        for url in urls:
            tmpdir = Path(tempfile.mkdtemp(prefix="agentx-cg-"))
            archive = tmpdir / "cg"
            try:
                if url == urls[0] or url.startswith("https://registry.npmjs.org"):
                    integrity = _npm_integrity(client, target)
                    _verify_expected(integrity)
                    expected_b64 = integrity.split("-", 1)[1]
                else:
                    expected = EXPECTED_SHA512.get(target)
                    if not expected:
                        raise BootstrapError("GitHub 通道无固化 SHA512，已拒绝")
                    expected_b64 = expected
                _download(client, url, archive, expected_b64)

                unpacked = tmpdir / "unpacked"
                unpacked.mkdir()
                _extract_archive(archive, unpacked, url)
                package = unpacked / "package"
                node = package / NODE_REL
                bin_path = package.joinpath(*NODE_BIN_REL)
                if not (node.is_file() and bin_path.is_file()):
                    raise BootstrapError(f"下载包结构异常：缺少 {NODE_REL} 或 codegraph.js")

                # 原子安装：先 rename 到最终目录，避免半成品被读取
                final = install_dir()
                final.parent.mkdir(parents=True, exist_ok=True)
                if final.exists():
                    shutil.rmtree(final)
                package.replace(final)
                (final / VERSION_FILE).write_text(CODEGRAPH_VERSION, encoding="utf-8")
                (final / LICENSE_FILE).write_text(CODEGRAPH_LICENSE_TEXT, encoding="utf-8")
                _write_metadata(final, target, "npm" if "npmjs" in url else "github", expected_b64)
                return final / NODE_REL, final.joinpath(*NODE_BIN_REL)
            except Exception as e:  # 尝试下一个源
                errors.append(f"{url.split('/')[2]}: {e}")
            finally:
                shutil.rmtree(tmpdir, ignore_errors=True)
    raise BootstrapError("全部下载源失败：" + "；".join(errors))


def ensure_codegraph() -> tuple[Path | None, Path | None, str | None]:
    """统一解析入口：返回 (node, bin, error_reason)。

    优先级：显式 env → vendored → bootstrap → (None, None, reason)。
    不抛异常（fail-fast 由调用方按 REQUIRED_ENV 决定）。
    """
    try:
        ext = external_node_bin()
        if ext is not None:
            return *ext, None
    except BootstrapError as e:
        if os.environ.get(REQUIRED_ENV) == "1":
            raise BootstrapError(
                f"CodeGraph 外部配置错误（AGENTX_CODEGRAPH_REQUIRED=1 强制要求）: {e}"
            ) from e
        return None, None, str(e)

    vendored = installed_node_bin()
    if vendored is not None:
        return *vendored, None

    try:
        return *bootstrap_install(), None
    except BootstrapError as e:
        reason = str(e)
    except Exception as e:  # 网络/IO 等意外
        reason = f"{type(e).__name__}: {e}"
    if os.environ.get(REQUIRED_ENV) == "1":
        raise BootstrapError(
            f"CodeGraph bootstrap 失败（AGENTX_CODEGRAPH_REQUIRED=1 强制要求）: {reason}"
        )
    return None, None, reason


def bootstrap_status() -> dict[str, Any]:
    """CLI status 用：当前 vendor 状态。"""
    target = current_target()
    vendored = installed_node_bin()
    status: dict[str, Any] = {
        "version": CODEGRAPH_VERSION,
        "target": target,
        "installed": vendored is not None,
    }
    if vendored is not None:
        node, bin_path = vendored
        status["node"] = str(node)
        status["bin"] = str(bin_path)
        meta_file = node.parent / METADATA_FILE
        if meta_file.is_file():
            with contextlib.suppress(OSError, json.JSONDecodeError):
                status["metadata"] = json.loads(meta_file.read_text(encoding="utf-8"))
    return status
