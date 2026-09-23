#!/usr/bin/env python3
"""REL-06 S6 — honest-return relabel: value pins + grep gate + contract-doc pins.

The delivery vocabulary was renamed to what the code can truthfully prove:
``woke_native_channel`` → ``queued_wake``, ``notified`` → ``queued_notification``.
This smoke is the red-first pin for the rename:

  * the mint-site constants carry the NEW values, and the OLD identifiers are gone
    (revert either → this smoke goes red);
  * a grep gate proves ZERO occurrences of the distinctive old token
    ``woke_native_channel`` anywhere outside ``workbench/`` history (so the old
    vocabulary cannot creep back into code / contract docs);
  * the two role-send process JSONs carry the new values, not the old.

Run:
    SOLET_NAME=<name>-test .venv/bin/python3 \
        plugins/agent_messaging_plugin/tests/rel06_relabel_smoke.py
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))
sys.path.insert(0, str(REPO_ROOT / "plugins" / "agent_messaging_plugin" / "src"))

from agent_messaging_plugin import peer_dispatch  # noqa: E402

_passed = 0
_failed: list[str] = []

_KB = REPO_ROOT / "plugins" / "agent_messaging_plugin" / "knowledge_base"
_OLD_TOKEN = "woke" + "_native_channel"  # split so THIS pin file is not a hit


def _check(condition: object, label: str) -> None:
    global _passed
    if condition:
        _passed += 1
        print(f"  PASS  {label}")
        return
    _failed.append(label)
    print(f"  FAIL  {label}")


def test_mint_site_new_values() -> None:
    _check(
        peer_dispatch.DELIVERY_QUEUED_WAKE == "queued_wake",
        "S6: DELIVERY_QUEUED_WAKE == 'queued_wake'",
    )
    _check(
        peer_dispatch.DELIVERY_QUEUED_NOTIFICATION == "queued_notification",
        "S6: DELIVERY_QUEUED_NOTIFICATION == 'queued_notification'",
    )
    _check(
        peer_dispatch.DELIVERY_QUEUED_FOR_REPLAY == "queued_for_replay",
        "S6: the already-honest queued_for_replay value is unchanged",
    )


def test_old_identifiers_gone() -> None:
    _check(
        not hasattr(peer_dispatch, "DELIVERY_WOKE_NATIVE_CHANNEL"),
        "S6: the old DELIVERY_WOKE_NATIVE_CHANNEL identifier is gone (hard rename)",
    )
    _check(
        not hasattr(peer_dispatch, "DELIVERY_NOTIFIED"),
        "S6: the old DELIVERY_NOTIFIED identifier is gone (hard rename)",
    )
    _check(
        not hasattr(peer_dispatch, "DELIVERY_PERSISTED_SILENT"),
        "A4: the retired DELIVERY_PERSISTED_SILENT identifier is gone — no "
        "delivery outcome skips the queue anymore",
    )


def test_grep_gate_zero_old_token_outside_workbench() -> None:
    result = subprocess.run(  # noqa: S603
        [
            "grep",
            "-rIn",
            _OLD_TOKEN,
            "--include=*.py",
            "--include=*.json",
            "--include=*.md",
            "--exclude-dir=__pycache__",
            "--exclude-dir=.ruff_cache",
            "--exclude-dir=workbench",
            str(REPO_ROOT / "plugins"),
            str(REPO_ROOT / "ananta"),
            str(REPO_ROOT / "knowledge_bases"),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    # This pin file references the token only via a split literal, so it is not a
    # match; filter defensively in case a scanner concatenates it.
    hits = [
        line
        for line in result.stdout.splitlines()
        if line.strip() and "rel06_relabel_smoke" not in line
    ]
    _check(
        result.returncode in (0, 1) and not hits,
        f"S6 grep gate: ZERO '{_OLD_TOKEN}' outside workbench/ (hits: {hits[:3]})",
    )


def test_process_jsons_carry_new_vocabulary() -> None:
    for name in ("send_peer_message.json", "peer_send_by_name.json"):
        content = (_KB / "processes" / name).read_text()
        _check(
            "queued_wake" in content
            and "queued_notification" in content
            and _OLD_TOKEN not in content,
            f"S6: {name} carries the new vocabulary (no old token)",
        )


def main() -> None:
    print("=== REL-06 S6 honest-return relabel smoke ===")
    test_mint_site_new_values()
    test_old_identifiers_gone()
    test_grep_gate_zero_old_token_outside_workbench()
    test_process_jsons_carry_new_vocabulary()
    print(f"\n{_passed} passed, {len(_failed)} failed")
    if _failed:
        for label in _failed:
            print(f"  FAILED: {label}")
        sys.exit(1)


if __name__ == "__main__":
    main()
