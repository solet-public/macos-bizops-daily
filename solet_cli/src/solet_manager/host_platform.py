"""The measured host platform an update's host-gated closure pieces are decided on (iss_6d26db73, rul_385dac24).

Queried, never inferred: ``/usr/bin/sw_vers -productVersion`` for the macOS
version and ``/usr/bin/uname -m`` for the machine, each bounded.  A probe that
fails or answers in an unexpected shape raises :class:`HostPlatformError`; the
caller blocks the plan rather than guessing which pieces the host can take.
The threshold a release applies lives in its transition bundle's
``host_profiles``, not here.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass

__all__ = ["HostPlatform", "HostPlatformError", "read_host_platform"]

_VERSION = re.compile(r"^(\d+)(?:\.\d+){0,2}$")
_MACHINE = re.compile(r"^[a-z0-9_]{1,32}$")
_TIMEOUT_SECONDS = 5


class HostPlatformError(RuntimeError):
    """The host platform could not be measured."""


@dataclass(frozen=True, slots=True)
class HostPlatform:
    product_version: str
    machine: str

    def __post_init__(self) -> None:
        if _VERSION.fullmatch(self.product_version) is None:
            raise HostPlatformError(f"macOS product version is not a version: {self.product_version!r}")
        if _MACHINE.fullmatch(self.machine) is None:
            raise HostPlatformError(f"machine is not a machine name: {self.machine!r}")

    @property
    def macos_major(self) -> int:
        return int(self.product_version.split(".", 1)[0])


def read_host_platform() -> HostPlatform:
    """Measure this host; raises :class:`HostPlatformError` instead of answering from a guess."""
    return HostPlatform(_probe(("/usr/bin/sw_vers", "-productVersion")), _probe(("/usr/bin/uname", "-m")))


def _probe(argv: tuple[str, ...]) -> str:
    try:
        completed = subprocess.run(argv, capture_output=True, text=True, timeout=_TIMEOUT_SECONDS, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise HostPlatformError(f"{argv[0]} could not run: {exc}") from exc
    if completed.returncode != 0:
        raise HostPlatformError(f"{argv[0]} exited {completed.returncode}: {completed.stderr.strip()[:200]}")
    return completed.stdout.strip()
