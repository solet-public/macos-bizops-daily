"""A control-plane credential on a live command line is reported, never quoted.

Detection coverage for ``iss_7020ef5b`` (D-7-residuals).  The tunnel supervisor
passes ``--control-plane.api-key`` to its child as a literal argv element and
accepts ``--control-plane-api-key`` on its own, so the credential is readable by
any local process via ``ps``.

Two properties are pinned here, and the second is the one that matters most:

1. The exposure is DETECTED, and a bare flag with no value is not a false
   positive.
2. The advisory NEVER contains the secret.  A doctor advisory is written into a
   result document that gets pasted into issues and chat.  A check that quoted
   the credential it found would leak it further than the defect it reports --
   turning a local-only ``ps`` exposure into a durable, shareable one.
   :func:`_assert_secret_value_never_appears` fails on any such regression.

Offline: constructed ``ps`` output only.  No process is listed, started, or
signalled.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from solet_manager.doctor_secret_exposure_census import (  # noqa: E402
    collect_secret_exposure_advisories,
)

_CHECKS = 0
_SECRET = "cp-live-9d41f0aa7e2b4c6f"


def _check(condition: bool, message: str) -> None:
    global _CHECKS
    _CHECKS += 1
    if not condition:
        raise AssertionError(f"red: {message}")


class _Record:
    name = "exposure"


def _advisory(listing: str, *, code: int = 0, stderr: str = "") -> dict[str, object]:
    results = collect_secret_exposure_advisories(
        _Record(), process_lister=lambda: (code, listing, stderr)
    )
    _check(len(results) == 1, f"expected exactly one advisory, got {len(results)}")
    advisory = results[0]
    assert isinstance(advisory, dict)
    return advisory


_CLEAN = """  501 /usr/bin/ssh-agent -l
  733 /opt/homebrew/bin/python3 -m ananta.cli --app-home /Users/x/profile
"""

_CHILD_EXPOSED = (
    "  501 /usr/bin/ssh-agent -l\n"
    "  902 /opt/tunnel/tunnel-client run --control-plane.tunnel-id t-1"
    f" --control-plane.api-key {_SECRET}"
    " --mcp.server-url url=http://127.0.0.1:9\n"
)

_SUPERVISOR_EXPOSED = (
    "  911 /opt/homebrew/bin/python3 -m tunnel_supervisor"
    f" --control-plane-api-key {_SECRET} --solet exposure\n"
)

_EQUALS_FORM = f"""  912 /opt/tunnel/tunnel-client run --control-plane.api-key={_SECRET}
"""

_BARE_FLAG = """  913 /opt/tunnel/tunnel-client run --control-plane.api-key
"""


def _assert_clean_host_is_green() -> None:
    advisory = _advisory(_CLEAN)
    _check(
        advisory["status"] == "verified",
        f"a host with no exposure was warned about: {advisory['status']}",
    )


def _assert_child_argv_exposure_is_named() -> None:
    advisory = _advisory(_CHILD_EXPOSED)
    _check(
        advisory["reason_code"] == "argv_secret_exposure",
        f"a child-argv credential exposure was not NAMED: {advisory['reason_code']}",
    )
    exposures = advisory["observed"]["exposures"]
    assert isinstance(exposures, list)
    _check(len(exposures) == 1, f"expected one exposure, got {len(exposures)}")
    entry = exposures[0]
    assert isinstance(entry, dict)
    _check(entry["pid"] == "902", f"the owning pid was not recorded: {entry['pid']}")
    _check(
        entry["flag"] == "--control-plane.api-key",
        f"the matched flag was not recorded: {entry['flag']}",
    )


def _assert_supervisors_own_argv_is_also_caught() -> None:
    """The dashed spelling is a second, distinct exposure site -- one cause."""

    advisory = _advisory(_SUPERVISOR_EXPOSED)
    exposures = advisory["observed"]["exposures"]
    assert isinstance(exposures, list)
    _check(
        len(exposures) == 1 and isinstance(exposures[0], dict),
        "the supervisor's own dashed-flag exposure was missed",
    )
    entry = exposures[0]
    assert isinstance(entry, dict)
    _check(
        entry["flag"] == "--control-plane-api-key",
        f"the dashed spelling was not matched: {entry['flag']}",
    )


def _assert_equals_form_is_caught() -> None:
    advisory = _advisory(_EQUALS_FORM)
    _check(
        advisory["reason_code"] == "argv_secret_exposure",
        "the flag=value form was not detected as an exposure",
    )


def _assert_bare_flag_is_not_a_false_positive() -> None:
    """A flag with nothing after it discloses nothing."""

    advisory = _advisory(_BARE_FLAG)
    _check(
        advisory["status"] == "verified",
        f"a valueless flag was reported as a disclosed credential: {advisory['status']}",
    )


def _assert_unreadable_process_list_is_unknown_not_green() -> None:
    advisory = _advisory("", code=1, stderr="ps: operation not permitted")
    _check(
        advisory["status"] == "unknown",
        f"an unreadable process list did not read as unknown: {advisory['status']}",
    )
    _check(
        advisory["reason_code"] == "process_list_unreadable",
        f"the unreadable process list was not NAMED: {advisory['reason_code']}",
    )


def _assert_secret_value_never_appears() -> None:
    """The advisory must report the exposure without reproducing the secret.

    Serialized whole, because the leak could hide anywhere in the structure --
    a summary, a repair string, an observed field, a nested list entry.
    """

    for listing in (_CHILD_EXPOSED, _SUPERVISOR_EXPOSED, _EQUALS_FORM):
        advisory = _advisory(listing)
        serialized = json.dumps(advisory)
        _check(
            _SECRET not in serialized,
            "the advisory REPRODUCED the credential it was reporting; that turns a "
            "local ps exposure into a durable one in every document this result "
            "is pasted into",
        )


def main() -> int:
    _assert_clean_host_is_green()
    _assert_child_argv_exposure_is_named()
    _assert_supervisors_own_argv_is_also_caught()
    _assert_equals_form_is_caught()
    _assert_bare_flag_is_not_a_false_positive()
    _assert_unreadable_process_list_is_unknown_not_green()
    _assert_secret_value_never_appears()
    print(f"doctor_secret_exposure_census_smoke OK: {_CHECKS} checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
