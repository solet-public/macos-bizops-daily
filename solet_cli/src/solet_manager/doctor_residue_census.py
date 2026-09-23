"""Host-side residue advisories: state left behind that no writer corrects.

The vintage census in :mod:`doctor_residue_census`'s sibling asks whether a
target is *current*.  This module asks a different question: whether the host
carries leftovers that every writer in the system believes are somebody else's
to remove.  Those leftovers are readable, they are not transient, and until
now nothing named them -- a target could be fully green while a released
loopback port sat advertised in a file, or half a release tree sat abandoned.

Each check names its specific cause.  A generic "something is wrong" red is
worth nothing to the operator who has to act on it, so every ``warn`` here
carries the reason code and the repair for exactly one defect.

These are advisories: they report, they never refuse doctor.
"""

from __future__ import annotations

import os
import socket
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import cast

from .doctor_check_results import unknown, verified, warn
from .models import InstanceRecord, JsonValue

# ``~/.ananta/runtime`` and ``~/.ananta/releases`` are the conventions owned by
# macos_self_deployment_plugin (service_install.RUNTIME_DIR and
# release_manager.RELEASES_ROOT_DEFAULT).  The manager deliberately does not
# import that plugin -- doctor must run against a host whose deployment plugin
# is absent or broken, which is exactly when residue accumulates -- so the
# conventions are restated here and both roots are injectable for tests.
RUNTIME_DIRECTORY_DEFAULT: str = "~/.ananta/runtime"
RELEASES_ROOT_DEFAULT: str = "~/.ananta/releases"

# release_manager.STAGING_SUFFIX.  A staging directory is renamed onto its
# final name by ``os.replace``; one still wearing this suffix was never
# finalized.
STAGING_SUFFIX: str = ".incoming"

_INGRESS_PORT_SUFFIX: str = ".mcp_ingress.port"
_ROUTER_PORT_SUFFIX: str = ".router.port"
_BRIDGE_PORT_SUFFIX: str = ".bridge.port"

_CONNECT_TIMEOUT_SECONDS: float = 1.0

type ConnectProbe = Callable[[int], bool]

# ``None`` means the query itself failed, which is reported as ``unknown``
# rather than being folded into "absent" -- the absent-reads-as-false collapse
# is precisely the defect class these advisories exist to name.
type RouterPresenceProbe = Callable[[str], bool | None]


def collect_residue_advisories(
    record: InstanceRecord,
    *,
    runtime_directory: Path | None = None,
    releases_root: Path | None = None,
    connect_probe: ConnectProbe | None = None,
    router_presence: RouterPresenceProbe | None = None,
) -> list[JsonValue]:
    """Return report-only checks over host residue for one managed instance."""

    runtime = (
        Path(RUNTIME_DIRECTORY_DEFAULT).expanduser()
        if runtime_directory is None
        else runtime_directory
    )
    releases = Path(RELEASES_ROOT_DEFAULT).expanduser() if releases_root is None else releases_root
    probe = _loopback_accepts if connect_probe is None else connect_probe
    presence = _router_launchagent_present if router_presence is None else router_presence
    return [
        _stale_ingress_port_advisory(record, runtime, probe),
        _abandoned_release_staging_advisory(releases),
        _orphaned_router_port_advisory(record, runtime, presence),
    ]


def _stale_ingress_port_advisory(
    record: InstanceRecord,
    runtime: Path,
    probe: ConnectProbe,
) -> dict[str, JsonValue]:
    """Detect an ``<name>.mcp_ingress.port`` file whose port nobody is serving.

    The ingress writes this file and never unlinks it, and the stale-runtime
    cleanup that scrubs ``rest.port`` and ``bridge.port`` does not know it
    exists.  Between ingress death and supervisor restart the file advertises a
    released ephemeral loopback port that any local process may bind -- and the
    tunnel supervisor compares recorded values rather than liveness, so it
    forwards externally-originated MCP traffic there.
    """

    check_id = "doctor::stale_ingress_port_file_v1"
    path = runtime / f"{record.name}{_INGRESS_PORT_SUFFIX}"
    expected: dict[str, JsonValue] = {
        "ingress_port_file_absent_or_served": True,
    }
    if not path.exists():
        return verified(
            check_id,
            "No MCP ingress port file is present, so none can advertise a released port.",
            expected,
            {"ingress_port_file": None, "port": None, "accepts_connections": None},
            str(path),
        )
    port = _read_port(path)
    if port is None:
        return unknown(
            check_id,
            "The MCP ingress port file exists but does not hold a usable port.",
            expected,
            {"ingress_port_file": str(path), "port": None, "accepts_connections": None},
            str(path),
            "ingress_port_file_unreadable",
            "The file could not be read as a TCP port in 1..65535.",
        )
    accepts = probe(port)
    observed: dict[str, JsonValue] = {
        "ingress_port_file": str(path),
        "port": port,
        "accepts_connections": accepts,
    }
    if accepts:
        return verified(
            check_id,
            "The MCP ingress port file names a port that is accepting connections.",
            expected,
            observed,
            str(path),
        )
    return warn(
        check_id,
        "The MCP ingress port file names a port that refuses connections, so it is stale.",
        expected,
        observed,
        str(path),
        "ingress_port_file_stale",
        (
            "Remove the stale ingress port file; nothing unlinks it on ingress exit and "
            "the stale-runtime cleanup does not cover it. While it names a released "
            "loopback port, any local process that binds that port receives "
            "externally-originated MCP traffic."
        ),
    )


def _abandoned_release_staging_advisory(releases: Path) -> dict[str, JsonValue]:
    """Detect ``*.incoming`` release staging directories that were never finalized.

    Garbage collection walks finalized releases only, and the staging
    directories are excluded from that list by construction, so a build that
    fails between staging and the atomic rename strands its tree permanently.
    Each one is a full release tree, so this is measured in gigabytes rather
    than stray files.
    """

    check_id = "doctor::abandoned_release_staging_v1"
    expected: dict[str, JsonValue] = {"abandoned_staging_directory_count": 0}
    if not releases.is_dir():
        return verified(
            check_id,
            "No release root is present, so no staging directory can be abandoned.",
            expected,
            {"releases_root": str(releases), "abandoned": [], "abandoned_count": 0},
            str(releases),
        )
    try:
        abandoned = sorted(
            entry.name
            for entry in releases.iterdir()
            if entry.is_dir() and entry.name.endswith(STAGING_SUFFIX)
        )
    except OSError as exc:
        return unknown(
            check_id,
            "The release root could not be listed, so abandoned staging state is unknown.",
            expected,
            {"releases_root": str(releases), "abandoned": [], "abandoned_count": None},
            str(releases),
            "releases_root_unreadable",
            str(exc),
        )
    observed: dict[str, JsonValue] = {
        "releases_root": str(releases),
        "abandoned": [cast(JsonValue, name) for name in abandoned],
        "abandoned_count": len(abandoned),
    }
    if not abandoned:
        return verified(
            check_id,
            "No abandoned release staging directories are present.",
            expected,
            observed,
            str(releases),
        )
    return warn(
        check_id,
        "Release staging directories were never finalized and no reaper will remove them.",
        expected,
        observed,
        str(releases),
        "abandoned_release_staging",
        (
            "Remove the listed .incoming directories after confirming no build is in "
            "flight. Release garbage collection walks finalized releases only and "
            "excludes staging directories by construction, so these are not "
            "reclaimed by any existing path."
        ),
    )


def _orphaned_router_port_advisory(
    record: InstanceRecord,
    runtime: Path,
    presence: RouterPresenceProbe,
) -> dict[str, JsonValue]:
    """Detect router-owned port files left behind after the router is gone.

    ``uninstall_router`` removes both files today, so this advisory is a
    regression guard and a detector for files stranded by the earlier
    single-file uninstall -- not a live code defect.  It stays useful because
    the files outlive the writer that was fixed: a host uninstalled before that
    fix still carries them, and a crash between bootout and unlink still
    strands them.

    Router presence is measured, never assumed.  Defaulting it to "installed"
    would make the ``warn`` branch unreachable on every real host, which is a
    check that cannot fail rather than a check that passes.
    """

    check_id = "doctor::orphaned_router_port_files_v1"
    expected: dict[str, JsonValue] = {"router_owned_port_files_when_router_absent": 0}
    candidates = (
        runtime / f"{record.name}{_ROUTER_PORT_SUFFIX}",
        runtime / f"{record.name}{_BRIDGE_PORT_SUFFIX}",
    )
    present = sorted(str(path) for path in candidates if path.exists())
    router_installed = presence(record.name)
    observed: dict[str, JsonValue] = {
        "router_installed": router_installed,
        "present_port_files": [cast(JsonValue, item) for item in present],
        "present_count": len(present),
    }
    if router_installed is None:
        return unknown(
            check_id,
            "Router presence could not be determined, so orphaned port files are unknown.",
            expected,
            observed,
            str(runtime),
            "router_presence_unknown",
            "The router LaunchAgent could not be queried for this instance.",
        )
    if router_installed:
        return verified(
            check_id,
            "The router is installed, so its port files are expected to be present.",
            expected,
            observed,
            str(runtime),
        )
    if not present:
        return verified(
            check_id,
            "The router is absent and it left no port files behind.",
            expected,
            observed,
            str(runtime),
        )
    return warn(
        check_id,
        "Router-owned port files remain while the router itself is absent.",
        expected,
        observed,
        str(runtime),
        "orphaned_router_port_files",
        (
            "Remove the listed router-owned port files. Uninstall removes both today, "
            "so their presence means either an uninstall predating that fix or an "
            "interruption between bootout and unlink."
        ),
    )


def _read_port(path: Path) -> int | None:
    try:
        raw = path.read_text(encoding="utf-8").strip()
        port = int(raw)
    except (OSError, ValueError):
        return None
    return port if 1 <= port <= 65535 else None


def _loopback_accepts(port: int) -> bool:
    """Return whether something accepts a loopback TCP connection on ``port``."""

    try:
        with socket.create_connection(("127.0.0.1", port), _CONNECT_TIMEOUT_SECONDS):
            return True
    except OSError:
        return False


def _router_launchagent_present(name: str) -> bool | None:
    """Return whether the instance's router LaunchAgent is loaded.

    ``launchctl print`` exits 0 for a loaded label and non-zero for an unknown
    one.  A failure to run it at all is ``None`` -- unknown, not absent.
    """

    label = f"local.solet.{name}.router"
    try:
        completed = subprocess.run(
            ("/bin/launchctl", "print", f"gui/{os.getuid()}/{label}"),
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return completed.returncode == 0
