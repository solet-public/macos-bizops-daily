"""Public facade for the stdlib-only bootstrap operation adapter."""

from __future__ import annotations

from .dependency import ClosureState, probe_dependency_closure
from .protocol import operation_adapter_main as _run_protocol
from .routes import execute_adapter_request

__all__ = [
    "ClosureState",
    "execute_adapter_request",
    "operation_adapter_main",
    "probe_dependency_closure",
]


def operation_adapter_main() -> int:
    """Run the closed stdin/stdout adapter using the reviewed route registry."""

    return _run_protocol(execute_adapter_request)
