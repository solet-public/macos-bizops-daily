"""Cross-host P3 ledger smoke against kara-keen-keeper.

Mints a JWT via OAuth client_credentials against kara's /oauth/token, opens
a bridge session, POSTs ingest_raw_chunk + search_sessions process_call
envelopes, polls each terminal result, and reports SUCCESS or FAILURE.

This is a LIVE network smoke against the kara-keen-keeper remote solet
(W5.G elevation target). When kara is unreachable (DNS fails / network down /
host offline) OR when local OAuth credentials are missing, the smoke SKIPs
with rc=0 instead of failing the suite — mirroring the pattern in
``reset_ingest_state_live_db_smoke.py`` and ``secretgate_ripout_live_db_smoke.py``.
This is the right shape for a cross-host live smoke: the test premise is
invalid without the remote host, so suite regression should not flag the
absence as a code drift.

Usage::

    .venv/bin/python3 ananta/tests/llm/session_ledger/cross_host_kara_ledger_live_smoke.py
"""

from __future__ import annotations

import json
import os
import socket
import sys
import time
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx

KARA_BASE = "https://kara-keen-keeper.acute-focus.com"
TOKEN_URL = f"{KARA_BASE}/oauth/token"
OPEN_URL = f"{KARA_BASE}/api/v1/bridge/open"
CREDS_PATH = Path.home() / "Workspace" / "kara-keen-keeper" / "credentials.json"

SENTINEL = f"kara-ledger-smoke-{uuid.uuid4().hex[:12]}"


def load_machine_client() -> tuple[str, str]:
    creds = json.loads(CREDS_PATH.read_text())
    for c in creds["oauth_clients"]:
        if c["label"] == "machine":
            return c["client_id"], c["client_secret"]
    raise SystemExit("no machine OAuth client in credentials.json")


def mint_token(client_id: str, client_secret: str) -> str:
    r = httpx.post(
        TOKEN_URL,
        auth=(client_id, client_secret),
        data={"grant_type": "client_credentials"},
        timeout=15.0,
    )
    r.raise_for_status()
    return str(r.json()["access_token"])


def open_bridge(bearer: str) -> str:
    headers = {"Authorization": f"Bearer {bearer}", "Content-Type": "application/json"}
    r = httpx.post(OPEN_URL, headers=headers, json={"parent_pid": os.getpid()}, timeout=15.0)
    print(f"  POST {OPEN_URL} -> {r.status_code}")
    r.raise_for_status()
    return str(r.json()["bridge_id"])


def call_process(bearer: str, bridge_id: str, process_key: str, arguments: dict[str, Any]) -> dict[str, Any]:
    url = f"{KARA_BASE}/api/v1/bridge/{bridge_id}/process/call"
    headers = {"Authorization": f"Bearer {bearer}", "Content-Type": "application/json"}
    body = {"process_key": process_key, "arguments": arguments, "reason": "kara cross-host P3 smoke"}
    r = httpx.post(url, headers=headers, json=body, timeout=30.0)
    print(f"  POST .../process/call({process_key}) -> {r.status_code}")
    r.raise_for_status()
    return r.json()  # type: ignore[no-any-return]


def fetch_result(bearer: str, bridge_id: str, action_id: str, attempts: int = 30, interval: float = 2.0) -> dict[str, Any]:
    url = f"{KARA_BASE}/api/v1/bridge/{bridge_id}/process/result/{action_id}"
    headers = {"Authorization": f"Bearer {bearer}"}
    for i in range(attempts):
        r = httpx.get(url, headers=headers, timeout=15.0)
        if r.status_code != 200:
            print(f"  result attempt {i + 1}: HTTP {r.status_code}")
            time.sleep(interval)
            continue
        payload = r.json()
        status = payload.get("status") or payload.get("action_status")
        if status in ("completed", "failed", "success"):
            return payload  # type: ignore[no-any-return]
        print(f"  result attempt {i + 1}: status={status}")
        time.sleep(interval)
    return {"status": "timeout"}


def close_bridge(bearer: str, bridge_id: str) -> None:
    url = f"{KARA_BASE}/api/v1/bridge/{bridge_id}/close"
    headers = {"Authorization": f"Bearer {bearer}"}
    try:
        httpx.post(url, headers=headers, json={}, timeout=10.0)
    except httpx.HTTPError:
        pass


def _kara_reachable() -> tuple[bool, str]:
    """Probe whether the kara host is DNS-resolvable + reachable.

    Returns (True, "") when the host resolves AND a quick TCP-connect
    attempt to port 443 succeeds; (False, reason) when either step fails
    so the smoke can SKIP cleanly rather than fail with an httpx exception.
    """
    parsed = urlparse(KARA_BASE)
    host = parsed.hostname or ""
    port = parsed.port or 443
    if not host:
        return False, "could not parse host from KARA_BASE"
    try:
        socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        return False, f"DNS resolution failed for {host}: {exc}"
    try:
        with socket.create_connection((host, port), timeout=5.0):
            pass
    except (TimeoutError, OSError) as exc:
        return False, f"TCP connect to {host}:{port} failed: {exc}"
    return True, ""


def _prereq_skip_reason() -> str | None:
    """Return a SKIP-reason string when prerequisites for the live smoke
    are not met; return None when everything is in place to actually run."""
    reachable, reason = _kara_reachable()
    if not reachable:
        return f"kara unreachable: {reason}"
    if not CREDS_PATH.exists():
        return f"credentials file absent at {CREDS_PATH}"
    return None


def _build_chunk_text() -> str:
    session_uuid = uuid.uuid4().hex
    ts = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime())
    sentinel_text = (
        f"Cross-host P3 validation sentinel for {SENTINEL}. "
        f"This is a synthetic user message generated by the kara-ledger-smoke "
        f"harness against kara-keen-keeper to verify the cross-host shipping "
        f"path: ingest_raw_chunk → claude_code_pushed_session_source_plugin "
        f"parser → ledger writes events to Postgres → M6 summarize cron → "
        f"search_sessions returns the sentinel."
    )
    return json.dumps({
        "sessionId": session_uuid,
        "type": "user",
        "message": {"role": "user", "content": sentinel_text},
        "timestamp": ts,
        "cwd": "/Users/alice/Workspace/example",
    })


def _extract_hits(result: dict[str, Any]) -> list[Any]:
    """Pull the hits list out of the polled-result envelope variants."""
    inner_result = result.get("result")
    if isinstance(inner_result, dict):
        data = inner_result.get("data")
    else:
        data = result.get("data")
    if not isinstance(data, dict):
        return []
    hits = data.get("hits") or data.get("matches") or []
    return list(hits) if isinstance(hits, list) else []


def _drive_search_phase(bearer: str, bridge_id: str) -> bool:
    """Return True when the sentinel is found in search_sessions hits."""
    print("[*] Phase 4c: search_sessions")
    search = call_process(
        bearer, bridge_id,
        "service_interface::session_ledger_service::search_sessions",
        {"query": SENTINEL, "top_k": 5},
    )
    print(f"  search envelope: {json.dumps(search, indent=2)[:600]}")
    action_id = search.get("action_id") or search.get("data", {}).get("action_id")
    if not action_id:
        print("[fail] P3 round-trip not confirmed (no search action_id)")
        return False
    result = fetch_result(bearer, bridge_id, action_id)
    print(f"  search terminal: {json.dumps(result, indent=2)[:1500]}")
    hits = _extract_hits(result)
    print(f"  hit count: {len(hits)}")
    for h in hits[:3]:
        print(f"    - {str(h)[:200]}")
    if any(SENTINEL in str(h) for h in hits):
        print("[ok] P3 ROUND-TRIP CONFIRMED — sentinel found in search results")
        return True
    print("[!] sentinel NOT found in search hits")
    print("[fail] P3 round-trip not confirmed")
    return False


def _drive_ingest_phase(bearer: str, bridge_id: str) -> None:
    print("[*] Phase 4a: ingest_raw_chunk (JSONL claude_code transcript shape)")
    chunk_text = _build_chunk_text()
    ingest = call_process(
        bearer, bridge_id,
        "service_interface::session_ledger_service::ingest_raw_chunk",
        {"source_kind": "claude_code_pushed", "chunk_text": chunk_text},
    )
    print(f"  ingest envelope: {json.dumps(ingest, indent=2)[:600]}")
    action_id = ingest.get("action_id") or ingest.get("data", {}).get("action_id")
    if action_id:
        result = fetch_result(bearer, bridge_id, action_id)
        print(f"  ingest terminal: {json.dumps(result, indent=2)[:800]}")
    else:
        print("[!] no action_id in ingest envelope")


def main() -> int:
    print(f"[*] sentinel keyword: {SENTINEL}")
    skip_reason = _prereq_skip_reason()
    if skip_reason is not None:
        print(f"[SKIP] {skip_reason}")
        print(
            "[SKIP] cross-host P3 smoke requires kara-keen-keeper live + "
            "DNS-resolvable + local OAuth credentials present; treating "
            "as SKIP rather than FAIL so suite regression does not flag "
            "the absence as code drift."
        )
        return 0
    cid, csec = load_machine_client()
    print(f"[*] machine client_id: {cid}")

    bearer = mint_token(cid, csec)
    print(f"[ok] minted bearer (len={len(bearer)})")

    bridge_id = open_bridge(bearer)
    print(f"[ok] opened bridge: {bridge_id}")

    try:
        _drive_ingest_phase(bearer, bridge_id)
        print("[*] Phase 4b: wait briefly for summarize cron")
        time.sleep(20)
        if _drive_search_phase(bearer, bridge_id):
            return 0
        return 1
    finally:
        close_bridge(bearer, bridge_id)
        print("[ok] bridge closed")


if __name__ == "__main__":
    sys.exit(main())
