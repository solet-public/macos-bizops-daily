"""The live router is actually compared against the record's stated expectation.

Detection coverage for ``iss_c30be917`` (D-7.4).

``_verify_router_up`` returns on ANY status reply containing ``router_started_at``
-- no pid, port, or identity comparison -- so an INCUMBENT router already
listening at the socket is accepted as the install's own.  Install reports
success over a foreign router, publishes a port nothing uses, and the new launchd
job crash-loops under KeepAlive.

The comparison was always available and simply never made: ``InstanceRecord``
carries ``expected_router_socket`` and ``expected_router_port_range`` from create
time, and a whole-tree read finds them written, serialised and listed -- and
compared against a live router nowhere.  This smoke pins the read-back.

The discriminator is deliberately not "the check went warn".  Each red asserts its
NAMED ``reason_code``, and the shapes produce DIFFERENT names:

* a published port outside the recorded range -> router_port_outside_expected_range
* a record with no router expectation at all  -> router_expectation_unrecorded
* a range that will not parse                 -> router_port_range_unparseable
* a socket that cannot be read                -> router_status_unreadable

Offline: constructed status payloads only.  Nothing here opens a socket, starts a
router, or reads a real target.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from solet_manager.doctor_router_identity_census import (  # noqa: E402
    collect_router_identity_advisories,
)

_ID = "doctor::router_identity_matches_record_v1"

_CHECKS = 0


def _check(condition: bool, message: str) -> None:
    global _CHECKS
    _CHECKS += 1
    if not condition:
        raise AssertionError(f"red: {message}")


class _Record:
    """A registry record carrying the router expectations create time records."""

    name = "census"
    target = "/nonexistent/census-target"
    expected_router_name = "census"
    expected_router_socket = "/nonexistent/census.router.sock"
    expected_router_port_range = "8800-8999"


def _status(*ports: int, active: str | None = "census-blue") -> dict[str, Any]:
    return {
        "router_started_at": 1.0,
        "active_color": "blue",
        "active_instance_id": active,
        "colors": [
            {"color": "blue", "port": port, "instance_id": active, "status": "live"}
            for port in ports
        ],
    }


def _advisory(record: object, payload: dict[str, Any] | Exception) -> dict[str, object]:
    def _reader(_socket_path: Path) -> dict[str, Any]:
        if isinstance(payload, Exception):
            raise payload
        return payload

    results = collect_router_identity_advisories(record, status_reader=_reader)
    _check(len(results) == 1, f"the census stopped emitting exactly one check: {len(results)}")
    entry = results[0]
    assert isinstance(entry, dict)
    _check(str(entry["check_id"]) == _ID, f"unexpected check id: {entry['check_id']}")
    return entry


def _assert_ports_in_range_are_green() -> None:
    advisory = _advisory(_Record(), _status(8801, 8802))
    _check(
        advisory["status"] == "verified",
        f"in-range ports were not verified: {advisory['status']}",
    )
    _check(
        advisory["blocking"] is False,
        "a router identity advisory must never block a doctor result",
    )


def _assert_foreign_port_is_named() -> None:
    """The residue: a router at our socket publishing a port we never chose."""

    advisory = _advisory(_Record(), _status(9101))
    _check(
        advisory["reason_code"] == "router_port_outside_expected_range",
        f"a foreign published port was not named: {advisory['reason_code']}",
    )
    observed = advisory["observed"]
    assert isinstance(observed, dict)
    _check(
        observed["ports_outside_expected_range"] == [9101],
        f"the foreign port was not identified: {observed['ports_outside_expected_range']}",
    )


def _assert_one_foreign_among_valid_is_still_named() -> None:
    """A single out-of-range binding is enough; it must not be diluted by valid ones."""

    advisory = _advisory(_Record(), _status(8801, 9101))
    _check(
        advisory["reason_code"] == "router_port_outside_expected_range",
        f"a mixed binding set was not named: {advisory['reason_code']}",
    )
    observed = advisory["observed"]
    assert isinstance(observed, dict)
    _check(
        observed["ports_outside_expected_range"] == [9101],
        f"only the foreign port should be listed: {observed['ports_outside_expected_range']}",
    )


def _assert_boundary_ports_are_inclusive() -> None:
    """The recorded range is inclusive at both ends; an off-by-one here warns falsely."""

    advisory = _advisory(_Record(), _status(8800, 8999))
    _check(
        advisory["status"] == "verified",
        f"the range boundaries were not treated as inclusive: {advisory['reason_code']}",
    )


def _assert_no_colors_is_not_a_divergence() -> None:
    advisory = _advisory(_Record(), _status())
    _check(
        advisory["status"] == "verified",
        f"a router with no bindings was warned about: {advisory['reason_code']}",
    )


def _assert_unrecorded_expectation_is_unknown_not_green() -> None:
    """An older record cannot be graded, and ungradable is not passing."""

    class _Old:
        name = "census"
        target = "/nonexistent/census-target"
        expected_router_name = None
        expected_router_socket = None
        expected_router_port_range = None

    advisory = _advisory(_Old(), _status(8801))
    _check(
        advisory["status"] == "unknown",
        f"an unrecorded expectation did not read as unknown: {advisory['status']}",
    )
    _check(
        advisory["reason_code"] == "router_expectation_unrecorded",
        f"an unrecorded expectation was not named: {advisory['reason_code']}",
    )


def _assert_unparseable_range_is_its_own_name() -> None:
    class _Bad:
        name = "census"
        target = "/nonexistent/census-target"
        expected_router_name = "census"
        expected_router_socket = "/nonexistent/census.router.sock"
        expected_router_port_range = "not-a-range"

    advisory = _advisory(_Bad(), _status(8801))
    _check(
        advisory["reason_code"] == "router_port_range_unparseable",
        f"an unparseable range was not named: {advisory['reason_code']}",
    )


def _assert_unreadable_socket_is_unknown_not_green() -> None:
    advisory = _advisory(_Record(), OSError("connection refused"))
    _check(
        advisory["status"] == "unknown",
        f"an unreadable socket did not read as unknown: {advisory['status']}",
    )
    _check(
        advisory["reason_code"] == "router_status_unreadable",
        f"an unreadable socket was not named: {advisory['reason_code']}",
    )


def main() -> int:
    _assert_ports_in_range_are_green()
    _assert_foreign_port_is_named()
    _assert_one_foreign_among_valid_is_still_named()
    _assert_boundary_ports_are_inclusive()
    _assert_no_colors_is_not_a_divergence()
    _assert_unrecorded_expectation_is_unknown_not_green()
    _assert_unparseable_range_is_its_own_name()
    _assert_unreadable_socket_is_unknown_not_green()
    print(f"doctor_router_identity_census_smoke OK: {_CHECKS} checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
