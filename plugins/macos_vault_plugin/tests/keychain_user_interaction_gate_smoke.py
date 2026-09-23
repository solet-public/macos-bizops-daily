#!/usr/bin/env python3
"""Smoke: credential READS never block on a Keychain prompt; writes still can.

Guards the fix for the 2026-08-31 blue-green spawn hang, where an ACL-refused
``db_password`` read blocked a spawned solet for the swap's full 600 s
``register_timeout`` — silently, because a SecurityAgent dialog was raised for a
background process nobody was watching.

The subsmokes and the mutation each one catches:

* ``SC-1 flips-and-restores`` — dropping the ``set_user_interaction_allowed``
  call inside :func:`user_interaction_disallowed`, or the ``finally`` restore.
* ``SC-2 restores-on-raise`` — moving the restore out of the ``finally``, which
  would let one failed daemon read silently disarm an operator tool later in the
  same process.
* ``SC-3 default-gates-every-read`` — gating only the read that happened to bite
  us and leaving the other three seams promptable. Asserts on all four
  independently, so a partial application cannot pass.
* ``SC-4 operator-opt-in-still-prompts`` — latching the gate process-wide or
  ignoring ``allow_user_interaction``, either of which breaks the operator
  repair path that re-grants the ACL.
* ``SC-5 writes-are-not-gated`` — widening the gate onto ``store_credential``,
  which would stop the seed tool from being able to answer a prompt.

Hermetic: ``keyring`` is replaced with a recorder, so no real Keychain item is
read, written, or deleted. The only OS calls are the process-local
get/set of the user-interaction flag, which touches no stored secret.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

os.environ.setdefault("SOLET_NAME", "smoke_keychain_gate")

from macos_vault_plugin import keychain_interaction  # noqa: E402
from macos_vault_plugin.keychain import SystemKeychain  # noqa: E402

_PLUGIN = "some_plugin"
_CREDENTIAL = "some_credential"


class _Recorder:
    """Stands in for ``keyring``, capturing the prompt policy at call time."""

    def __init__(self) -> None:
        self.reads_allowed_interaction: list[bool] = []
        self.writes_allowed_interaction: list[bool] = []

    def get_password(self, service: str, account: str) -> str | None:
        del service, account
        self.reads_allowed_interaction.append(
            keychain_interaction.user_interaction_allowed(),
        )
        return None

    def set_password(self, service: str, account: str, value: str) -> None:
        del service, account, value
        self.writes_allowed_interaction.append(
            keychain_interaction.user_interaction_allowed(),
        )

    def get_keyring(self) -> object:
        return self


def _install_recorder() -> _Recorder:
    """Swap ``keyring`` for a recorder, leaving ``keyring.backends.fail`` intact."""
    import keyring

    recorder = _Recorder()
    original_get = keyring.get_password
    original_set = keyring.set_password
    keyring.get_password = recorder.get_password  # type: ignore[assignment]
    keyring.set_password = recorder.set_password  # type: ignore[assignment]
    _RESTORE.append(lambda: setattr(keyring, "get_password", original_get))
    _RESTORE.append(lambda: setattr(keyring, "set_password", original_set))
    return recorder


_RESTORE: list[Callable[[], None]] = []


def _sc1_flips_and_restores() -> None:
    keychain_interaction.set_user_interaction_allowed(True)
    inside: list[bool] = []
    with keychain_interaction.user_interaction_disallowed():
        inside.append(keychain_interaction.user_interaction_allowed())
    after = keychain_interaction.user_interaction_allowed()
    assert inside == [False], f"gate did not disallow interaction: {inside}"
    assert after is True, "gate did not restore the prior (allowed) state"


def _sc2_restores_on_raise() -> None:
    keychain_interaction.set_user_interaction_allowed(True)
    sentinel = RuntimeError("read blew up")
    try:
        with keychain_interaction.user_interaction_disallowed():
            raise sentinel
    except RuntimeError as exc:
        assert exc is sentinel, "gate swallowed or replaced the body's exception"
    assert keychain_interaction.user_interaction_allowed() is True, (
        "a raising read left the process unable to prompt — an operator tool "
        "running later in this process would be silently disarmed"
    )


def _sc3_default_gates_every_read(recorder: _Recorder) -> None:
    keychain_interaction.set_user_interaction_allowed(True)
    keychain = SystemKeychain()
    recorder.reads_allowed_interaction.clear()

    reads: dict[str, Callable[[], Any]] = {
        "retrieve": lambda: keychain.retrieve("account"),
        "exists": lambda: keychain.exists("account"),
        "retrieve_credential": lambda: keychain.retrieve_credential(_PLUGIN, _CREDENTIAL),
        "exists_credential": lambda: keychain.exists_credential(_PLUGIN, _CREDENTIAL),
    }
    ungated: list[str] = []
    for name, call in reads.items():
        recorder.reads_allowed_interaction.clear()
        call()
        assert recorder.reads_allowed_interaction, f"{name} never reached keyring"
        if any(recorder.reads_allowed_interaction):
            ungated.append(name)
    assert not ungated, (
        f"credential read(s) can still block on a Keychain prompt: {ungated}. "
        "Every read seam must be gated, not just the one that hung."
    )


def _sc4_operator_opt_in_still_prompts(recorder: _Recorder) -> None:
    keychain_interaction.set_user_interaction_allowed(True)
    keychain = SystemKeychain(allow_user_interaction=True)
    recorder.reads_allowed_interaction.clear()
    keychain.retrieve_credential(_PLUGIN, _CREDENTIAL)
    assert recorder.reads_allowed_interaction == [True], (
        "operator tooling lost the ability to be shown a prompt; answering one "
        "is how a human re-grants an ACL after the signing identity changes"
    )


def _sc5_writes_are_not_gated(recorder: _Recorder) -> None:
    keychain_interaction.set_user_interaction_allowed(True)
    keychain = SystemKeychain()
    recorder.writes_allowed_interaction.clear()
    keychain.store_credential(_PLUGIN, _CREDENTIAL, b"value")
    assert recorder.writes_allowed_interaction == [True], (
        "a credential WRITE was gated; only reads produced the hang, and a "
        "gated write would break the operator seed path"
    )


def _check(name: str, fn: Callable[[], None], failures: list[str]) -> None:
    try:
        fn()
    except Exception as exc:  # noqa: BLE001 — the smoke reports, never propagates
        print(f"  FAIL {name}: {exc}")
        failures.append(name)
    else:
        print(f"  ok   {name}")


def main() -> int:
    if sys.platform != "darwin":
        print("SKIP: macOS-only vault substrate.")
        return 0

    entry_state = keychain_interaction.user_interaction_allowed()
    recorder = _install_recorder()
    failures: list[str] = []
    print("keychain user-interaction gate:")
    try:
        _check("SC-1 flips-and-restores", _sc1_flips_and_restores, failures)
        _check("SC-2 restores-on-raise", _sc2_restores_on_raise, failures)
        _check(
            "SC-3 default-gates-every-read",
            lambda: _sc3_default_gates_every_read(recorder),
            failures,
        )
        _check(
            "SC-4 operator-opt-in-still-prompts",
            lambda: _sc4_operator_opt_in_still_prompts(recorder),
            failures,
        )
        _check(
            "SC-5 writes-are-not-gated",
            lambda: _sc5_writes_are_not_gated(recorder),
            failures,
        )
    finally:
        for restore in _RESTORE:
            restore()
        keychain_interaction.set_user_interaction_allowed(entry_state)

    if failures:
        print(f"\nFAIL — {len(failures)} case(s): {', '.join(failures)}")
        return 1
    print("\nPASS — credential reads fail fast; writes and operator tooling still prompt.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
