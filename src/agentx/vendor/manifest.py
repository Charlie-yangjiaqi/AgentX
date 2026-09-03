"""CodeGraph vendored distribution manifest（唯一版本事实源）。

AgentX 锁定 CodeGraph 版本并自动 bootstrap（A' 方案）：
用户安装 AgentX 后无需单独安装 CodeGraph。

许可证：@colbymchenry/codegraph 为 MIT（Copyright (c) 2026 Colby Mchenry），
再分发义务：所有副本/实质性部分须包含版权声明与 MIT 全文（见 _LICENSE_TEXT）。
"""

from __future__ import annotations

import platform
import sys
from typing import Final

# 锁定版本：AgentX 升级时在此手动 bump（与 CodeGraph 官方节奏解耦）
CODEGRAPH_VERSION: Final[str] = "1.6.0"

# npm registry 平台包（win32-x64 等，1.6.0 起官方按平台分发，含自带 node.exe）
NPM_TARBALL: Final[str] = (
    "https://registry.npmjs.org/@colbymchenry/codegraph-{target}/-/codegraph-{target}-{version}.tgz"
)
# 备选：官方 GitHub Releases 自包含 zip（install.ps1 同款产物）
GH_RELEASE: Final[str] = (
    "https://github.com/colbymchenry/codegraph/releases/download/v{version}/codegraph-{target}.zip"
)

# 发布时固化的 npm integrity（sha512）——与下载时实时读取的 registry integrity 双重校验
# 生成方式：GET registry.npmjs.org/@colbymchenry/codegraph-{target} → dist.integrity
EXPECTED_SHA512: Final[dict[str, str]] = {
    "win32-x64": (
        "zSjrLgkE2j2UYsPp6ThlVKL4xoZ1WnMkOfNH8i3XFGNXBS8p500WFA9bh0JJJ+IQx6vhtltTxTTdlVmVICGYsw=="
    ),
}

# 平台 → 官方 target 名映射
PLATFORM_MAP: Final[dict[tuple[str, str], str]] = {
    ("win32", "AMD64"): "win32-x64",
    ("win32", "ARM64"): "win32-arm64",
    ("linux", "x86_64"): "linux-x64",
    ("linux", "aarch64"): "linux-arm64",
    ("darwin", "x86_64"): "darwin-x64",
    ("darwin", "arm64"): "darwin-arm64",
}

# 环境变量
VENDOR_DIR_ENV: Final[str] = "AGENTX_VENDOR_DIR"  # 覆盖 vendor 根目录（测试/企业用）
MIRROR_ENV: Final[str] = "CODEGRAPH_MIRROR"  # 下载源镜像前缀
REQUIRED_ENV: Final[str] = "AGENTX_CODEGRAPH_REQUIRED"  # =1 时 bootstrap 失败 fail-fast
TELEMETRY_ENV: Final[str] = "CODEGRAPH_TELEMETRY"
DO_NOT_TRACK_ENV: Final[str] = "DO_NOT_TRACK"

# 包内相对路径（1.6.0 npm 平台包结构，已实测）
NODE_REL: Final[str] = "node.exe"  # 自带 vendored Node（win 下是 node.exe）
NODE_BIN_REL: Final[list[str]] = ["lib", "dist", "bin", "codegraph.js"]

# 下载超时 / 校验
DOWNLOAD_TIMEOUT_S: Final[int] = 600


def current_target() -> str | None:
    """当前平台的官方 target 名；不支持的平台返回 None。"""
    return PLATFORM_MAP.get((sys.platform, platform.machine()))


# MIT 全文（@colbymchenry/codegraph，取自 https://github.com/colbymchenry/codegraph/LICENSE）
CODEGRAPH_LICENSE_TEXT: Final[str] = """MIT License

Copyright (c) 2026 Colby Mchenry

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
"""
