"""Physical-memory assertions used by the bootstrap adapter contract smoke."""

from __future__ import annotations

from pathlib import Path
from types import ModuleType
from typing import Any
from unittest.mock import patch


class _Version39(tuple[int, int, int, str, int]):
    @property
    def major(self) -> int:
        return self[0]

    @property
    def minor(self) -> int:
        return self[1]

    @property
    def micro(self) -> int:
        return self[2]


def check_python_guard(
    *,
    module: ModuleType,
    root: Path,
    request_factory: Any,
    execute: Any,
    completed: Any,
    check: Any,
) -> None:
    """An unsupported running interpreter never silently verifies setup."""
    request = request_factory(
        root,
        operation_id="python_version_valid",
        operation_ref="bootstrap::python.probe_version",
        purpose="stage_exit",
    )
    with patch.object(module.sys, "version_info", _Version39((3, 9, 6, "final", 0))):
        result = execute(module, request, lambda *_args, **_kwargs: completed(), lambda _name: None)
    check(result["checkpoint_status"] == "blocked", "Python 3.9 cannot silently verify")


def check_physical_memory_guard(
    *,
    module: ModuleType,
    root: Path,
    request_factory: Any,
    execute: Any,
    completed: Any,
    check: Any,
) -> None:
    """The host-capacity probe is fixed, fail-closed, and accepts the 24 GB floor."""
    request = request_factory(
        root,
        operation_id="minimum_physical_memory_valid",
        operation_ref="bootstrap::host.probe_physical_memory",
        purpose="stage_entry",
    )
    command = ["/usr/sbin/sysctl", "-n", "hw.memsize"]

    def runner(memory: str, code: int = 0) -> Any:
        def run(actual: list[str], **_kwargs: object) -> Any:
            check(actual == command, "physical-memory probe uses the fixed sysctl command")
            return completed(code, stdout=memory)

        return run

    below = execute(module, request, runner("17179869184\n"), lambda _name: None)
    check(
        below["checkpoint_status"] == "blocked"
        and below["error_kind"] == "physical_memory_below_minimum",
        "16 GB physical memory fails closed before setup",
    )
    floor = execute(module, request, runner("24000000000\n"), lambda _name: None)
    check(floor["checkpoint_status"] == "verified", "24 GB physical memory verifies")
    malformed = execute(module, request, runner("not-a-number\n"), lambda _name: None)
    check(
        malformed["checkpoint_status"] == "blocked"
        and malformed["error_kind"] == "physical_memory_unavailable",
        "malformed physical-memory output fails closed",
    )
    unavailable = execute(module, request, runner("", code=1), lambda _name: None)
    check(
        unavailable["checkpoint_status"] == "blocked"
        and unavailable["error_kind"] == "physical_memory_unavailable",
        "unavailable physical-memory command fails closed",
    )
