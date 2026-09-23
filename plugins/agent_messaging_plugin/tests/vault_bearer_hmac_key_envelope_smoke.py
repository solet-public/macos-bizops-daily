#!/usr/bin/env python3
"""Unit smoke for the vault-read envelope bug (Dax Part 36 §36.2) —
``agent_messaging_plugin.plugin._vault_retrieve_value`` and its caller
``_load_or_create_bearer_hmac_key``.

The bug: ``_load_or_create_bearer_hmac_key`` used to key its hit-branch on
``retrieved.get("status") == "success"`` — a shape ``macos_vault_plugin``
never returns (its real envelope carries ``action_status: "completed"`` for
BOTH a hit and a genuine miss, distinguished by ``data`` shape). The hit
branch never fired, so an existing bearer-HMAC key was never recognized and
the signing secret was re-minted on every boot, invalidating every
outstanding bearer token.

``_FakeVault`` below returns the REAL ``macos_vault_plugin`` ActionResult
envelope shapes byte-for-byte (verified against
``plugins/macos_vault_plugin/src/macos_vault_plugin/plugin.py``'s
``_success``/``_not_found`` methods at the time this smoke was written) —
the adopter's point, and the reason the original bug shipped undetected,
is that a fake returning the IMAGINED ``{"status": "success", ...}`` shape
would have passed a smoke that never exercised the real defect.

Run:
    SOLET_NAME=<name>-test .venv/bin/python3 \
        plugins/agent_messaging_plugin/tests/vault_bearer_hmac_key_envelope_smoke.py
"""

from __future__ import annotations

import base64
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))
sys.path.insert(0, str(REPO_ROOT / "plugins" / "agent_messaging_plugin" / "src"))

from agent_messaging_plugin.plugin import (  # noqa: E402
    _BEARER_HMAC_KEY_VAULT_NAME,
    HMAC_KEY_BYTE_LENGTH,
    VaultEnvelopeError,
    _load_or_create_bearer_hmac_key,
    _vault_retrieve_value,
)

_passed = 0
_failed: list[str] = []


def _check(condition: object, label: str) -> None:
    global _passed
    if condition:
        _passed += 1
        print(f"  PASS  {label}")
    else:
        _failed.append(label)
        print(f"  FAIL  {label}")


class _FakeVault:
    """Real ``macos_vault_plugin`` ActionResult envelope shapes, hand-held
    (not the real plugin instance — see module docstring for why the shape
    fidelity is what matters, not the backing store)."""

    def __init__(self, *, existing: dict[str, str] | None = None) -> None:
        self._store: dict[str, str] = dict(existing or {})
        self.store_calls: list[tuple[str, str]] = []

    def retrieve(self, key: str) -> dict[str, Any]:
        if key in self._store:
            # Mirrors macos_vault_plugin.plugin.MacosVaultPlugin._success()
            # composed with ._retrieve_impl()'s hit branch exactly.
            return {
                "action_status": "completed",
                "timestamp": "2026-08-10T00:00:00+00:00",
                "data": {"key": key, "value": self._store[key]},
                "actions": [],
                "error": None,
            }
        # Mirrors ._not_found() exactly -- action_status is STILL
        # "completed" (a miss is a valid business outcome, not an error).
        return {
            "action_status": "completed",
            "timestamp": "2026-08-10T00:00:00+00:00",
            "data": {"found": False, "key": key, "message": f"Secret '{key}' not found"},
            "actions": [],
            "error": None,
        }

    def store(
        self, key: str, value: str,
        tags: list[str] | None = None, metadata: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        self._store[key] = value
        self.store_calls.append((key, value))
        return {
            "action_status": "completed",
            "timestamp": "2026-08-10T00:00:00+00:00",
            "data": {"key": key, "version": 1, "message": "Secret stored"},
            "actions": [],
            "error": None,
        }


class _ImaginedStatusVault:
    """The envelope shape the ORIGINAL bug assumed -- a top-level
    ``"status": "success"`` key the real vault never sends. Used to prove
    the malformed-envelope leg raises loud rather than reading as a miss."""

    def retrieve(self, key: str) -> dict[str, Any]:
        del key
        return {"status": "success", "data": {"value": "should-never-be-read"}}

    def store(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        raise AssertionError("store() must never be called on a malformed-envelope leg")


class _VaultErrorEnvelope:
    """A REAL vault error (e.g. keychain unavailable) -- action_status is
    'error', not 'completed'. Must raise, never read as a miss (that would
    silently re-mint the signing secret on a transient vault outage)."""

    def retrieve(self, key: str) -> dict[str, Any]:
        del key
        return {
            "action_status": "error",
            "timestamp": "2026-08-10T00:00:00+00:00",
            "data": {},
            "actions": [],
            "error": {
                "type": "VaultError", "code": "ENCRYPTION_FAILED",
                "message": "Keychain unavailable", "details": {},
                "severity": "error", "timestamp": "2026-08-10T00:00:00+00:00",
            },
        }

    def store(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        raise AssertionError("store() must never be called on a malformed-envelope leg")


def test_vault_retrieve_value_hit() -> None:
    vault = _FakeVault(existing={"k": "the-value"})
    _check(
        _vault_retrieve_value(vault, "k") == "the-value",
        "a well-formed hit (action_status='completed', data.value present) returns the value",
    )


def test_vault_retrieve_value_miss() -> None:
    vault = _FakeVault()
    _check(
        _vault_retrieve_value(vault, "missing-key") is None,
        "a well-formed miss (action_status='completed', data.found=False) returns None",
    )


def test_vault_retrieve_value_raises_on_imagined_status_envelope() -> None:
    raised = False
    try:
        _vault_retrieve_value(_ImaginedStatusVault(), "k")
    except VaultEnvelopeError:
        raised = True
    _check(
        raised,
        "the imagined {'status': 'success'} shape (no action_status key) is "
        "NOT a recognized completed envelope -- raises loud, never reads as a hit or a miss",
    )


def test_vault_retrieve_value_raises_on_real_vault_error() -> None:
    raised = False
    try:
        _vault_retrieve_value(_VaultErrorEnvelope(), "k")
    except VaultEnvelopeError:
        raised = True
    _check(
        raised,
        "action_status='error' (a real vault error, e.g. keychain unavailable) "
        "raises loud -- never silently reads as 'key absent'",
    )


def test_leg_a_existing_key_recognized_no_remint() -> None:
    """Leg (a): the exact regression. Fails if the recognition check
    reverts from ``action_status == 'completed'`` to the old imagined
    ``status == 'success'`` -- the real envelope carries no top-level
    'status' key at all, so the hit would never be recognized and this
    leg's ``store_calls == []`` assertion would fail (a re-mint fires)."""
    fresh_bytes = b"\x01" * HMAC_KEY_BYTE_LENGTH
    stored_b64 = base64.b64encode(fresh_bytes).decode("ascii")
    vault = _FakeVault(existing={_BEARER_HMAC_KEY_VAULT_NAME: stored_b64})
    returned = _load_or_create_bearer_hmac_key(vault)
    _check(returned == fresh_bytes, "the existing stored key is decoded and returned verbatim")
    _check(vault.store_calls == [], "an existing key is recognized -- store() is NEVER called")


def test_leg_b_genuine_miss_mints_and_stores() -> None:
    """Leg (b): first boot, no entry yet -- mint + store exactly once."""
    vault = _FakeVault()
    returned = _load_or_create_bearer_hmac_key(vault)
    _check(len(returned) == HMAC_KEY_BYTE_LENGTH, "a freshly minted key has the declared byte length")
    _check(len(vault.store_calls) == 1, "a genuine miss mints and stores exactly once")
    stored_key, stored_value = vault.store_calls[0]
    _check(stored_key == _BEARER_HMAC_KEY_VAULT_NAME, "stores under the declared scoped vault key")
    _check(
        base64.b64decode(stored_value) == returned,
        "the stored (base64) value decodes back to exactly the bytes returned",
    )


def test_leg_c_malformed_envelope_raises_no_mint() -> None:
    """Leg (c): a malformed/imagined-shape envelope is an ERROR, not a
    miss -- must raise loud and never call store()."""
    raised = False
    try:
        _load_or_create_bearer_hmac_key(_ImaginedStatusVault())
    except VaultEnvelopeError:
        raised = True
    _check(raised, "a malformed envelope propagates as VaultEnvelopeError, not a silent mint")


def main() -> int:
    test_vault_retrieve_value_hit()
    test_vault_retrieve_value_miss()
    test_vault_retrieve_value_raises_on_imagined_status_envelope()
    test_vault_retrieve_value_raises_on_real_vault_error()
    test_leg_a_existing_key_recognized_no_remint()
    test_leg_b_genuine_miss_mints_and_stores()
    test_leg_c_malformed_envelope_raises_no_mint()

    print()
    print(f"PASSED: {_passed}")
    print(f"FAILED: {len(_failed)}")
    for label in _failed:
        print(f"  - {label}")
    return 1 if _failed else 0


if __name__ == "__main__":
    sys.exit(main())
