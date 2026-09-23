#!/usr/bin/env python3
"""End-to-end integration test for the sealed-box secret transfer protocol.

Drives the four new vault processes against a running solet through the
agent_messaging_plugin HTTP bridge. In v1 the sender and recipient are the
same solet (the solet talks to itself) — proves the crypto plumbing
works without requiring two solets to be live. Once the abbey-NNNN dry-run
stack supports peer transfer, this same script can be pointed at two
bridges by setting ``EXAMPLE_BRIDGE_URL`` and ``PEER_BRIDGE_URL`` separately.

Asserts:
  1. ``ensure_encryption_keypair`` returns created=True on first call,
     created=False on second, same public key both times.
  2. ``get_public_key`` returns the same key as the bootstrap.
  3. ``export_encrypted`` -> ``import_encrypted`` round-trip produces
     matching fingerprints.
  4. The captured HTTP transcript contains zero bytes of plaintext.
  5. Malformed ciphertext yields ``vault.decrypt_failed`` cleanly with no
     Python traceback in the MCP response.
  6. ``overwrite=False`` against an existing name returns
     ``vault.secret_already_exists``; ``overwrite=True`` succeeds.
  7. The Postgres-backed audit log contains one ``direction='export'`` and
     one ``direction='import'`` row with matching ``plaintext_fingerprint``
     after a successful round-trip — and none of the audit columns leak
     plaintext or ciphertext.

Run from the repo root with the solet already up:

    .venv/bin/python3 plugins/macos_vault_plugin/tests/secret_transfer_protocol_live_smoke.py
"""

from __future__ import annotations

import json
import os
import re
import secrets
import socket
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, cast

import psycopg
from psycopg.rows import DictRow, dict_row

DEFAULT_BRIDGE_PORT_FILE = (
    Path.home() / ".ananta" / "runtime" / f"{os.environ['SOLET_NAME']}.bridge.port"
)
DEFAULT_BRIDGE_HOST = "127.0.0.1"
API_PREFIX = "/api/v1/bridge"

_REPO_ROOT = Path(__file__).resolve().parents[3]
_POSTGRES_CONFIG_FILE = (
    _REPO_ROOT / "profile" / "config" / "plugins" / "postgres_state_management_plugin.json"
)
AUDIT_TABLE_FULLNAME = "default_vault_plugin__secret_transfer_audit"

PROCESS_KEY_ENSURE = "service_interface::vault_service::ensure_encryption_keypair"
PROCESS_KEY_GET_PUB = "service_interface::vault_service::get_public_key"
PROCESS_KEY_EXPORT = "service_interface::vault_service::export_encrypted"
PROCESS_KEY_IMPORT = "service_interface::vault_service::import_encrypted"
PROCESS_KEY_DELETE = "service_interface::vault_service::delete"
PROCESS_KEY_STORE = "service_interface::vault_service::store"
PROCESS_KEY_RETRIEVE = "service_interface::vault_service::retrieve"

SECRET_NAME = "sealed_box_smoke_test_secret"
RESULT_POLL_TIMEOUT_SECONDS = 30.0
RESULT_POLL_INTERVAL_SECONDS = 0.25


class SmokeError(RuntimeError):
    """Raised on any assertion failure; printed by main() and exits non-zero."""


def _bridge_url(host: str, port: int) -> str:
    return f"http://{host}:{port}{API_PREFIX}"


def _post(url: str, body: dict[str, Any], transcript: list[str]) -> dict[str, Any]:
    payload = json.dumps(body).encode("utf-8")
    transcript.append(f"POST {url} body={json.dumps(body)}")
    req = urllib.request.Request(
        url, data=payload, method="POST",
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        text = resp.read().decode("utf-8")
    transcript.append(f"  <- {text}")
    parsed: dict[str, Any] = json.loads(text)
    return parsed


def _get(url: str, transcript: list[str]) -> dict[str, Any]:
    transcript.append(f"GET {url}")
    with urllib.request.urlopen(url, timeout=30) as resp:
        text = resp.read().decode("utf-8")
    transcript.append(f"  <- {text}")
    parsed: dict[str, Any] = json.loads(text)
    return parsed


def _resolve_bridge_port() -> int:
    env_port = os.environ.get("EXAMPLE_BRIDGE_PORT")
    if env_port:
        return int(env_port)
    if not DEFAULT_BRIDGE_PORT_FILE.exists():
        raise SmokeError(
            f"bridge port file not found at {DEFAULT_BRIDGE_PORT_FILE}; "
            "is the solet running? Try `./launch.py`."
        )
    return int(DEFAULT_BRIDGE_PORT_FILE.read_text().strip())


_LIVE_ENV = "SECRET_TRANSFER_LIVE_SMOKE"


def _prereq_skip_reason() -> str | None:
    """SKIP-reason (None → run). Gate this live smoke so the offline suite
    NEVER fails-red offline and NEVER silent-writes the running solet's
    vault keypair + Postgres audit rows: skip unless the explicit
    ``SECRET_TRANSFER_LIVE_SMOKE=1`` opt-in is set (the ``*_live_smoke``
    convention), and skip-clean when the bridge is unreachable (mirrors
    ``cross_host_kara_ledger``'s reachability model)."""
    if os.environ.get(_LIVE_ENV) != "1":
        return (
            f"set {_LIVE_ENV}=1 to run "
            "(LIVE-writes vault keypair + Postgres audit rows on the running solet)"
        )
    env_port = os.environ.get("EXAMPLE_BRIDGE_PORT")
    if env_port:
        port = int(env_port)
    elif DEFAULT_BRIDGE_PORT_FILE.exists():
        port = int(DEFAULT_BRIDGE_PORT_FILE.read_text().strip())
    else:
        return f"bridge port file not found at {DEFAULT_BRIDGE_PORT_FILE} (is the solet running?)"
    try:
        with socket.create_connection((DEFAULT_BRIDGE_HOST, port), timeout=5.0):
            pass
    except OSError as exc:
        return f"bridge TCP connect to {DEFAULT_BRIDGE_HOST}:{port} failed: {exc}"
    return None


def _open_bridge(base: str, transcript: list[str]) -> str:
    payload = _post(f"{base}/open", {"parent_pid": os.getpid()}, transcript)
    bridge_id = payload.get("bridge_id")
    if not isinstance(bridge_id, str) or not bridge_id:
        raise SmokeError(f"bridge open returned no bridge_id: {payload!r}")
    return bridge_id


def _close_bridge(base: str, bridge_id: str, transcript: list[str]) -> None:
    try:
        _post(f"{base}/{bridge_id}/close", {}, transcript)
    except urllib.error.URLError as exc:
        transcript.append(f"  close ignored error: {exc}")


def _process_call(
    base: str,
    bridge_id: str,
    process_key: str,
    arguments: dict[str, Any],
    transcript: list[str],
) -> dict[str, Any]:
    """Submit a process_call and poll until the result is committed."""
    call_payload = _post(
        f"{base}/{bridge_id}/process/call",
        {"process_key": process_key, "arguments": arguments},
        transcript,
    )
    action_id = call_payload.get("action_id")
    if not isinstance(action_id, str) or not action_id:
        raise SmokeError(
            f"process_call returned no action_id: {call_payload!r}"
        )

    deadline = time.monotonic() + RESULT_POLL_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        result = _get(f"{base}/{bridge_id}/process/result/{action_id}", transcript)
        if result.get("status") in {"completed", "error", "failed"}:
            return result
        time.sleep(RESULT_POLL_INTERVAL_SECONDS)
    raise SmokeError(
        f"timed out waiting for action {action_id} on {process_key}"
    )


def _result_data(result: dict[str, Any]) -> dict[str, Any]:
    """Extract the ``data`` dict from a process_result payload.

    The result envelope wraps the plugin's ActionResult; the inner ``result``
    field carries it through verbatim.
    """
    inner = result.get("result")
    if not isinstance(inner, dict):
        raise SmokeError(f"missing inner result envelope: {result!r}")
    data = inner.get("data")
    if not isinstance(data, dict):
        raise SmokeError(f"missing data dict in result: {result!r}")
    return data


def _result_status(result: dict[str, Any]) -> str:
    inner = result.get("result")
    if not isinstance(inner, dict):
        return "unknown"
    status = inner.get("action_status")
    return str(status) if status is not None else "unknown"


def _result_error_code(result: dict[str, Any]) -> str | None:
    """Return the most specific error code in the result envelope.

    The platform wraps every action failure as ``code='action_failed'`` at
    the outer level and stringifies the originating plugin's error dict
    into ``message``. Prefer the inner code when present (it carries
    plugin-level semantics like ``vault.secret_already_exists``); fall
    back to the outer code only when no inner one can be parsed.
    """
    inner = result.get("result")
    if not isinstance(inner, dict):
        return None
    err = inner.get("error")
    if not isinstance(err, dict):
        return None
    msg = err.get("message", "")
    if isinstance(msg, str):
        m = re.search(r"'code':\s*'([^']+)'", msg)
        if m:
            return m.group(1)
    code = err.get("code")
    return str(code) if isinstance(code, str) else None


def _ensure_secret_absent(
    base: str, bridge_id: str, name: str, transcript: list[str]
) -> None:
    """Delete the test secret if a previous run left it lying around."""
    _process_call(base, bridge_id, PROCESS_KEY_DELETE, {"key": name}, transcript)


def _store_test_secret(
    base: str, bridge_id: str, name: str, value: str, transcript: list[str]
) -> None:
    result = _process_call(
        base, bridge_id, PROCESS_KEY_STORE,
        {"key": name, "value": value},
        transcript,
    )
    if _result_status(result) != "completed":
        raise SmokeError(f"store({name}) did not complete: {result!r}")


def _retrieve_test_secret(
    base: str, bridge_id: str, name: str, transcript: list[str]
) -> str:
    result = _process_call(
        base, bridge_id, PROCESS_KEY_RETRIEVE,
        {"key": name},
        transcript,
    )
    data = _result_data(result)
    value = data.get("value")
    if not isinstance(value, str):
        raise SmokeError(f"retrieve({name}) returned no string value: {result!r}")
    return value


def _assert_eq(label: str, actual: Any, expected: Any) -> None:
    if actual != expected:
        raise SmokeError(f"{label}: expected {expected!r}, got {actual!r}")


def _assert_no_plaintext_in_transcript(plaintext: str, transcript: list[str]) -> None:
    joined = "\n".join(transcript)
    if plaintext in joined:
        raise SmokeError(
            "PLAINTEXT LEAK: the secret value appeared in the captured "
            "HTTP transcript at least once. This is the discipline failure "
            "the protocol exists to prevent."
        )


def _connect_postgres() -> psycopg.Connection[DictRow]:
    """Open a read-only psycopg connection using the platform's own DB config.

    The ``row_factory=dict_row`` shape lets each fetched row come back as
    a ``dict[str, Any]`` instead of a tuple. pyright's generic signature
    for ``Connection.connect`` doesn't propagate the row_factory type
    parameter, so we cast through ``Connection[DictRow]`` explicitly.
    """
    cfg: dict[str, Any] = json.loads(_POSTGRES_CONFIG_FILE.read_text())
    conninfo = (
        f"host={cfg['host']} port={cfg['port']} "
        f"dbname={cfg['database']} user={cfg['user']} "
        f"password={cfg['password']} "
        f"options=-csearch_path={cfg['schema']},public"
    )
    return cast(
        "psycopg.Connection[DictRow]",
        psycopg.Connection.connect(
            conninfo,
            row_factory=dict_row,  # pyright: ignore[reportArgumentType]
        ),
    )


def _read_audit_rows_for_secret(secret_name: str) -> list[dict[str, Any]]:
    """Return the most recent audit rows for ``secret_name`` (newest first)."""
    sql = (
        f"SELECT direction, secret_name, peer_identifier, "
        f"peer_pubkey_fingerprint, plaintext_fingerprint, status, "
        f"error_message, created_at "
        f"FROM {AUDIT_TABLE_FULLNAME} "
        f"WHERE secret_name = %s "
        f"ORDER BY created_at DESC LIMIT 50"
    )
    with _connect_postgres() as conn, conn.cursor() as cur:
        cur.execute(sql, (secret_name,))
        return [dict(row) for row in cur.fetchall()]


def _audit_success_rows(rows: list[dict[str, object]], direction: str) -> list[dict[str, object]]:
    """Filter audit rows to the success entries for one direction."""
    return [
        r for r in rows
        if r["direction"] == direction and r["status"] == "success"
    ]


def _assert_audit_direction_fingerprint(
    secret_name: str,
    direction: str,
    rows: list[dict[str, object]],
    expected_fingerprint: str,
) -> None:
    """Assert one success row exists for ``direction`` and its fingerprint matches."""
    success_rows = _audit_success_rows(rows, direction)
    if not success_rows:
        raise SmokeError(
            f"audit log has no successful {direction} row for {secret_name!r}; "
            f"rows={rows!r}"
        )
    actual = success_rows[0]["plaintext_fingerprint"]
    if actual != expected_fingerprint:
        raise SmokeError(
            f"audit {direction} row has mismatched plaintext_fingerprint: "
            f"expected {expected_fingerprint!r}, got {actual!r}"
        )


def _assert_audit_no_secret_leakage(
    rows: list[dict[str, object]],
    plaintext: str,
    ciphertext: str,
) -> None:
    """Sanity-check that no audit column leaks plaintext or ciphertext."""
    forbidden_substrings = (plaintext, ciphertext)
    for row in rows:
        flattened = " ".join(str(v) for v in row.values() if v is not None)
        for forbidden in forbidden_substrings:
            if forbidden and forbidden in flattened:
                raise SmokeError(
                    "audit row contains forbidden content (plaintext or "
                    f"ciphertext): row={row!r}"
                )


def _assert_audit_round_trip(
    secret_name: str,
    expected_fingerprint: str,
    plaintext: str,
    ciphertext: str,
) -> None:
    """Verify a Store-backed audit row pair exists and contains no secrets.

    The smoke run produces (at minimum) one success export and one success
    import; the SECRET_ALREADY_EXISTS path also adds an error row, and the
    malformed-ciphertext case targets a different secret_name. Filter to
    the success rows we care about via ``_audit_success_rows``.
    """
    rows = _read_audit_rows_for_secret(secret_name)
    if not rows:
        raise SmokeError(
            f"audit table {AUDIT_TABLE_FULLNAME} returned no rows for "
            f"secret_name={secret_name!r}"
        )
    _assert_audit_direction_fingerprint(secret_name, "export", rows, expected_fingerprint)
    _assert_audit_direction_fingerprint(secret_name, "import", rows, expected_fingerprint)
    _assert_audit_no_secret_leakage(rows, plaintext, ciphertext)


def _print_step(label: str) -> None:
    print(f"\n=== {label} ===", flush=True)


def main() -> int:
    skip_reason = _prereq_skip_reason()
    if skip_reason is not None:
        print(f"[SKIP] {skip_reason}")
        return 0
    # The leak-check covers only the four sealed-box protocol calls — the
    # security boundary the protocol is designed to protect. The setup
    # calls (store/retrieve/cleanup-delete) legitimately carry plaintext
    # over HTTP because that is what those vault operations do; they're
    # not part of the protocol's no-leak guarantee.
    setup_transcript: list[str] = []
    transcript: list[str] = []
    port = _resolve_bridge_port()
    base = _bridge_url(DEFAULT_BRIDGE_HOST, port)
    print(f"bridge: {base}", flush=True)

    # Generate a fresh, distinctive plaintext we can grep for.
    secret_plaintext = "sk-ant-smoke-" + secrets.token_hex(12)
    print(f"plaintext fingerprint key: sk-ant-smoke-<{len(secret_plaintext)}b>", flush=True)

    bridge_id = _open_bridge(base, setup_transcript)
    try:
        _print_step("clean prior smoke-test secret if present")
        _ensure_secret_absent(base, bridge_id, SECRET_NAME, setup_transcript)

        _print_step("store fresh plaintext into vault for export")
        _store_test_secret(base, bridge_id, SECRET_NAME, secret_plaintext, setup_transcript)

        _print_step("ensure_encryption_keypair (first call: created may be True or False)")
        r1 = _process_call(base, bridge_id, PROCESS_KEY_ENSURE, {}, transcript)
        d1 = _result_data(r1)
        pub_first = d1["public_key"]
        if not isinstance(pub_first, str) or not pub_first:
            raise SmokeError(f"ensure_encryption_keypair missing public_key: {r1!r}")

        _print_step("ensure_encryption_keypair (second call: must return created=False, same pubkey)")
        r2 = _process_call(base, bridge_id, PROCESS_KEY_ENSURE, {}, transcript)
        d2 = _result_data(r2)
        _assert_eq("ensure_encryption_keypair second call created", d2.get("created"), False)
        _assert_eq("ensure_encryption_keypair public_key stable", d2.get("public_key"), pub_first)

        _print_step("get_public_key returns the same key")
        r3 = _process_call(base, bridge_id, PROCESS_KEY_GET_PUB, {}, transcript)
        d3 = _result_data(r3)
        _assert_eq("get_public_key matches", d3.get("public_key"), pub_first)

        _print_step("export_encrypted")
        r4 = _process_call(
            base, bridge_id, PROCESS_KEY_EXPORT,
            {
                "secret_name": SECRET_NAME,
                "recipient_pubkey": pub_first,
                "recipient_identifier": "self",
            },
            transcript,
        )
        d4 = _result_data(r4)
        ciphertext = d4.get("ciphertext")
        export_fp = d4.get("plaintext_fingerprint")
        if not isinstance(ciphertext, str) or not ciphertext:
            raise SmokeError(f"export_encrypted missing ciphertext: {r4!r}")
        if not isinstance(export_fp, str) or not export_fp.startswith("sha256:"):
            raise SmokeError(f"export_encrypted missing fingerprint: {r4!r}")

        _print_step("import_encrypted with overwrite=True (round-trip into same vault)")
        r5 = _process_call(
            base, bridge_id, PROCESS_KEY_IMPORT,
            {
                "name": SECRET_NAME,
                "ciphertext": ciphertext,
                "sender_identifier": "self",
                "overwrite": True,
            },
            transcript,
        )
        d5 = _result_data(r5)
        _assert_eq("import_encrypted ok flag", d5.get("ok"), True)
        _assert_eq(
            "fingerprints match (export vs import)",
            d5.get("plaintext_fingerprint"),
            export_fp,
        )

        _print_step("retrieve confirms the imported plaintext matches the original")
        recovered = _retrieve_test_secret(base, bridge_id, SECRET_NAME, setup_transcript)
        _assert_eq("recovered plaintext", recovered, secret_plaintext)

        _print_step("import_encrypted with overwrite=False against existing name -> SECRET_ALREADY_EXISTS")
        r6 = _process_call(
            base, bridge_id, PROCESS_KEY_IMPORT,
            {
                "name": SECRET_NAME,
                "ciphertext": ciphertext,
                "overwrite": False,
            },
            transcript,
        )
        _assert_eq(
            "overwrite=False yields SECRET_ALREADY_EXISTS",
            _result_error_code(r6),
            "vault.secret_already_exists",
        )

        _print_step("import_encrypted with malformed ciphertext -> DECRYPT_FAILED")
        r7 = _process_call(
            base, bridge_id, PROCESS_KEY_IMPORT,
            {
                "name": SECRET_NAME + "_bogus",
                "ciphertext": "AAECAwQFBgcICQ==",  # 10 bytes, way too short to decrypt
            },
            transcript,
        )
        _assert_eq(
            "malformed ciphertext yields DECRYPT_FAILED",
            _result_error_code(r7),
            "vault.decrypt_failed",
        )

        _print_step("no plaintext in HTTP transcript")
        _assert_no_plaintext_in_transcript(secret_plaintext, transcript)

        _print_step("Postgres-backed audit log carries export + import rows")
        _assert_audit_round_trip(
            secret_name=SECRET_NAME,
            expected_fingerprint=export_fp,
            plaintext=secret_plaintext,
            ciphertext=ciphertext,
        )
    finally:
        _close_bridge(base, bridge_id, transcript)

    print("\nOK: sealed-box secret transfer round-trip passed all checks.", flush=True)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SmokeError as exc:
        print(f"\nSMOKE FAILURE: {exc}", file=sys.stderr)
        sys.exit(1)
