"""Slice 3 smoke: invariant I1.A — no hardcoded port bands.

Validates the structural change from Slice 3 of the bridge-port-
routing-and-session-lifecycle design record (dev-checkout workbench —
not part of the shipped tree):

* **``RouterState.register`` accepts any port.** With the old
  ``_port_in_band`` validation deleted, register_color must succeed for
  ports anywhere in the OS ephemeral range, including high ports
  (50000+) that the pre-Slice-3 8101-8198 band would have rejected.
  The spawn-path guarantee from invariant I2 (Slices 2 + 2.5) replaces
  band validation: every spawn path either registers correctly or
  self-SIGTERMs.

* **``port_manager.find_available_port`` uses ``bind(0)``.** Each call
  returns a fresh OS-assigned port; consecutive calls return distinct
  ports (no static counter, no band-iterate-and-test).

* **``port_manager.write_port_file(service_name='bridge', ...)`` is
  rejected.** The fail-fast guard prevents a regression where a
  solet-side component overwrites the router's canonical
  ``<name>.bridge.port`` file. Same guard on ``remove_port_file``.

Runs entirely in-process — no router subprocess, no solet spawn.
The dispatch's heavier end-to-end scenarios (``apply_manifest`` cycle,
multi-solet collision) are deferred to Slice 6 per the
operator-locked C+ sequencing.

No ``pytest``; runs directly via
``.venv/bin/python3 plugins/macos_self_deployment_plugin/tests/blue_green_router/slice3_no_hardcoded_bands_smoke.py``
and exits 0 on success, 1 on any failure with stderr detail.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from ananta.core.runtime.port_manager import (  # noqa: E402
    find_available_port,
    read_port_file,
    remove_port_file,
    write_port_file,
)
from macos_self_deployment_plugin.blue_green_router.router_state import RouterState  # noqa: E402

# Deliberately above the old blue/green bands (8101-8198) — the smoke
# fails immediately if a future change reintroduces port_in_band
# validation against those ranges.
_HIGH_BLUE_PORT = 54001
_HIGH_GREEN_PORT = 54002


def _expect(condition: bool, message: str) -> None:
    if not condition:
        print(f"FAIL: {message}", file=sys.stderr)
        sys.exit(1)
    print(f"  OK  {message}")


def _scenario_router_state_accepts_any_port() -> None:
    print("Scenario 1: RouterState.register accepts ports outside old 8101-8198 bands")
    state = RouterState()

    result = state.register(
        port=_HIGH_BLUE_PORT, color="blue", instance_id="test-blue-1",
    )
    _expect(
        result.accepted,
        f"register blue@{_HIGH_BLUE_PORT} accepted (reason={result.reason!r})",
    )
    _expect(
        state.bindings["test-blue-1"].port == _HIGH_BLUE_PORT,
        f"binding records port={_HIGH_BLUE_PORT}",
    )

    result = state.register(
        port=_HIGH_GREEN_PORT, color="green", instance_id="test-green-1",
    )
    _expect(
        result.accepted,
        f"register green@{_HIGH_GREEN_PORT} accepted (reason={result.reason!r})",
    )

    # Cross-band: blue port that the old band check would have rejected
    # as out-of-blue-band (8101-8149). 50000 is far outside.
    result = state.register(
        port=50000, color="blue", instance_id="test-blue-cross",
    )
    _expect(
        result.accepted,
        "register blue@50000 accepted (proves no _port_in_band guard)",
    )

    # Unknown color still rejected — that's invariant, not band.
    result = state.register(
        port=_HIGH_BLUE_PORT, color="purple", instance_id="test-purple",
    )
    _expect(
        not result.accepted and result.reason == "unknown_color",
        f"unknown color still rejected (reason={result.reason!r})",
    )


def _scenario_find_available_port_uses_bind_zero() -> None:
    print("Scenario 2: find_available_port returns distinct OS-assigned ports")
    port_a = find_available_port()
    port_b = find_available_port()
    _expect(
        port_a > 0 and port_b > 0,
        f"both ports valid (a={port_a}, b={port_b})",
    )
    _expect(
        port_a != port_b,
        f"consecutive calls return distinct ports (a={port_a}, b={port_b})",
    )

    # Preferred fast-path: when preferred is available, it wins.
    preferred = find_available_port()
    confirmed = find_available_port(preferred=preferred)
    _expect(
        confirmed == preferred,
        f"available preferred port returned verbatim ({preferred})",
    )


def _scenario_bridge_port_file_writes_forbidden() -> None:
    print("Scenario 3: write_port_file / remove_port_file refuse service_name='bridge'")
    try:
        write_port_file(8765, "bridge", "example")
    except ValueError as exc:
        _expect(
            "forbidden" in str(exc).lower(),
            f"write_port_file raises ValueError with 'forbidden' (msg={exc})",
        )
    else:
        print("FAIL: write_port_file('bridge', ...) did NOT raise", file=sys.stderr)
        sys.exit(1)

    try:
        remove_port_file("bridge", "example")
    except ValueError as exc:
        _expect(
            "forbidden" in str(exc).lower(),
            f"remove_port_file raises ValueError with 'forbidden' (msg={exc})",
        )
    else:
        print("FAIL: remove_port_file('bridge', ...) did NOT raise", file=sys.stderr)
        sys.exit(1)


def _scenario_port_file_round_trip_for_non_bridge() -> None:
    print("Scenario 4: non-bridge port file round-trip uses canonical name only")
    # Hijack the runtime dir to a tmpfs so the smoke doesn't pollute the
    # operator's real runtime state.
    with tempfile.TemporaryDirectory(prefix="slice3_smoke_") as tmp:
        prior_xdg = os.environ.get("XDG_RUNTIME_DIR")
        os.environ["XDG_RUNTIME_DIR"] = tmp
        try:
            written = write_port_file(9876, "rest", "example")
            _expect(
                written.name == "example.rest.port",
                f"non-bridge file uses canonical name (got {written.name!r})",
            )
            _expect(
                read_port_file("rest", "example") == 9876,  # noqa: PLR2004
                "read_port_file round-trips",
            )
            _expect(
                remove_port_file("rest", "example"),
                "remove_port_file returns True when file exists",
            )
            _expect(
                read_port_file("rest", "example") is None,
                "read_port_file returns None after remove",
            )

            # Color env var must NOT affect the resolved path — Slice 3
            # eliminated the color-aware branch.
            prior_color = os.environ.get("SOLET_COLOR")
            os.environ["SOLET_COLOR"] = "blue"
            try:
                written_with_color = write_port_file(9877, "rest", "example")
                _expect(
                    written_with_color.name == "example.rest.port",
                    "SOLET_COLOR=blue does NOT yield a per-color path "
                    f"(got {written_with_color.name!r})",
                )
            finally:
                if prior_color is None:
                    os.environ.pop("SOLET_COLOR", None)
                else:
                    os.environ["SOLET_COLOR"] = prior_color
        finally:
            if prior_xdg is None:
                os.environ.pop("XDG_RUNTIME_DIR", None)
            else:
                os.environ["XDG_RUNTIME_DIR"] = prior_xdg


def main() -> int:
    print("Slice 3 smoke: no hardcoded port bands + bridge-file write guard\n")
    _scenario_router_state_accepts_any_port()
    print()
    _scenario_find_available_port_uses_bind_zero()
    print()
    _scenario_bridge_port_file_writes_forbidden()
    print()
    _scenario_port_file_round_trip_for_non_bridge()
    print("\nAll scenarios passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
