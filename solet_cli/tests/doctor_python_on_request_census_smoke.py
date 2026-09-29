"""Doctor names a solet venv whose ``python@3.13`` is kept only as a dependency.

``iss_d62aeab7`` / ``dec_b08cf4c7``.  On every real install Homebrew records
``python@3.13`` as ``installed_on_request=false``: only the solet keg's dependency
keeps it.  r61 drops that dependency, after which ``brew autoremove`` would delete the
interpreter each solet venv links.  This advisory reports the condition while it is
still harmless and names the exact command that fixes it.

REPORT-ONLY, like the interpreter-drift census beside it.  Each red asserts its NAMED
``reason_code``; each control keeps the check honest:

* the keg receipt says not-on-request             -> ``python_not_installed_on_request`` (warn)
* the keg receipt says on-request (control)       -> verified
* the launcher is not a Homebrew keg (control)    -> verified, not a false alarm
* the receipt is absent or malformed              -> ``python_install_receipt_unreadable`` (unknown)
* the launcher link no longer resolves            -> ``venv_interpreter_missing`` (unknown)
* the target has no ``.venv``                     -> ``instance_venv_absent`` (unknown)

Offline: constructed directory trees under a temporary directory only.  Nothing here runs
Homebrew, and nothing reads the host's real Cellar.
"""

from __future__ import annotations

import json
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from solet_manager.doctor_python_on_request_census import (  # noqa: E402
    collect_python_on_request_advisories,
)

_CHECK_ID = "doctor::python_installed_on_request_v1"
_FIX = "brew tab --installed-on-request python@3.13"
_CELLAR_TAIL = "Frameworks/Python.framework/Versions/3.13/bin/python3.13"

_CHECKS = 0


def _check(condition: bool, message: str) -> None:
    global _CHECKS
    _CHECKS += 1
    if not condition:
        raise AssertionError(f"red: {message}")


class _Record:
    """The only fields the census reads off a registry record."""

    def __init__(self, target: Path) -> None:
        self.name = "census"
        self.target = str(target)


def _cellar_interpreter(root: Path, *, receipt: object | None) -> Path:
    keg = root / "Cellar" / "python@3.13" / "3.13.15"
    binary = keg / _CELLAR_TAIL
    binary.parent.mkdir(parents=True)
    binary.write_bytes(b"\x00")
    binary.chmod(0o755)
    if receipt is not None:
        (keg / "INSTALL_RECEIPT.json").write_text(receipt if isinstance(receipt, str) else json.dumps(receipt), encoding="utf-8")
    return binary


def _venv(target: Path, launcher: Path | None) -> Path:
    venv = target / ".venv"
    (venv / "bin").mkdir(parents=True)
    if launcher is not None:
        (venv / "bin" / "python3.13").symlink_to(launcher)
        (venv / "bin" / "python3").symlink_to("python3.13")
    return venv


def _advisory(target: Path) -> dict[str, object]:
    results = collect_python_on_request_advisories(_Record(target))
    _check(len(results) == 1, f"the census stopped emitting exactly one check: {len(results)}")
    entry = results[0]
    assert isinstance(entry, dict)
    _check(str(entry["check_id"]) == _CHECK_ID, f"unexpected check id: {entry['check_id']}")
    _check(entry["blocking"] is False, "an on-request advisory must never block a doctor result")
    return entry


def _assert_dependency_only_is_named_with_its_fix(root: Path) -> None:
    target = root / "dependency"
    _venv(target, _cellar_interpreter(root / "d", receipt={"installed_on_request": False}))
    advisory = _advisory(target)
    _check(advisory["status"] == "warn", f"a dependency-only python@3.13 was not a warning: {advisory['status']}")
    _check(
        advisory["reason_code"] == "python_not_installed_on_request",
        f"a dependency-only python@3.13 was not named: {advisory['reason_code']}",
    )
    _check(_FIX in str(advisory["repair"]), f"the repair must carry the exact command `{_FIX}`: {advisory['repair']}")
    _check("brew autoremove" in str(advisory["summary"]) + str(advisory["repair"]), "the advisory must name the autoremove hazard")


def _assert_on_request_is_green(root: Path) -> None:
    target = root / "requested"
    _venv(target, _cellar_interpreter(root / "r", receipt={"installed_on_request": True}))
    advisory = _advisory(target)
    _check(advisory["status"] == "verified", f"an on-request python@3.13 was not verified: {advisory['status']}")


def _assert_non_homebrew_launcher_is_not_a_false_alarm(root: Path) -> None:
    target = root / "copied"
    venv = _venv(target, None)
    (venv / "bin" / "python3").write_bytes(b"\x00")
    (venv / "bin" / "python3").chmod(0o755)
    advisory = _advisory(target)
    _check(advisory["status"] == "verified", f"a non-Homebrew launcher raised an alarm: {advisory['status']}")


def _assert_unreadable_receipt_is_unknown_not_green(root: Path) -> None:
    for label, receipt in (("absent", None), ("malformed", "{not json"), ("no_flag", {"poured_from_bottle": True})):
        target = root / f"receipt_{label}"
        _venv(target, _cellar_interpreter(root / f"u_{label}", receipt=receipt))
        advisory = _advisory(target)
        _check(advisory["status"] == "unknown", f"a {label} receipt read as {advisory['status']}")
        _check(
            advisory["reason_code"] == "python_install_receipt_unreadable",
            f"a {label} receipt was not named: {advisory['reason_code']}",
        )


def _assert_dangling_link_is_its_own_name(root: Path) -> None:
    target = root / "dangling"
    _venv(target, root / "gone" / _CELLAR_TAIL)
    advisory = _advisory(target)
    _check(advisory["status"] == "unknown", f"a dangling link read as {advisory['status']}")
    _check(advisory["reason_code"] == "venv_interpreter_missing", f"a dangling link was not named: {advisory['reason_code']}")


def _assert_absent_venv_is_unknown_not_green(root: Path) -> None:
    target = root / "novenv"
    target.mkdir()
    advisory = _advisory(target)
    _check(advisory["status"] == "unknown", f"an absent venv read as {advisory['status']}")
    _check(advisory["reason_code"] == "instance_venv_absent", f"an absent venv was not named: {advisory['reason_code']}")


_ASSERTIONS: tuple[Callable[[Path], None], ...] = (
    _assert_dependency_only_is_named_with_its_fix,
    _assert_on_request_is_green,
    _assert_non_homebrew_launcher_is_not_a_false_alarm,
    _assert_unreadable_receipt_is_unknown_not_green,
    _assert_dangling_link_is_its_own_name,
    _assert_absent_venv_is_unknown_not_green,
)


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="python-on-request-census-") as scratch:
        root = Path(scratch)
        for assertion in _ASSERTIONS:
            assertion(root)
    print(f"doctor_python_on_request_census_smoke: {_CHECKS} checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
