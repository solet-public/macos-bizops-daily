"""Passive advisory over the postgres major version a target actually runs.

Detection coverage for the layer-0 postgres row of the 2026-09-02 doctor-detection
audit.  REPORT-ONLY, for the reason the vintage census gives: the running
postgres major is host state, not part of the target's pinned completion
contract, so a divergence here must be named loudly without retroactively
refusing an installation whose own contract verified.

Row detected
------------
``iss_9d6b61ba`` (D-E1) -- layer-0 postgres repair is UNPINNED while its
acceptance is PINNED.  ``bootstrap.ensure_postgres`` reaches its ABSENT branch
and runs ``brew install postgresql`` -- unversioned -- yet both layers require
major ``17`` (``_SUPPORTED_POSTGRES_MAJOR``), and the function returns
``installed_and_started`` immediately WITHOUT re-probing the major it just
installed.  Its sibling ``ensure_pgvector`` does exactly the opposite: it
re-probes after installing and raises when the post-install probe disagrees.

So the repair path can hand back a green "installed and started" while Homebrew's
unversioned formula supplied a different major than the one every later layer
pins.  ``probe_postgres`` already knows how to detect that -- it has a
``RUNNING_WRONG_VERSION`` state -- but only ever applies it to a PRE-EXISTING
install, never to its own.  This advisory applies that same comparison to the
target at doctor time, which is the first point after the unpinned install where
anything looks again.

What this module does NOT claim
-------------------------------
It does not detect the missing re-probe itself -- that the ABSENT branch lacks
the ``state_after`` check its pgvector sibling has is a property of the installer's
control flow, and a scan of source text is the wrong instrument for a behavioural
property.  What is measurable from the target is the CONSEQUENCE: the major that
is actually running versus the major every layer pins.  A target repaired onto
the correct major is graded verified here even though the unpinned install path
is unchanged, and that is deliberate -- this check grades the target, not the
installer.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable

from .doctor_advisory_result import advisory_unknown, advisory_verified, advisory_warn
from .models import InstanceRecord, JsonValue

# ``bootstrap._SUPPORTED_POSTGRES_MAJOR`` is the source of truth.  It is spelled
# here rather than imported because ``bootstrap.py`` is a root-level installer
# script, not a package the manager depends on; the smoke pins the value so a
# drift between the two is a red rather than a silent disagreement.
_REQUIRED_POSTGRES_MAJOR = 17

_PIN_CHECK_ID = "doctor::postgres_major_pin_v1"

_PROBE_TIMEOUT_S = 10

ServerVersionReader = Callable[[], tuple[int, str, str]]


def collect_postgres_pin_advisories(
    record: InstanceRecord,
    *,
    version_reader: ServerVersionReader | None = None,
) -> list[JsonValue]:
    """Return the report-only check for the target's postgres major version."""

    reader = _run_psql_server_version if version_reader is None else version_reader
    return [_postgres_major_pin_advisory(record, reader)]


def _run_psql_server_version() -> tuple[int, str, str]:
    """Return the connected server's ``server_version_num`` through ``psql``."""

    try:
        completed = subprocess.run(
            [
                "psql",
                "--no-psqlrc",
                "--tuples-only",
                "--no-align",
                "--command",
                "SHOW server_version_num",
            ],
            capture_output=True,
            text=True,
            timeout=_PROBE_TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return 1, "", str(exc)
    return completed.returncode, completed.stdout, completed.stderr


def _postgres_major_pin_advisory(
    record: InstanceRecord, reader: ServerVersionReader
) -> dict[str, JsonValue]:
    expected: dict[str, JsonValue] = {
        "postgres_major": _REQUIRED_POSTGRES_MAJOR,
        "instance_name": record.name,
    }
    observed: dict[str, JsonValue] = {
        "postgres_major": None,
        "server_version_num": None,
    }
    source = "psql --no-psqlrc --tuples-only --no-align --command 'SHOW server_version_num'"

    code, stdout, stderr = reader()
    if code != 0:
        return advisory_unknown(
            _PIN_CHECK_ID,
            "The running postgres server could not be queried, so its major version is unknown.",
            expected,
            observed,
            source,
            "postgres_version_unreadable",
            (stderr or stdout).strip() or f"postgres server version query exited {code}",
        )

    server_version_num = stdout.strip()
    if not server_version_num.isdecimal():
        return advisory_unknown(
            _PIN_CHECK_ID,
            "The running postgres server reported a version number this check cannot parse, so the major is unknown.",
            expected,
            observed,
            source,
            "postgres_version_unparseable",
            "An unparseable server version is not a passing version; the major could not be compared.",
        )

    version_num = int(server_version_num)
    major = version_num // 10_000
    observed["server_version_num"] = version_num
    observed["postgres_major"] = major
    if major != _REQUIRED_POSTGRES_MAJOR:
        return advisory_warn(
            _PIN_CHECK_ID,
            "The running postgres major is not the major every layer pins.",
            expected,
            observed,
            source,
            "postgres_major_unpinned",
            (
                "Install and start the pinned major explicitly (postgresql@"
                f"{_REQUIRED_POSTGRES_MAJOR}); the layer-0 repair path installs the "
                "unversioned Homebrew formula and reports success without re-probing "
                "the major it installed, so a green install step does not mean the "
                "pinned major is what is running."
            ),
        )

    return advisory_verified(
        _PIN_CHECK_ID,
        "The running postgres major matches the major every layer pins.",
        expected,
        observed,
        source,
    )
