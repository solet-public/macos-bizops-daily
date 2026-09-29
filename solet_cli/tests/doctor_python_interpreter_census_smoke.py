"""A solet venv whose Homebrew interpreter moved since the venv was made is named at doctor time.

Detection coverage for ``iss_d62aeab7`` (macos-bizops issue 73, Part 55.6).

Before r61, ``brew install solet-public/tap/solet`` upgraded the shared ``python@3.13``
as an ordinary formula dependency; from r61 the Manager formula no longer depends on it,
but a user's own ``brew upgrade`` or any other dependent formula still can.  A co-located solet whose ``.venv`` links that
framework then resolves to a different ad-hoc-signed binary.  The Keychain ACL of
every credential that solet owns pins the OLD binary's code-directory hash, so the
next restart fails with ``-25293`` until each item is re-authorized by hand.

The doctor cannot read an ACL without a prompt, so it grades the cause it can see
without executing anything: the ``version`` the venv recorded in ``pyvenv.cfg``
when it was created, against the Cellar version its interpreter link resolves to
now.  The discriminator is not "the check went warn": each red asserts its NAMED
``reason_code``, and each failure shape carries a DIFFERENT name:

* the resolved Cellar version differs from the recorded one -> python_framework_changed_since_venv_created
* the interpreter link no longer resolves to a file          -> venv_interpreter_missing
* ``pyvenv.cfg`` is absent or has no version                 -> venv_python_version_unreadable
* the target has no ``.venv``                                -> instance_venv_absent

Two controls keep the check honest: a venv whose recorded and resolved versions
agree is ``verified``, and a venv whose launcher is a copied-in real file (nothing
Homebrew can repoint) is ``verified`` rather than a false alarm.

Offline: constructed directory trees under a temporary directory only.  Nothing
here runs Homebrew, codesign or python, and nothing touches a Keychain.
"""

from __future__ import annotations

import sys
import tempfile
from collections.abc import Callable
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from solet_manager.doctor_python_interpreter_census import (  # noqa: E402
    collect_python_interpreter_advisories,
)

_CHECK_ID = "doctor::python_interpreter_drift_v1"
_CELLAR_TAIL = "Frameworks/Python.framework/Versions/3.13/bin/python3.13"

_CHECKS = 0


def _check(condition: bool, message: str) -> None:
    global _CHECKS
    _CHECKS += 1
    if not condition:
        raise AssertionError(f"red: {message}")


class _Record:
    """The only fields the interpreter census reads off a registry record."""

    def __init__(self, target: Path) -> None:
        self.name = "census"
        self.target = str(target)


def _cellar_interpreter(root: Path, keg: str) -> Path:
    binary = root / "Cellar" / "python@3.13" / keg / _CELLAR_TAIL
    binary.parent.mkdir(parents=True)
    binary.write_bytes(b"\x00")
    binary.chmod(0o755)
    return binary


def _venv(target: Path, *, recorded: str | None, launcher: Path | None) -> Path:
    venv = target / ".venv"
    (venv / "bin").mkdir(parents=True)
    if recorded is not None:
        (venv / "pyvenv.cfg").write_text(
            f"home = /opt/homebrew/opt/python@3.13/bin\nversion = {recorded}\n",
            encoding="utf-8",
        )
    if launcher is not None:
        (venv / "bin" / "python3.13").symlink_to(launcher)
        (venv / "bin" / "python3").symlink_to("python3.13")
    return venv


def _advisory(target: Path) -> dict[str, object]:
    results = collect_python_interpreter_advisories(_Record(target))
    _check(len(results) == 1, f"the census stopped emitting exactly one check: {len(results)}")
    entry = results[0]
    assert isinstance(entry, dict)
    _check(str(entry["check_id"]) == _CHECK_ID, f"unexpected check id: {entry['check_id']}")
    _check(entry["blocking"] is False, "an interpreter advisory must never block a doctor result")
    return entry


def _assert_matching_version_is_green(root: Path) -> None:
    target = root / "matching"
    _venv(target, recorded="3.13.15", launcher=_cellar_interpreter(root / "m", "3.13.15"))
    advisory = _advisory(target)
    _check(advisory["status"] == "verified", f"a matching venv was not verified: {advisory['status']}")


def _assert_revision_suffix_is_not_drift(root: Path) -> None:
    """``3.13.13_1`` is the keg spelling of the same 3.13.13 the venv recorded."""

    target = root / "revision"
    _venv(target, recorded="3.13.13", launcher=_cellar_interpreter(root / "r", "3.13.13_1"))
    advisory = _advisory(target)
    _check(advisory["status"] == "verified", f"a keg revision suffix read as drift: {advisory['status']}")


def _assert_upgraded_framework_is_named(root: Path) -> None:
    """The exact residue of the issue: the venv was made under 3.13.13, brew moved it to 3.13.15."""

    target = root / "drifted"
    _venv(target, recorded="3.13.13", launcher=_cellar_interpreter(root / "d", "3.13.15"))
    advisory = _advisory(target)
    _check(advisory["status"] == "warn", f"an upgraded framework was not a warning: {advisory['status']}")
    _check(
        advisory["reason_code"] == "python_framework_changed_since_venv_created",
        f"an upgraded framework was not named: {advisory['reason_code']}",
    )
    observed = advisory["observed"]
    assert isinstance(observed, dict)
    _check(observed["venv_recorded_version"] == "3.13.13", f"recorded version lost: {observed}")
    _check(observed["resolved_keg_version"] == "3.13.15", f"resolved version lost: {observed}")
    repair = str(advisory["repair"])
    _check("-25293" in repair, "the repair must name the failure the operator will see")
    _check("Always Allow" in repair, "the repair must name the exact ACL answer that fixes it")
    _check(str(observed["resolved_interpreter"]) in repair, "the repair must name the binary to authorize")


def _assert_dangling_link_is_its_own_name(root: Path) -> None:
    target = root / "dangling"
    _venv(target, recorded="3.13.13", launcher=root / "gone" / _CELLAR_TAIL)
    advisory = _advisory(target)
    _check(
        advisory["reason_code"] == "venv_interpreter_missing",
        f"a dangling interpreter link was not named: {advisory['reason_code']}",
    )


def _assert_copied_in_launcher_is_not_a_false_alarm(root: Path) -> None:
    target = root / "copied"
    venv = _venv(target, recorded="3.13.13", launcher=None)
    (venv / "bin" / "python3").write_bytes(b"\x00")
    (venv / "bin" / "python3").chmod(0o755)
    advisory = _advisory(target)
    _check(
        advisory["status"] == "verified",
        f"a copied-in launcher (nothing brew can repoint) raised an alarm: {advisory['status']}",
    )


def _assert_missing_version_is_unknown_not_green(root: Path) -> None:
    target = root / "noversion"
    _venv(target, recorded=None, launcher=_cellar_interpreter(root / "n", "3.13.15"))
    advisory = _advisory(target)
    _check(advisory["status"] == "unknown", f"an unreadable venv version read as {advisory['status']}")
    _check(
        advisory["reason_code"] == "venv_python_version_unreadable",
        f"an unreadable venv version was not named: {advisory['reason_code']}",
    )


def _assert_absent_venv_is_unknown_not_green(root: Path) -> None:
    target = root / "novenv"
    target.mkdir()
    advisory = _advisory(target)
    _check(advisory["status"] == "unknown", f"an absent venv read as {advisory['status']}")
    _check(
        advisory["reason_code"] == "instance_venv_absent",
        f"an absent venv was not named: {advisory['reason_code']}",
    )


_ASSERTIONS: tuple[Callable[[Path], None], ...] = (
    _assert_matching_version_is_green,
    _assert_revision_suffix_is_not_drift,
    _assert_upgraded_framework_is_named,
    _assert_dangling_link_is_its_own_name,
    _assert_copied_in_launcher_is_not_a_false_alarm,
    _assert_missing_version_is_unknown_not_green,
    _assert_absent_venv_is_unknown_not_green,
)


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="python-interpreter-census-") as scratch:
        root = Path(scratch)
        for assertion in _ASSERTIONS:
            assertion(root)
    print(f"doctor_python_interpreter_census_smoke: {_CHECKS} checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
