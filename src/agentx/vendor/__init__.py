"""CodeGraph vendored distribution（A' 方案）：锁定版本 + 自动 bootstrap。"""

from agentx.vendor.bootstrap import (
    BootstrapError,
    UnsupportedPlatformError,
    bootstrap_install,
    bootstrap_status,
    ensure_codegraph,
    external_node_bin,
    installed_node_bin,
    vendor_root,
)
from agentx.vendor.manifest import CODEGRAPH_VERSION

__all__ = [
    "BootstrapError",
    "CODEGRAPH_VERSION",
    "UnsupportedPlatformError",
    "bootstrap_install",
    "bootstrap_status",
    "ensure_codegraph",
    "external_node_bin",
    "installed_node_bin",
    "vendor_root",
]
