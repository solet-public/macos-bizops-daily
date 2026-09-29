"""Passive advisory over a solet venv whose Homebrew interpreter moved under it.

Detection coverage for ``iss_d62aeab7`` (macos-bizops issue 73, Part 55.6).
REPORT-ONLY, for the reason the vintage census gives: an interpreter that changed
is host state, not part of the target's pinned completion contract, so it must be
named loudly without retroactively refusing an installation whose own contract
verified.

Row detected
------------
A Homebrew ``python@3.13`` upgrade (which ``brew install`` of a Manager before r61,
or of any other formula that depends on it, performs as an ordinary dependency
upgrade; from r61 the Manager formula no longer depends on it, but a user's own
``brew upgrade`` still can) replaces the ad-hoc-signed interpreter that a solet's ``.venv`` links.  An ad-hoc
signature has no stable signing authority, so the Keychain ACL of every credential
the solet owns pins the OLD binary's code-directory hash.  The next restart fails
each read with ``-25293`` until a human re-authorizes each item.

What this module measures, and what it does not
-----------------------------------------------
The ACL itself cannot be read without a prompt, so this grades the cause that is
visible without executing anything: the ``version`` the venv recorded in
``pyvenv.cfg`` when it was created, against the Cellar version its interpreter
link resolves to now.  A difference means the framework changed under the venv.

It does not see a Homebrew *revision* bump of the same version (``3.13.15`` to
``3.13.15_1``): the venv never recorded the revision, and the keg's ``_N`` suffix
is deliberately ignored so an ordinary keg spelling is not read as drift.  It also
cannot tell that the ACL was re-authorized afterwards, so a warning stays until the
venv is rebuilt.  Recording the interpreter's code-directory hash at each start
would close both gaps; that is a state-recording design change, deliberately not
made here.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from .doctor_advisory_result import advisory_unknown, advisory_verified, advisory_warn
from .models import InstanceRecord, JsonValue

_CHECK_ID = "doctor::python_interpreter_drift_v1"
_SOURCE = "<target>/.venv/pyvenv.cfg version vs realpath(<target>/.venv/bin/python3) Cellar segment"

_KEG_VERSION = re.compile(r"/python@3\.13/(?P<version>[0-9]+\.[0-9]+\.[0-9]+)(?:_[0-9]+)?/")
_CFG_VERSION = re.compile(r"^version\s*=\s*(?P<version>[0-9]+\.[0-9]+\.[0-9]+)\s*$", re.MULTILINE)


def collect_python_interpreter_advisories(record: InstanceRecord) -> list[JsonValue]:
    """Return the report-only check for the target venv's interpreter drift."""

    return [_interpreter_drift_advisory(record)]


def _interpreter_drift_advisory(record: InstanceRecord) -> dict[str, JsonValue]:
    venv = Path(record.target) / ".venv"
    expected: dict[str, JsonValue] = {"instance_name": record.name, "interpreter_matches_venv_record": True}
    observed: dict[str, JsonValue] = {
        "venv_recorded_version": None,
        "resolved_interpreter": None,
        "resolved_keg_version": None,
    }
    if not venv.is_dir():
        return advisory_unknown(
            _CHECK_ID,
            "The target has no .venv, so its interpreter cannot be compared with what the venv recorded.",
            expected,
            observed,
            _SOURCE,
            "instance_venv_absent",
            f"{venv} is not a directory.",
        )
    launcher = venv / "bin" / "python3"
    resolved = Path(os.path.realpath(launcher))
    observed["resolved_interpreter"] = str(resolved)
    if launcher.is_symlink() and not resolved.is_file():
        return advisory_warn(
            _CHECK_ID,
            "The venv's interpreter link no longer resolves to a file.",
            expected,
            observed,
            _SOURCE,
            "venv_interpreter_missing",
            "The interpreter this venv linked was removed (the post-`brew upgrade python@3.13` shape). "
            "Rebuild the venv, then re-authorize each Keychain item the solet owns: read it once in a "
            "logged-in GUI session and answer Always Allow.",
        )
    keg = _KEG_VERSION.search(str(resolved))
    if keg is None:
        return advisory_verified(
            _CHECK_ID,
            "The venv launcher is not a Homebrew python@3.13 link, so a Homebrew upgrade cannot repoint it.",
            expected,
            observed,
            _SOURCE,
        )
    observed["resolved_keg_version"] = keg["version"]
    return _compare_recorded_version(venv, str(resolved), keg["version"], expected, observed)


def _compare_recorded_version(
    venv: Path,
    resolved: str,
    keg_version: str,
    expected: dict[str, JsonValue],
    observed: dict[str, JsonValue],
) -> dict[str, JsonValue]:
    try:
        cfg = (venv / "pyvenv.cfg").read_text(encoding="utf-8")
    except OSError as exc:
        cfg = ""
        detail = str(exc)
    else:
        detail = "pyvenv.cfg carries no `version = X.Y.Z` line."
    match = _CFG_VERSION.search(cfg)
    if match is None:
        return advisory_unknown(
            _CHECK_ID,
            "The venv did not record its interpreter version, so drift cannot be graded.",
            expected,
            observed,
            _SOURCE,
            "venv_python_version_unreadable",
            detail,
        )
    observed["venv_recorded_version"] = match["version"]
    if match["version"] == keg_version:
        return advisory_verified(
            _CHECK_ID,
            "The venv's interpreter is the Homebrew python@3.13 version the venv recorded.",
            expected,
            observed,
            _SOURCE,
        )
    return advisory_warn(
        _CHECK_ID,
        "The Homebrew python@3.13 this venv links is not the version the venv was created under.",
        expected,
        observed,
        _SOURCE,
        "python_framework_changed_since_venv_created",
        (
            f"The venv was created under {match['version']} and now resolves to {keg_version}. Keychain items "
            "authorized to the old binary are refused with -25293 on the next restart. Before restarting, in a "
            f"logged-in GUI session (not SSH), read each Keychain item the solet owns once under {resolved} and "
            "answer Always Allow (plain Allow is asked again on every spawn), then re-run doctor."
        ),
    )
