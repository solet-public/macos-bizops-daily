"""Passive advisory for secrets exposed on process command lines.

Detection coverage for ``iss_7020ef5b`` (D-7-residuals): the tunnel supervisor
passes ``--control-plane.api-key`` to its child as a LITERAL argv element
(``tunnel_supervisor._child_argv``), and the supervisor itself accepts
``--control-plane-api-key`` on its own command line.  Process arguments are
world-readable on macOS, so any local process can recover the control-plane
credential with ``ps``.  Two exposures, one cause.

This check reports the EXPOSURE, never the SECRET.  It matches on the flag and
records only that a value followed it, plus the owning pid and executable.  The
value itself is never read into the advisory, never logged, and never returned
-- a doctor advisory is written to a result document that gets pasted into
issues and chat, and a check that "helpfully" quoted the credential it found
would be a worse leak than the one it reports.  That property is asserted by
this module's smoke, not merely intended.

Scope limit, stated because it bounds what a green here means: this observes
LIVE processes.  A credential that was exposed on a command line yesterday, or
that is exposed only while a tunnel is briefly up, leaves no residue for a
target at rest, so a ``verified`` result means "nothing is exposing it right
now", not "nothing ever did".
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from typing import Final

from .doctor_advisory_result import advisory_unknown, advisory_verified, advisory_warn
from .models import InstanceRecord, JsonValue

ProcessLister = Callable[[], tuple[int, str, str]]

_ARGV_SECRET_CHECK_ID = "doctor::argv_secret_exposure_v1"

# Both spellings are live: the child takes the dotted form, the supervisor's own
# parser takes the dashed form. Matching only one would miss half the exposure.
_SECRET_FLAGS: Final[tuple[str, ...]] = (
    "--control-plane.api-key",
    "--control-plane-api-key",
)


def collect_secret_exposure_advisories(
    record: InstanceRecord,
    *,
    process_lister: ProcessLister | None = None,
) -> list[JsonValue]:
    """Return report-only checks for credentials visible on live command lines."""

    lister = _run_ps if process_lister is None else process_lister
    return [_argv_secret_exposure_advisory(record, lister)]


def _argv_secret_exposure_advisory(
    record: InstanceRecord,
    lister: ProcessLister,
) -> dict[str, JsonValue]:
    expected: dict[str, JsonValue] = {"argv_exposed_secret_flags": 0}
    observed: dict[str, JsonValue] = {"exposures": [], "instance_name": record.name}
    source = "ps -axww -o pid=,args="

    code, stdout, stderr = lister()
    if code != 0:
        return advisory_unknown(
            _ARGV_SECRET_CHECK_ID,
            "The process list could not be read, so argv credential exposure is unknown.",
            expected,
            observed,
            source,
            "process_list_unreadable",
            stderr.strip()[:200] or f"ps exited {code}",
        )

    exposures = _scan(stdout)
    observed = {"exposures": exposures, "instance_name": record.name}

    if not exposures:
        return advisory_verified(
            _ARGV_SECRET_CHECK_ID,
            "No live process exposes a control-plane credential on its command line.",
            expected,
            observed,
            source,
        )

    return advisory_warn(
        _ARGV_SECRET_CHECK_ID,
        f"{len(exposures)} live process(es) carry a control-plane credential as a "
        "literal command-line argument, where any local process can read it.",
        expected,
        observed,
        source,
        "argv_secret_exposure",
        "Pass the credential through the environment or a mode-0600 file read by "
        "the child, not as an argv element. Rotate it: argv is world-readable, so "
        "treat it as disclosed for as long as it has been running this way.",
    )


def _scan(listing: str) -> list[JsonValue]:
    """Find flag-with-a-value occurrences, recording position rather than value."""

    exposures: list[JsonValue] = []
    for line in listing.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        pid, _, argv = stripped.partition(" ")
        fields = argv.split()
        for flag in _SECRET_FLAGS:
            if not _carries_value(fields, flag):
                continue
            exposures.append(
                {
                    "pid": pid,
                    "flag": flag,
                    # The executable only -- never the remainder of argv, which is
                    # where the credential sits.
                    "executable": fields[0] if fields else None,
                }
            )
    return exposures


def _carries_value(fields: list[str], flag: str) -> bool:
    """True when ``flag`` appears AND something follows it to be the value.

    A bare flag with nothing after it is a malformed command line, not a
    disclosed credential, and reporting it would be a false positive. The
    ``flag=value`` form is also matched, since it exposes the value identically.
    """

    for index, field in enumerate(fields):
        if field == flag and index + 1 < len(fields):
            return True
        prefix = flag + "="
        if field.startswith(prefix) and len(field) > len(prefix):
            return True
    return False


def _run_ps() -> tuple[int, str, str]:
    try:
        result = subprocess.run(
            ("ps", "-axww", "-o", "pid=,args="),
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 125, "", str(exc)
    return result.returncode, result.stdout, result.stderr
