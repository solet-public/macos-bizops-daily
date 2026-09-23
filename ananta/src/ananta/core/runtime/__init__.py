"""Runtime utilities for Ananta services.

This module provides utilities for:
- Dynamic port allocation and discovery
- Runtime file management (port files, PID files)
- Service discovery between components
"""

from .drain_sentinel import (
    DRAINING_SENTINEL_SUFFIX,
    draining_sentinel_path,
    is_draining,
)
from .port_manager import (
    PortManager,
    find_available_port,
    get_runtime_dir,
    read_port_file,
    write_port_file,
    write_routerless_bridge_port_file,
)

__all__ = [
    "DRAINING_SENTINEL_SUFFIX",
    "PortManager",
    "draining_sentinel_path",
    "find_available_port",
    "get_runtime_dir",
    "is_draining",
    "read_port_file",
    "write_port_file",
    "write_routerless_bridge_port_file",
]
