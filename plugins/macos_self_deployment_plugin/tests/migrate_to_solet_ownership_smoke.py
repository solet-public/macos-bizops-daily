#!/usr/bin/env python3
"""LaunchAgent OWNERSHIP smoke for ``deployment/scripts/migrate_to_solet.py``.

The P2 rebrand (2026-08-13) renamed the ``--homunculus`` entry-point flag to
``--solet``. ``migrate_to_solet.py`` knew about that exact flag and would have
rewritten it — but its candidate filter skipped any plist named
``com.openai.*``, to spare the codex-wake toolchain. That skip also caught
``com.openai.tunnel-client.<solet>``: OUR tunnel supervisor, launched from OUR
venv, named after the vendor binary it supervises. It kept the dead flag,
exited 2 on every start, and the ChatGPT connector lost its public ingress for
four days with no symptom but an ABSENCE of traffic (dev-checkout
workbench incident record — not part of the shipped tree).

This smoke pins BOTH directions, because each one alone is satisfied by a
catastrophic implementation:

* positive only — "migrate the vendor-named plist" — is satisfied by migrating
  EVERY plist on the machine, which is the blast radius the skip exists to
  prevent (and which an earlier lane nearly shipped);
* negative only — "leave third-party agents alone" — is satisfied by the
  original defect, i.e. migrating nothing new at all.

It also pins the report/apply symmetry: ``--scan-stale`` was blind to
``~/Library/LaunchAgents`` entirely, so it returned a clean report about the
one surface apply mode edits.

Hermetic: ``LAUNCH_AGENTS``, ``REPO`` and ``CLAUDE_DIR`` are redirected into a
temp tree and ``_is_loaded`` is stubbed, so the real machine is never read or
touched.

Run:
    .venv/bin/python3 \
        plugins/macos_self_deployment_plugin/tests/migrate_to_solet_ownership_smoke.py
"""

from __future__ import annotations

import plistlib
import sys
import tempfile
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[3]
for _p in (
    str(_REPO_ROOT / "plugins" / "macos_self_deployment_plugin" / "src"),
    str(_REPO_ROOT / "ananta" / "src"),
    str(_REPO_ROOT / "deployment" / "scripts"),
):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import migrate_to_solet as mts  # noqa: E402

_passed = 0
_failed: list[str] = []


def _check(condition: bool, label: str) -> None:
    global _passed
    if condition:
        _passed += 1
        print(f"  ok   {label}")
    else:
        _failed.append(label)
        print(f"  FAIL {label}")


def _write_plist(directory: Path, name: str, payload: dict[str, Any]) -> Path:
    path = directory / f"{name}.plist"
    path.write_bytes(plistlib.dumps(payload))
    return path


def _build_fixture(root: Path) -> Path:
    """Four plists: two ours (one vendor-named), two genuinely third-party."""
    agents = root / "LaunchAgents"
    agents.mkdir(parents=True)
    checkout = root / "checkout"
    (checkout / ".venv" / "bin").mkdir(parents=True)

    # The fixture solet name is the neutral sentinel `origin`, NOT this
    # deployment's own name. This file SHIPS in every seed, and the seal's
    # reserved-origin-identity scan fail-closes on the origin token in any
    # shipped byte — a real name here refuses the mint. Third recurrence of
    # that class (01695c228 fixed it, 7ab7004e7 and bf96a24d7 reintroduced
    # it), and a sentinel proves the ownership logic identically.
    # OURS, vendor-NAMED — the exact shape that bit us on 2026-08-15.
    _write_plist(
        agents,
        "com.openai.tunnel-client.origin",
        {
            "Label": "com.openai.tunnel-client.origin",
            "ProgramArguments": [
                f"{checkout}/.venv/bin/python3",
                "-m",
                "macos_self_deployment_plugin.blue_green_router.tunnel_supervisor",
                "--homunculus",
                "origin",
            ],
        },
    )
    # OURS, by the label convention — regression guard for the pre-existing path.
    _write_plist(
        agents,
        "local.homunculus.origin",
        {
            "Label": "local.homunculus.origin",
            "ProgramArguments": [f"{checkout}/.venv/bin/python3", "--homunculus", "origin"],
            "EnvironmentVariables": {"HOMUNCULUS_NAME": "origin"},
        },
    )
    # THIRD-PARTY, and it even mentions the token — the blast-radius guard.
    # Nothing here points at our checkout or ~/.ananta, so it is not ours.
    _write_plist(
        agents,
        "com.adobe.GC.Invoker-1.0",
        {
            "Label": "com.adobe.GC.Invoker-1.0",
            "ProgramArguments": [
                "/Library/Application Support/Adobe/agcinvokerutility",
                "--homunculus",
                "not-ours",
            ],
        },
    )
    # THIRD-PARTY, vendor-named like ours but running the vendor's own binary.
    _write_plist(
        agents,
        "com.openai.codex-wake",
        {
            "Label": "com.openai.codex-wake",
            "ProgramArguments": ["/usr/local/bin/codex", "wake", "--homunculus", "x"],
        },
    )
    return agents


def run_smoke() -> int:
    print("migrate_to_solet_ownership_smoke")
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        agents = _build_fixture(root)
        claude_dir = root / "claude"
        claude_dir.mkdir()

        mts.LAUNCH_AGENTS = agents
        mts.REPO = root / "checkout"
        mts.CLAUDE_DIR = claude_dir
        mts.ANANTA_HOME = root / "ananta"
        mts._is_loaded = lambda _label: False  # never shell out to launchctl

        planned = {plan.old_path.name for plan in mts.plan_plists()}

        # ---- POSITIVE: our vendor-named plist is in remit -------------------
        _check(
            "com.openai.tunnel-client.origin.plist" in planned,
            "owned_vendor_named_plist_is_planned",
        )
        tunnel = [
            p for p in mts.plan_plists()
            if p.old_path.name == "com.openai.tunnel-client.origin.plist"
        ]
        _check(
            bool(tunnel) and tunnel[0].arg_renames == [(3, "--homunculus", "--solet")],
            "owned_vendor_named_plist_rewrites_the_flag_at_the_right_index",
        )
        _check(
            "local.homunculus.origin.plist" in planned,
            "label_convention_plist_still_planned",
        )

        # ---- NEGATIVE: third-party agents stay untouched --------------------
        _check(
            "com.adobe.GC.Invoker-1.0.plist" not in planned,
            "third_party_plist_with_stale_token_is_not_planned",
        )
        _check(
            "com.openai.codex-wake.plist" not in planned,
            "third_party_vendor_binary_plist_is_not_planned",
        )

        # ---- REPORT/APPLY SYMMETRY -----------------------------------------
        scanned = {hit.path.name for hit in mts.scan_stale()}
        _check(
            "com.openai.tunnel-client.origin.plist" in scanned,
            "scan_reports_owned_launch_agent",
        )
        _check(
            "com.adobe.GC.Invoker-1.0.plist" not in scanned,
            "scan_ignores_third_party_launch_agent",
        )

    print(f"\nmigrate_to_solet_ownership_smoke: {_passed} passed, {len(_failed)} failed")
    for label in _failed:
        print(f"  FAILED: {label}")
    return 1 if _failed else 0


if __name__ == "__main__":
    sys.exit(run_smoke())
