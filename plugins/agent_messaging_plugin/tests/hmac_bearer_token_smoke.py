#!/usr/bin/env python3
"""Smoke test for the Task #53 HMAC bearer-token implementation (no pytest).

Exercises ``BearerVerifier`` + the ``_issue_access_token`` mint helper
against the 7 cases from the design doc §7 plus the §8 forged-old-
sealed-claim rejection.

Run:
    .venv/bin/python3 plugins/agent_messaging_plugin/tests/hmac_bearer_token_smoke.py

Project policy: no pytest. Exits 0 on success, 1 on first failure
with a clear label.
"""

from __future__ import annotations

import base64
import hmac as hmac_module
import inspect
import json
import secrets
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))
sys.path.insert(0, str(REPO_ROOT / "plugins" / "agent_messaging_plugin" / "src"))

import jwt  # noqa: E402

from agent_messaging_plugin.mcp_streamable.auth import (  # noqa: E402
    HMAC_KEY_BYTE_LENGTH,
    HMAC_SIGNING_ALGORITHM,
    BearerAuthError,
    BearerVerifier,
)
from agent_messaging_plugin.mcp_streamable.oauth import (  # noqa: E402
    _issue_access_token,
    _oauth_agent_session_id,
)

if TYPE_CHECKING:
    from collections.abc import Callable

_AUDIENCE = "https://abbey0011.example.com/mcp"

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


def _fixed_clock(when: datetime) -> Callable[[], datetime]:
    return lambda: when


def _mint_test_token(
    *,
    hmac_key: bytes,
    client_id: str = "test-client",
    client_name: str = "Test Client",
    resource: str = _AUDIENCE,
    token_ttl_seconds: int = 86_400,
) -> str:
    """Wrapper around ``_issue_access_token`` for test ergonomics."""
    return str(_issue_access_token(
        client_id=client_id,
        client_name=client_name,
        scopes=["mcp:read", "mcp:write"],
        resource=resource,
        hmac_key=hmac_key,
        token_ttl_seconds=token_ttl_seconds,
    ))


def _mint_legacy_sealed_box_token(public_key_b64url: str) -> str:
    """Construct a token in the pre-Task-#53 format.

    Sealed-box ciphertext of a JSON claim, base64url-encoded with
    padding stripped. Mimics what an attacker holding the publicly-
    discoverable X25519 key could mint against the old server. The
    new HMAC verifier MUST reject it.
    """
    from nacl.public import PublicKey, SealedBox

    pub_bytes = base64.urlsafe_b64decode(
        public_key_b64url + "=" * (-len(public_key_b64url) % 4),
    )
    forged_claim = {
        "agent_id": "claude_phone",
        "agent_instance_id": "agi-forged-attacker",
        "issued_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "session_label": "operator-forged",
        "scopes": ["mcp:read", "mcp:write"],
        "aud": _AUDIENCE,
    }
    plaintext = json.dumps(forged_claim, separators=(",", ":")).encode("utf-8")
    ciphertext = SealedBox(PublicKey(pub_bytes)).encrypt(plaintext)
    return base64.urlsafe_b64encode(ciphertext).decode("ascii").rstrip("=")


def _verifier_for(hmac_key: bytes, *, at: datetime | None = None) -> BearerVerifier:
    clock_fn = _fixed_clock(at) if at is not None else None
    return BearerVerifier(
        hmac_key=hmac_key,
        max_age_seconds=300,
        accepted_audiences=(_AUDIENCE,),
        clock=clock_fn,
    )


def _assert_raises_with_code(
    verifier: BearerVerifier, token: str, expected_code: str,
) -> tuple[bool, str]:
    """Verify the token; return (matched, actual_code-or-empty)."""
    try:
        verifier.verify(f"Bearer {token}")
    except BearerAuthError as exc:
        return (exc.code == expected_code, exc.code)
    return (False, "")


def _case_1_roundtrip(key_a: bytes) -> str:
    print("Case 1: Roundtrip mint + verify")
    token = _mint_test_token(hmac_key=key_a)
    claim = _verifier_for(key_a).verify(f"Bearer {token}")
    _check(claim.agent_id == "claude_phone", "1a: claim.agent_id preserved")
    _check(
        claim.agent_instance_id == "agi-oauth-test-client",
        "1b: claim.agent_instance_id preserved",
    )
    _check(
        claim.agent_session_id == _oauth_agent_session_id("test-client"),
        "1c: claim.agent_session_id preserved",
    )
    _check(claim.audience == _AUDIENCE, "1d: claim.audience preserved")
    _check(
        claim.session_label == "Test Client",
        "1e: claim.session_label preserved",
    )
    _check(
        claim.issued_at.tzinfo is not None,
        "1f: claim.issued_at parsed to aware datetime",
    )
    return token


def _case_2_tampered_payload(key_a: bytes, token: str) -> None:
    print("\nCase 2: Tampered payload rejected")
    header_b64, payload_b64, signature_b64 = token.split(".")
    tampered = f"{header_b64}.{payload_b64}X.{signature_b64}"
    matched, _ = _assert_raises_with_code(
        _verifier_for(key_a), tampered, "bearer.invalid_signature",
    )
    _check(matched, "2: BearerAuthError(bearer.invalid_signature) raised")


def _case_3_tampered_signature(key_a: bytes, token: str) -> None:
    print("\nCase 3: Tampered signature rejected")
    header_b64, payload_b64, signature_b64 = token.split(".")
    # Mutate a byte of the DECODED signature, not a base64 CHARACTER. Flipping
    # the final base64url char is unsound: a 32-byte HMAC-SHA256 signature ends
    # in a char that carries only the low 4 bits of byte 31 (2 bits are
    # non-significant padding), so a same-nibble flip (canonical 'A' -> 'B')
    # re-decodes to identical bytes and the "tampered" token still verifies —
    # a ~1/16 probabilistic pass-through that made this case flaky. Byte-level
    # mutation is deterministic: every bit of every signature byte is
    # significant, so a one-bit flip always yields a different signature.
    signature = base64.urlsafe_b64decode(
        signature_b64 + "=" * (-len(signature_b64) % 4),
    )
    tampered_signature = bytes([signature[0] ^ 0x01]) + signature[1:]
    tampered_signature_b64 = (
        base64.urlsafe_b64encode(tampered_signature).decode("ascii").rstrip("=")
    )
    tampered = f"{header_b64}.{payload_b64}.{tampered_signature_b64}"
    matched, _ = _assert_raises_with_code(
        _verifier_for(key_a), tampered, "bearer.invalid_signature",
    )
    _check(matched, "3: BearerAuthError(bearer.invalid_signature) raised")


def _case_4_wrong_key(key_b: bytes, token: str) -> None:
    print("\nCase 4: Wrong key rejected")
    matched, _ = _assert_raises_with_code(
        _verifier_for(key_b), token, "bearer.invalid_signature",
    )
    _check(matched, "4: token signed with key A rejected by verifier with key B")


def _case_5_alg_confusion(key_a: bytes) -> None:
    print("\nCase 5: alg-confusion (alg: none) blocked")
    header_none = base64.urlsafe_b64encode(
        json.dumps({"alg": "none", "typ": "JWT"}).encode("utf-8"),
    ).decode("ascii").rstrip("=")
    payload_attacker = base64.urlsafe_b64encode(
        json.dumps({
            "agent_id": "claude_phone",
            "agent_instance_id": "agi-attacker",
            "issued_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            "aud": _AUDIENCE,
        }).encode("utf-8"),
    ).decode("ascii").rstrip("=")
    none_token = f"{header_none}.{payload_attacker}."
    matched, _ = _assert_raises_with_code(
        _verifier_for(key_a), none_token, "bearer.invalid_signature",
    )
    _check(matched, "5: alg=none token rejected (algorithms pinned to HS256)")


def _case_6_expired(key_a: bytes) -> None:
    print("\nCase 6: Expired token rejected")
    expired_claim = {
        "agent_id": "claude_phone",
        "agent_instance_id": "agi-stale",
        "issued_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "aud": _AUDIENCE,
        "exp": int((datetime.now(UTC) - timedelta(hours=1)).timestamp()),
    }
    expired_token = jwt.encode(
        expired_claim, key_a, algorithm=HMAC_SIGNING_ALGORITHM,
    )
    matched, _ = _assert_raises_with_code(
        _verifier_for(key_a), expired_token, "bearer.expired",
    )
    _check(matched, "6: BearerAuthError(bearer.expired) raised on past exp")


def _case_7_constant_time() -> None:
    print("\nCase 7: Constant-time signature comparison")
    hs256_alg = jwt.get_algorithm_by_name("HS256")
    verify_src = inspect.getsource(hs256_alg.verify)
    uses_constant_time = (
        "compare_digest" in verify_src
        or "hmac.compare_digest" in verify_src
    )
    _check(
        uses_constant_time,
        "7a: pyjwt HS256.verify uses hmac.compare_digest (source inspection)",
    )
    _check(
        hmac_module.compare_digest(b"abc", b"abc")
        and not hmac_module.compare_digest(b"abc", b"abd"),
        "7b: hmac.compare_digest available and correct",
    )


def _case_8_forged_sealed_claim(key_a: bytes) -> None:
    print("\nCase 8 (§8): Forged old sealed-claim token rejected")
    from nacl.public import PrivateKey

    attacker_view_pubkey_b64url = base64.urlsafe_b64encode(
        bytes(PrivateKey.generate().public_key),
    ).decode("ascii").rstrip("=")
    legacy_token = _mint_legacy_sealed_box_token(attacker_view_pubkey_b64url)
    matched, code = _assert_raises_with_code(
        _verifier_for(key_a), legacy_token, "bearer.invalid_signature",
    )
    _check(
        matched,
        f"8: forged sealed-claim token rejected (code={code or '<none>'})",
    )


def _mint_raw_token(
    *,
    hmac_key: bytes,
    issued_at: datetime,
    exp: datetime | None,
    client_id: str = "test-client",
    aud: str = _AUDIENCE,
) -> str:
    """Mint a JWT with a caller-chosen ``issued_at`` / ``exp``.

    ``_issue_access_token`` stamps ``issued_at=now()`` internally, so it
    cannot produce an old-but-valid token; this helper builds the claim
    directly to exercise the skew window vs. ``exp`` independently.
    """
    claim: dict[str, object] = {
        "agent_id": "claude_phone",
        "agent_instance_id": f"agi-oauth-{client_id}",
        "issued_at": issued_at.isoformat().replace("+00:00", "Z"),
        "session_label": "Test Client",
        "scopes": ["mcp:read", "mcp:write"],
        "aud": aud,
        "client_id": client_id,
    }
    if exp is not None:
        claim["exp"] = int(exp.timestamp())
    return jwt.encode(claim, hmac_key, algorithm=HMAC_SIGNING_ALGORITHM)


def _case_9_exp_scopes_skew(key_a: bytes) -> None:
    """B1 Q2: an ``exp``-bearing token is bounded by ``exp``, NOT the issued_at skew.

    RED-FIRST for the auth.py Q2 fix (``if not claim.has_exp: self._check_skew``):
    revert it (verify always runs ``_check_skew``) and 9a flips red — the
    6-min-old-but-valid-exp OAuth token would be wrongly skew-rejected. 9c proves
    the 300s skew is SCOPED to exp-less claims, not removed.
    """
    print("\nCase 9 (B1 Q2): exp scopes the issued_at skew window")
    now = datetime.now(UTC).replace(microsecond=0)
    six_min_ago = now - timedelta(minutes=6)
    # 9a: a 6-min-old token with a valid 24h exp VERIFIES (skew skipped).
    fresh_exp_old_iat = _mint_raw_token(
        hmac_key=key_a, issued_at=six_min_ago, exp=now + timedelta(hours=24),
    )
    try:
        claim = _verifier_for(key_a, at=now).verify(f"Bearer {fresh_exp_old_iat}")
        _check(
            claim.has_exp,
            "9a: 6-min-old VALID-exp OAuth token verifies (skew skipped; has_exp=True)",
        )
    except BearerAuthError as exc:
        _check(False, f"9a: 6-min-old valid-exp token must verify, got {exc.code}")
    # 9b: a past-exp token REJECTS even with a FRESH issued_at (exp governs).
    past_exp = _mint_raw_token(
        hmac_key=key_a, issued_at=now, exp=now - timedelta(minutes=1),
    )
    matched_b, code_b = _assert_raises_with_code(
        _verifier_for(key_a, at=now), past_exp, "bearer.expired",
    )
    _check(
        matched_b,
        f"9b: past-exp token rejected (bearer.expired) despite fresh issued_at (code={code_b})",
    )
    # 9c: an EXP-LESS 6-min-old token is STILL skew-rejected (skew scoped, not removed).
    exp_less_old = _mint_raw_token(hmac_key=key_a, issued_at=six_min_ago, exp=None)
    matched_c, code_c = _assert_raises_with_code(
        _verifier_for(key_a, at=now), exp_less_old, "bearer.expired",
    )
    _check(
        matched_c,
        f"9c: exp-LESS 6-min-old token STILL skew-rejected (scope preserved, code={code_c})",
    )


def _case_10_unregistered_client(key_a: bytes) -> None:
    """Finding-A pin: BearerVerifier rejects a token whose client_id is not in the registry.

    Pins the ``client_exists_check`` enforcement that plugin.py wires on the
    real (non-permissive) verifier — the revoked/never-minted client rejection
    the cutover activates.
    """
    print("\nCase 10: unregistered/revoked client_id rejected (client_exists_check)")
    token = _mint_test_token(hmac_key=key_a)  # client_id="test-client"
    reject_verifier = BearerVerifier(
        hmac_key=key_a,
        max_age_seconds=300,
        accepted_audiences=(_AUDIENCE,),
        client_exists_check=lambda _client_id: False,  # registry: unknown/revoked
    )
    matched, code = _assert_raises_with_code(
        reject_verifier, token, "bearer.unknown_client",
    )
    _check(matched, f"10a: unregistered client_id rejected (bearer.unknown_client, code={code})")
    accept_verifier = BearerVerifier(
        hmac_key=key_a,
        max_age_seconds=300,
        accepted_audiences=(_AUDIENCE,),
        client_exists_check=lambda _client_id: True,  # registry: known
    )
    try:
        accept_verifier.verify(f"Bearer {token}")
        _check(True, "10b: registered client_id accepted (control)")
    except BearerAuthError as exc:
        _check(False, f"10b: registered client_id must be accepted, got {exc.code}")


def _case_11_wrong_audience(key_a: bytes) -> None:
    """Finding-A pin: BearerVerifier rejects a token whose ``aud`` is not accepted.

    Audience binding (MCP 2025-06-18) stops a token minted for endpoint A being
    replayed against endpoint B; pins the ``accepted_audiences`` enforcement.
    """
    print("\nCase 11: wrong-audience token rejected (audience binding)")
    wrong_aud_token = _mint_test_token(
        hmac_key=key_a, resource="https://evil.example.com/mcp",
    )
    matched, code = _assert_raises_with_code(
        _verifier_for(key_a), wrong_aud_token, "bearer.audience_mismatch",
    )
    _check(
        matched,
        f"11a: token minted for a different aud rejected (bearer.audience_mismatch, code={code})",
    )
    right_aud_token = _mint_test_token(hmac_key=key_a)  # resource=_AUDIENCE
    try:
        _verifier_for(key_a).verify(f"Bearer {right_aud_token}")
        _check(True, "11b: correct-audience token accepted (control)")
    except BearerAuthError as exc:
        _check(False, f"11b: correct-audience token must be accepted, got {exc.code}")


def main() -> int:
    print("== Task #53 HMAC bearer-token smoke test ==\n")
    key_a = secrets.token_bytes(HMAC_KEY_BYTE_LENGTH)
    key_b = secrets.token_bytes(HMAC_KEY_BYTE_LENGTH)
    token = _case_1_roundtrip(key_a)
    _case_2_tampered_payload(key_a, token)
    _case_3_tampered_signature(key_a, token)
    _case_4_wrong_key(key_b, token)
    _case_5_alg_confusion(key_a)
    _case_6_expired(key_a)
    _case_7_constant_time()
    _case_8_forged_sealed_claim(key_a)
    _case_9_exp_scopes_skew(key_a)
    _case_10_unregistered_client(key_a)
    _case_11_wrong_audience(key_a)
    print(f"\n== passed={_passed} failed={len(_failed)} ==")
    if _failed:
        print("FAIL labels:")
        for label in _failed:
            print(f"  - {label}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
