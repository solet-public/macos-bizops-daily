"""The postgres major a target actually runs is compared against the pinned one.

Detection coverage for ``iss_9d6b61ba`` (D-E1).

``bootstrap.ensure_postgres`` reaches its ABSENT branch and runs ``brew install
postgresql`` -- UNVERSIONED -- while both layers require major
``_SUPPORTED_POSTGRES_MAJOR``, and it returns ``installed_and_started`` without
re-probing the major it just installed.  Its sibling ``ensure_pgvector`` re-probes
and raises when the post-install probe disagrees.  ``probe_postgres`` already owns
a ``RUNNING_WRONG_VERSION`` state but only ever applies it to a PRE-EXISTING
install, never to its own.  So the repair path can report green while a different
major than the pinned one is what is running; this smoke pins the comparison that
catches it at doctor time.

The discriminator is deliberately not "the check went warn".  A check that warned
on every target would pass a warn-only assertion while being useless, so each red
asserts its NAMED ``reason_code``, and the failure shapes produce DIFFERENT names:

* a major that is not the pinned one -> postgres_major_unpinned
* a server that cannot be queried       -> postgres_version_unreadable
* a server version number that will not parse -> postgres_version_unparseable

Offline: constructed ``SHOW server_version_num`` output only. Nothing here runs
psql, touches a database, or reads a real target. The one file it does read is the
repo-root ``bootstrap.py``, and only to pin a single integer literal against the
census's own copy -- noted here because the shipped-smoke register filter keys on
the smoke file, not on its data dependencies.
"""

from __future__ import annotations

import re
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from solet_manager.doctor_postgres_pin_census import (  # noqa: E402
    _REQUIRED_POSTGRES_MAJOR,
    collect_postgres_pin_advisories,
)

_PIN_ID = "doctor::postgres_major_pin_v1"

_CHECKS = 0


def _check(condition: bool, message: str) -> None:
    global _CHECKS
    _CHECKS += 1
    if not condition:
        raise AssertionError(f"red: {message}")


class _Record:
    """The only field the postgres pin census reads off a registry record."""

    name = "census"
    target = "/nonexistent/census-target"


def _reader(code: int, stdout: str, stderr: str = "") -> Callable[[], tuple[int, str, str]]:
    def _read() -> tuple[int, str, str]:
        return code, stdout, stderr

    return _read


def _advisory(code: int, stdout: str, stderr: str = "") -> dict[str, object]:
    reader = _reader(code, stdout, stderr)
    results = collect_postgres_pin_advisories(_Record(), version_reader=reader)
    _check(len(results) == 1, f"the census stopped emitting exactly one check: {len(results)}")
    entry = results[0]
    assert isinstance(entry, dict)
    _check(str(entry["check_id"]) == _PIN_ID, f"unexpected check id: {entry['check_id']}")
    return entry


def _assert_pinned_major_is_green() -> None:
    advisory = _advisory(0, f"{_REQUIRED_POSTGRES_MAJOR}0004\n")
    _check(
        advisory["status"] == "verified",
        f"the pinned major was not verified: {advisory['status']}",
    )
    _check(
        advisory["blocking"] is False,
        "a postgres pin advisory must never block a doctor result",
    )
    observed = advisory["observed"]
    assert isinstance(observed, dict)
    _check(
        observed["postgres_major"] == _REQUIRED_POSTGRES_MAJOR,
        f"the observed major was not recorded: {observed['postgres_major']}",
    )


def _assert_unpinned_major_is_named() -> None:
    """The exact residue: Homebrew's unversioned formula supplied another major."""

    other = _REQUIRED_POSTGRES_MAJOR + 1
    advisory = _advisory(0, f"{other}0000\n")
    _check(
        advisory["reason_code"] == "postgres_major_unpinned",
        f"an unpinned major was not named: {advisory['reason_code']}",
    )
    observed = advisory["observed"]
    assert isinstance(observed, dict)
    _check(
        observed["postgres_major"] == other,
        f"the wrong major was not identified: {observed['postgres_major']}",
    )
    expected = advisory["expected"]
    assert isinstance(expected, dict)
    _check(
        expected["postgres_major"] == _REQUIRED_POSTGRES_MAJOR,
        "the advisory must state the major it expected",
    )


def _assert_older_major_is_also_named() -> None:
    """The divergence is two-sided: an older major is as unpinned as a newer one."""

    advisory = _advisory(0, f"{_REQUIRED_POSTGRES_MAJOR - 1}0009\n")
    _check(
        advisory["reason_code"] == "postgres_major_unpinned",
        f"an older major was not named: {advisory['reason_code']}",
    )


def _assert_unreadable_server_is_unknown_not_green() -> None:
    """An unreadable server probe is not a passing probe."""

    advisory = _advisory(2, "", "psql: connection to server failed")
    _check(
        advisory["status"] == "unknown",
        f"an unreadable server did not read as unknown: {advisory['status']}",
    )
    _check(
        advisory["reason_code"] == "postgres_version_unreadable",
        f"an unreadable server was not named: {advisory['reason_code']}",
    )


def _assert_unparseable_version_is_its_own_name() -> None:
    advisory = _advisory(0, "unknown-build\n")
    _check(
        advisory["status"] == "unknown",
        f"an unparseable version did not read as unknown: {advisory['status']}",
    )
    _check(
        advisory["reason_code"] == "postgres_version_unparseable",
        f"an unparseable version was not named: {advisory['reason_code']}",
    )


def _assert_default_reader_queries_the_running_server() -> None:
    """The default reader must not mistake a client binary for the server."""

    commands: list[list[str]] = []

    def _run(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        return subprocess.CompletedProcess(command, 0, f"{_REQUIRED_POSTGRES_MAJOR}0004\n", "")

    with patch("solet_manager.doctor_postgres_pin_census.subprocess.run", _run):
        entries = collect_postgres_pin_advisories(_Record())

    _check(len(entries) == 1, f"the default reader emitted {len(entries)} checks")
    entry = entries[0]
    assert isinstance(entry, dict)
    _check(entry["status"] == "verified", "the queried pinned server was not verified")
    _check(
        commands
        == [
            [
                "psql",
                "--no-psqlrc",
                "--tuples-only",
                "--no-align",
                "--command",
                "SHOW server_version_num",
            ]
        ],
        f"doctor did not issue the runtime server-version query: {commands}",
    )


def _assert_required_major_matches_bootstrap() -> None:
    """The census's pin and the installer's pin must be the same integer.

    Two independently-declared copies of one constant is exactly the shape that
    drifts silently, so the drift is a red here rather than a disagreement
    discovered on a target.
    """

    bootstrap = Path(__file__).resolve().parents[2] / "bootstrap.py"
    _check(bootstrap.is_file(), f"bootstrap.py not found at {bootstrap}")
    text = bootstrap.read_text(encoding="utf-8")
    match = re.search(r"^_SUPPORTED_POSTGRES_MAJOR\s*=\s*(\d+)", text, re.MULTILINE)
    _check(match is not None, "bootstrap.py no longer declares _SUPPORTED_POSTGRES_MAJOR")
    assert match is not None
    _check(
        int(match.group(1)) == _REQUIRED_POSTGRES_MAJOR,
        f"pin drift: bootstrap says {match.group(1)}, census says {_REQUIRED_POSTGRES_MAJOR}",
    )


def main() -> int:
    _assert_pinned_major_is_green()
    _assert_unpinned_major_is_named()
    _assert_older_major_is_also_named()
    _assert_unreadable_server_is_unknown_not_green()
    _assert_unparseable_version_is_its_own_name()
    _assert_default_reader_queries_the_running_server()
    _assert_required_major_matches_bootstrap()
    print(f"doctor_postgres_pin_census_smoke OK: {_CHECKS} checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
