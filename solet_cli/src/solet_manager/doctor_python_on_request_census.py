"""Passive advisory over a solet venv whose Homebrew ``python@3.13`` is kept only as a dependency.

Detection coverage for ``iss_d62aeab7`` / ``dec_b08cf4c7``.  REPORT-ONLY, for the reason
the interpreter-drift census gives: the state is host state, not part of the target's
pinned completion contract, so it is named loudly without refusing an installation whose
own contract verified.

Row detected
------------
On a real install Homebrew records ``python@3.13`` with ``installed_on_request=false``
because the Manager formula's dependency, not the operator, pulled it in.  While the
formula still depends on it that is harmless.  Once a later release drops the dependency,
``brew autoremove`` (which ``brew upgrade`` runs in its periodic cleanup) reports the
interpreter unneeded and deletes it from under every solet ``.venv`` that links it.

What this module measures
-------------------------
The keg's ``INSTALL_RECEIPT.json`` ``installed_on_request`` flag, for the keg the venv's
interpreter link resolves into.  Nothing is executed.  ``solet-manager create`` and
``update`` mark the flag themselves; this names the case where they have not run yet.

A venv whose launcher is not a Homebrew ``python@3.13`` keg is ``verified``: nothing here
can autoremove it.  An unreadable receipt is ``unknown``, never green.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

from .doctor_advisory_result import advisory_unknown, advisory_verified, advisory_warn
from .models import InstanceRecord, JsonValue

_CHECK_ID = "doctor::python_installed_on_request_v1"
_SOURCE = "<target>/.venv/bin/python3 realpath -> Cellar/python@3.13/<version>/INSTALL_RECEIPT.json installed_on_request"
_FIX = "brew tab --installed-on-request python@3.13"

_KEG = re.compile(r"^(?P<keg>.+/Cellar/python@3\.13/[^/]+)/")


def collect_python_on_request_advisories(record: InstanceRecord) -> list[JsonValue]:
    """Return the report-only check for the target venv's python@3.13 install-on-request flag."""

    return [_on_request_advisory(record)]


def _on_request_advisory(record: InstanceRecord) -> dict[str, JsonValue]:
    venv = Path(record.target) / ".venv"
    expected: dict[str, JsonValue] = {"instance_name": record.name, "python_installed_on_request": True}
    observed: dict[str, JsonValue] = {"resolved_interpreter": None, "installed_on_request": None}
    if not venv.is_dir():
        return advisory_unknown(
            _CHECK_ID,
            "The target has no .venv, so the Homebrew install state of its interpreter cannot be graded.",
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
        return advisory_unknown(
            _CHECK_ID,
            "The venv's interpreter link no longer resolves to a file, so its Homebrew install state cannot be read.",
            expected,
            observed,
            _SOURCE,
            "venv_interpreter_missing",
            "The interpreter this venv linked was removed; the interpreter-drift check names the rebuild.",
        )
    keg = _KEG.match(str(resolved))
    if keg is None:
        return advisory_verified(
            _CHECK_ID,
            "The venv launcher is not a Homebrew python@3.13 link, so `brew autoremove` cannot remove it.",
            expected,
            observed,
            _SOURCE,
        )
    return _grade_receipt(Path(keg["keg"]) / "INSTALL_RECEIPT.json", expected, observed)


def _grade_receipt(
    receipt_path: Path,
    expected: dict[str, JsonValue],
    observed: dict[str, JsonValue],
) -> dict[str, JsonValue]:
    try:
        receipt: object = json.loads(receipt_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return _unreadable(expected, observed, str(exc))
    flag = receipt.get("installed_on_request") if isinstance(receipt, dict) else None
    if not isinstance(flag, bool):
        return _unreadable(expected, observed, "the receipt carries no boolean `installed_on_request`.")
    observed["installed_on_request"] = flag
    if flag:
        return advisory_verified(
            _CHECK_ID,
            "Homebrew keeps python@3.13 on request, so `brew autoremove` will not remove the venv's interpreter.",
            expected,
            observed,
            _SOURCE,
        )
    return advisory_warn(
        _CHECK_ID,
        "python@3.13 is kept only as a dependency, so a later `brew autoremove` can delete the interpreter this venv links.",
        expected,
        observed,
        _SOURCE,
        "python_not_installed_on_request",
        (
            f"Run `{_FIX}`. It changes only Homebrew's receipt flag; it installs and upgrades nothing. "
            "`solet-manager update` and `create` run it for you, so this clears once either has run."
        ),
    )


def _unreadable(
    expected: dict[str, JsonValue],
    observed: dict[str, JsonValue],
    detail: str,
) -> dict[str, JsonValue]:
    return advisory_unknown(
        _CHECK_ID,
        "The Homebrew install receipt for python@3.13 could not be read, so its on-request flag cannot be graded.",
        expected,
        observed,
        _SOURCE,
        "python_install_receipt_unreadable",
        detail,
    )
