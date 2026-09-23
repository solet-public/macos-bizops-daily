#!/usr/bin/env python3
"""RFC 2397 (data URI) per-credential value-encoding smoke (G3).

Verifies the vault Keychain substrate's RFC-2397 encode/decode
(``macos_vault_plugin/keychain.py``):

* **text** values store as readable ``data:text/plain,<percent-encoded>`` and
  round-trip exactly — including the chars most likely to break a naive impl
  (``%``, ``,``, ``;``, ``=``, ``@``, space, non-ASCII).
* a **text value that LOOKS like a binary header**
  (``application/octet-stream;base64,AAAA``) round-trips as text, NOT misparsed
  as binary — locks the first-comma split + header-only mediatype check.
* **binary** (non-utf-8) values store as ``data:application/octet-stream;base64,``
  and round-trip exactly.
* **LEGACY bare-base64** entries (the pre-RFC-2397 form pgvector + the identity
  keypair still use) keep decoding correctly — ZERO migration / backward-compat.
* a simple password stores as literally ``data:text/plain,<password>`` (the
  human-readability goal the operator chose this encoding for).

Part A exercises the pure module helpers (no Keychain). Parts B–D exercise the
REAL ``SystemKeychain`` end-to-end (NO fake-vault — a mock would false-green the
exact base64-vs-raw defect this encoding closes), under a hermetic throwaway
solet + plugin namespace, with cleanup in ``finally``.

Standalone — not pytest. Run with::

    .venv/bin/python3 plugins/macos_vault_plugin/tests/rfc2397_credential_encoding_smoke.py
"""

from __future__ import annotations

# ruff: noqa: E402
import base64
import os
import sys
import traceback
from collections.abc import Callable

# Hermetic test namespace — the real-Keychain parts write under
# ``rfc2397smoke.__rfc2397_smoke__`` and are deleted on the way out, so this
# never touches a live ``solet.*`` / ``smoke.*`` entry. Set BEFORE importing the
# keychain module (SystemKeychain resolves SOLET_NAME eagerly).
os.environ["SOLET_NAME"] = "rfc2397smoke"

from pathlib import Path

_PLUGIN_ROOT = Path(__file__).resolve().parents[1]
_SRC = _PLUGIN_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from macos_vault_plugin.keychain import (  # noqa: E402
    SystemKeychain,
    _decode_credential_value,
    _encode_credential_value,
)

TEST_PLUGIN = "__rfc2397_smoke__"


def _check(name: str, fn: Callable[[], None], failures: list[str]) -> None:
    try:
        fn()
    except Exception:  # noqa: BLE001 — smoke runner: report every failure, keep going
        failures.append(name)
        print(f"  ✗ {name}")
        traceback.print_exc()
    else:
        print(f"  ✓ {name}")


# ── Part A: pure helper round-trips (no Keychain) ────────────────────────────

_TEXT_CASES: list[tuple[str, bytes]] = [
    ("simple", b"hunter2"),
    ("special-chars", "p@ss w,rd;%=&é".encode()),  # % , ; = & space + é
    ("looks-like-binary-header", b"application/octet-stream;base64,AAAA"),
    ("empty", b""),
    ("leading-data-colon-text", b"data:flavored but text"),
]

_BINARY_CASES: list[tuple[str, bytes]] = [
    ("non-utf8-bytes", b"\xff\xfe\x00\x01\x80\x7f"),
    ("nul-and-high", bytes(range(256))),
]


def _helper_text_roundtrips() -> None:
    for label, value in _TEXT_CASES:
        encoded = _encode_credential_value(value)
        assert encoded.startswith("data:text/plain,"), (label, encoded)
        assert "%25" not in encoded or b"%" in value, (label, encoded)
        decoded = _decode_credential_value(encoded)
        assert decoded == value, (label, decoded, value)


def _helper_binary_roundtrips() -> None:
    for label, value in _BINARY_CASES:
        encoded = _encode_credential_value(value)
        assert encoded.startswith("data:application/octet-stream;base64,"), (label, encoded)
        decoded = _decode_credential_value(encoded)
        assert decoded == value, (label, decoded[:16], value[:16])


def _helper_percent_is_encoded() -> None:
    # A literal '%' MUST be percent-encoded (else retrieve mis-reads it).
    encoded = _encode_credential_value(b"50%off")
    assert encoded == "data:text/plain,50%25off", encoded
    assert _decode_credential_value(encoded) == b"50%off"


def _helper_legacy_bare_base64() -> None:
    # Pre-RFC-2397 entries (pgvector, keypair) are bare base64 with no 'data:'
    # prefix — must decode via the legacy branch with zero migration.
    legacy = base64.b64encode(b"legacy-password!").decode("ascii")
    assert not legacy.startswith("data:")  # ':' not in the base64 alphabet
    assert _decode_credential_value(legacy) == b"legacy-password!"


def _helper_malformed_data_uri_fails_loud() -> None:
    try:
        _decode_credential_value("data:text/plain")  # no comma
    except ValueError:
        return
    raise AssertionError("malformed data: URI without a comma must raise ValueError")


# ── Parts B–D: REAL SystemKeychain end-to-end ────────────────────────────────


def _real_substrate_roundtrips(kc: SystemKeychain) -> None:
    for label, value in (*_TEXT_CASES, *_BINARY_CASES):
        acct = f"rt_{label}"
        kc.store_credential(TEST_PLUGIN, acct, value)
        got = kc.retrieve_credential(TEST_PLUGIN, acct)
        assert got == value, (label, got, value)


def _real_text_stored_form_is_readable(kc: SystemKeychain) -> None:
    import keyring

    kc.store_credential(TEST_PLUGIN, "readable", b"Simple-Pass_123")
    raw = keyring.get_password(kc._scoped_service_name(TEST_PLUGIN), "readable")
    assert raw == "data:text/plain,Simple-Pass_123", raw


def _real_legacy_bare_base64_backcompat(kc: SystemKeychain) -> None:
    import keyring

    # Simulate a pre-RFC-2397 entry: write bare base64 directly (the OLD
    # store_credential behavior), then read through the NEW retrieve.
    keyring.set_password(
        kc._scoped_service_name(TEST_PLUGIN),
        "legacy",
        base64.b64encode(b"old-style-secret").decode("ascii"),
    )
    assert kc.retrieve_credential(TEST_PLUGIN, "legacy") == b"old-style-secret"


_REAL_ACCOUNTS = [
    *(f"rt_{label}" for label, _ in (*_TEXT_CASES, *_BINARY_CASES)),
    "readable",
    "legacy",
]


def _cleanup(kc: SystemKeychain) -> None:
    for acct in _REAL_ACCOUNTS:
        try:
            kc.delete_credential(TEST_PLUGIN, acct)
        except Exception:  # noqa: BLE001 — best-effort teardown
            pass


def main() -> int:
    failures: list[str] = []

    print("Part A — pure RFC-2397 helpers:")
    _check("text-roundtrips", _helper_text_roundtrips, failures)
    _check("binary-roundtrips", _helper_binary_roundtrips, failures)
    _check("percent-is-encoded", _helper_percent_is_encoded, failures)
    _check("legacy-bare-base64", _helper_legacy_bare_base64, failures)
    _check("malformed-fails-loud", _helper_malformed_data_uri_fails_loud, failures)

    kc = SystemKeychain()
    if not kc.is_available():
        print("SKIP Parts B–D: no real keyring backend on this host.")
        return 1 if failures else 0

    print("Part B–D — real SystemKeychain:")
    try:
        _check("real-substrate-roundtrips", lambda: _real_substrate_roundtrips(kc), failures)
        _check("real-stored-form-readable", lambda: _real_text_stored_form_is_readable(kc), failures)
        _check("real-legacy-backcompat", lambda: _real_legacy_bare_base64_backcompat(kc), failures)
    finally:
        _cleanup(kc)

    if failures:
        print(f"\nFAIL — {len(failures)} case(s): {', '.join(failures)}")
        return 1
    print("\nPASS — RFC 2397 credential encoding round-trips + backward-compat verified.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
