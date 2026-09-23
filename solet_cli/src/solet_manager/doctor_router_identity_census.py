"""Passive advisory comparing the live router against the record's expectations.

Detection coverage for the router-identity row of the 2026-09-02 doctor-detection
audit.  REPORT-ONLY, for the reason the vintage census gives: which router is
answering is host state, not part of the target's pinned completion contract, so
a divergence must be named loudly without retroactively refusing an installation
whose own contract verified.

Row detected
------------
``iss_c30be917`` (D-7.4) -- ``_verify_router_up`` returns on ANY status reply that
contains ``router_started_at`` (``install_router.py``), with no comparison of pid,
port, or chosen identity.  So the installer accepts an INCUMBENT router that was
already listening at that socket: install reports success over a foreign router,
publishes a port nothing uses (the watchdog converges back to the incumbent), and
the new launchd job crash-loops under ``KeepAlive``.  ``router_mgmt``'s
``_reclaim_socket_path`` correctly REFUSES inside the incoming router, but the
installer never consumes that verdict.

The comparison this advisory makes was already possible and simply never made.
``InstanceRecord`` has carried ``expected_router_name``, ``expected_router_socket``
and ``expected_router_port_range`` since create time
(``create_execution.py``), and a whole-tree read finds them WRITTEN, serialised in
``models.py`` and listed in ``registry.py`` -- and compared against a live router
nowhere.  They are a recorded expectation nothing reads back, which is the shape
that eventually lies.  This check reads them back.

The discriminator is the published port.  ``_dispatch_status`` already returns a
``port`` per colour binding, so a router serving a DIFFERENT solet publishes ports
outside this record's ``expected_router_port_range``.  That is the same predicate
the row's own proposed fix names -- "installer requires port == chosen" -- applied
at doctor time instead of install time.

What this module does NOT claim
-------------------------------
It does not compare pid or the router's own bound management port, because the
status payload carries neither today; adding them is the row's install-time fix
and is not this check's business.  It also does not grade a record whose router
expectations were never recorded -- an older record carries ``None`` there, and a
record that cannot be compared is reported ``unknown``, never ``verified``.
"""

from __future__ import annotations

import json
import socket as socket_mod
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .doctor_advisory_result import advisory_unknown, advisory_verified, advisory_warn
from .models import InstanceRecord, JsonValue

_IDENTITY_CHECK_ID = "doctor::router_identity_matches_record_v1"

_STATUS_TIMEOUT_S = 2.0

StatusReader = Callable[[Path], dict[str, Any]]


def collect_router_identity_advisories(
    record: InstanceRecord,
    *,
    status_reader: StatusReader | None = None,
) -> list[JsonValue]:
    """Return the report-only check comparing the live router to the record."""

    reader = _read_router_status if status_reader is None else status_reader
    return [_router_identity_advisory(record, reader)]


def _read_router_status(socket_path: Path) -> dict[str, Any]:
    """Ask the router management socket for its status payload."""

    with socket_mod.socket(socket_mod.AF_UNIX, socket_mod.SOCK_STREAM) as sock:
        sock.settimeout(_STATUS_TIMEOUT_S)
        sock.connect(str(socket_path))
        sock.sendall(json.dumps({"verb": "status", "args": {}}).encode() + b"\n")
        buf = bytearray()
        while b"\n" not in buf:
            chunk = sock.recv(4096)
            if not chunk:
                break
            buf.extend(chunk)
    value = json.loads(bytes(buf).decode())
    if not isinstance(value, dict):
        raise ValueError(f"router status is {type(value).__name__}, not an object")
    return value


def _parse_port_range(raw: str) -> tuple[int, int]:
    """Parse ``"8800-8999"`` into an inclusive ``(low, high)`` pair."""

    low_text, _, high_text = raw.partition("-")
    return int(low_text.strip()), int(high_text.strip())


def _published_ports(status: dict[str, Any]) -> list[int]:
    """Return every port the router reports for a colour binding."""

    colors = status.get("colors")
    if not isinstance(colors, list):
        return []
    ports: list[int] = []
    for entry in colors:
        if isinstance(entry, dict) and isinstance(entry.get("port"), int):
            ports.append(entry["port"])
    return ports


def _router_identity_advisory(record: InstanceRecord, reader: StatusReader) -> dict[str, JsonValue]:
    expected: dict[str, JsonValue] = {
        "router_socket": record.expected_router_socket,
        "router_port_range": record.expected_router_port_range,
        "router_name": record.expected_router_name,
    }
    observed: dict[str, JsonValue] = {"published_ports": [], "active_instance_id": None}
    source = str(record.expected_router_socket)

    if record.expected_router_socket is None or record.expected_router_port_range is None:
        return advisory_unknown(
            _IDENTITY_CHECK_ID,
            "This record carries no router expectation, so the live router cannot be compared.",
            expected,
            observed,
            source,
            "router_expectation_unrecorded",
            "The record predates expected_router_socket/expected_router_port_range; an "
            "ungradable record is not a passing one.",
        )

    try:
        low, high = _parse_port_range(record.expected_router_port_range)
    except ValueError as exc:
        return advisory_unknown(
            _IDENTITY_CHECK_ID,
            "The recorded router port range could not be parsed, so no port can be graded.",
            expected,
            observed,
            source,
            "router_port_range_unparseable",
            str(exc),
        )

    try:
        status = reader(Path(record.expected_router_socket))
    except (OSError, ValueError) as exc:
        return advisory_unknown(
            _IDENTITY_CHECK_ID,
            "The router management socket could not be read, so its identity is unknown.",
            expected,
            observed,
            source,
            "router_status_unreadable",
            str(exc),
        )

    active_instance = status.get("active_instance_id")
    observed["active_instance_id"] = active_instance if isinstance(active_instance, str) else None

    return _grade_published_ports(status, low, high, expected, observed, source)


def _grade_published_ports(
    status: dict[str, Any],
    low: int,
    high: int,
    expected: dict[str, JsonValue],
    observed: dict[str, JsonValue],
    source: str,
) -> dict[str, JsonValue]:
    """Compare every published colour port against the recorded range.

    Split out of :func:`_router_identity_advisory` to keep both functions inside
    the radon-cc gate's A/B band; that gate's allowlist is tracked debt, not a
    skip path, so new code earns its rank rather than registering an exemption.
    """

    ports = _published_ports(status)
    published: list[JsonValue] = [*ports]
    foreign: list[JsonValue] = [port for port in ports if not low <= port <= high]
    observed["published_ports"] = published
    observed["ports_outside_expected_range"] = foreign

    if foreign:
        return advisory_warn(
            _IDENTITY_CHECK_ID,
            "The router answering at this record's socket publishes ports outside the range "
            "this install chose, which is what an incumbent foreign router looks like.",
            expected,
            observed,
            source,
            "router_port_outside_expected_range",
            "Confirm which router owns this socket before trusting the install: the installer "
            "accepts any status reply carrying router_started_at, so a pre-existing router at "
            "this path is reported as a successful install while the new launchd job crash-loops.",
        )

    if not ports:
        return advisory_verified(
            _IDENTITY_CHECK_ID,
            "The router reports no colour bindings yet, so no published port contradicts the "
            "record; the socket answered and nothing disagrees.",
            expected,
            observed,
            source,
        )

    return advisory_verified(
        _IDENTITY_CHECK_ID,
        "Every port the live router publishes falls inside the range this record chose.",
        expected,
        observed,
        source,
    )
