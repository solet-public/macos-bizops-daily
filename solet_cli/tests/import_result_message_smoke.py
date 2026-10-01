"""r65 fix: an import result's message agrees with its own status (iss_6a27a24b).

``ImportEnrollmentResult.to_command_result`` said "Existing Solet import is not yet enabled for target mutation." for
every status, including the ``imported`` and ``already_managed`` results it returns after import wrote its Manager
record.  Each status now says what happened, and the two messages differ.
"""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(_ROOT / "solet_cli" / "src"), str(_ROOT / "solet_setup_contracts" / "src")]

from solet_manager.import_enrollment import ImportEnrollmentResult, ImportPreview, ImportRequest  # noqa: E402
from solet_manager.paths import ManagerPaths  # noqa: E402

_CHECKS = 0
_STALE = "not yet enabled"
# Every already_managed path ends verified (finalized early return, v1_match tail, resumed journal) but two of them write.
ALREADY_MANAGED_MESSAGE = "Existing Solet is already managed by the Manager; its enrollment is recorded and verified."


def _check(condition: object, label: str) -> None:
    global _CHECKS
    _CHECKS += 1
    if not condition:
        raise AssertionError(label)


def _preview() -> ImportPreview:
    root = Path("/nonexistent/import_result_message_smoke")
    request = ImportRequest("fixture", root / "target", "stable", ManagerPaths(root / "config", root / "state", root / "cache"))
    return ImportPreview(request, "sha256:" + "a" * 64, {}, "ins_fixture", "op_fixture", "key_fixture", object())


def main() -> int:
    results = {status: ImportEnrollmentResult(_preview(), status).to_command_result() for status in ("imported", "already_managed")}
    for status, result in results.items():
        _check(result.status == status, f"{status}: the result keeps its status: {result.status}")
        _check(_STALE not in result.message.lower(), f"{status}: the message no longer says import is not enabled: {result.message}")
    _check(results["imported"].message != results["already_managed"].message, "the two statuses say different things")
    _check(results["already_managed"].message == ALREADY_MANAGED_MESSAGE, f"already_managed says the enrollment is recorded and verified: {results['already_managed'].message}")
    _check("no change" not in results["already_managed"].message, "already_managed never claims import changed nothing: two of its three paths write Manager state")
    _check("enrolled" in results["imported"].message, f"imported says the instance is enrolled: {results['imported'].message}")
    try:
        ImportEnrollmentResult(_preview(), "surprise").to_command_result()
    except KeyError:
        _check(True, "an unknown status fails fast")
    else:
        raise AssertionError("an unknown status must fail fast, never get a message")
    print(f"import_result_message_smoke OK: {_CHECKS} checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
