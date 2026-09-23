"""HTTP walker for Claude Code Cloud sessions (Managed Agents API).

Authentication primitive (macOS Keychain) + ``GET /v1/code/sessions?beta=true``
list pagination + ``GET /v1/code/sessions/<id>/events?beta=true`` per-session
event fetch with cursor pagination + ``asyncio.Semaphore``-bounded parallel
walk with exponential-backoff retry on HTTP 429. The walker produces one JSON
envelope per session (carrying all paginated events) and the plugin layer
dispatches it through the existing push-mode importer seam.

Design v3 §2.3a bearer-cell. Phase A
``workbench/2026-06-13_claude_code_web_phase_a_discovery.md``. Endpoints
corrected 2026-06-17 PT post-empirical-probe against api.anthropic.com — the
canonical path is ``/v1/code/sessions`` (Managed Agents API), NOT
``/v1/sessions`` as the older Phase A doc inferred from the CLI binary's
``--teleport`` symbols. The `result`/`control_request`/`control_response`
events on the cloud surface are managed-agents-API plumbing and have no
local-JSONL counterpart; they're filtered at the plugin's parse_chunk layer.
"""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
import subprocess
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

import httpx

logger = logging.getLogger(__name__)

_HEADER_AUTHORIZATION = "Authorization"
_HEADER_ANTHROPIC_VERSION = "anthropic-version"
_HEADER_ANTHROPIC_BETA = "anthropic-beta"
_HEADER_USER_AGENT = "User-Agent"
_USER_AGENT_CLAUDE_CODE_CLOUD = "ananta-claude-code-cloud-walker/1.0"


class ClaudeCodeCloudWalkerError(RuntimeError):
    """Walker-level error with a stable token in ``code``."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class ClaudeCodeCloudCredentials:
    """OAuth bearer extracted from the macOS Keychain envelope."""

    access_token: str


@dataclass(frozen=True, slots=True)
class WalkerConfig:
    """Per-call walker configuration (sourced from plugin.yaml)."""

    keychain_service: str
    keychain_envelope_path: str
    api_base_url: str
    anthropic_version: str
    anthropic_beta: str
    list_page_limit: int
    events_page_limit: int
    walker_concurrency: int
    fetch_timeout_seconds: int
    rate_limit_backoff_seconds: tuple[int, ...]


@dataclass(slots=True)
class WalkerReport:
    """Aggregated outcome of one ``backfill_from_cloud`` invocation."""

    listed_count: int = 0
    fetched_count: int = 0
    skipped_count: int = 0
    errored_count: int = 0
    errors: list[str] = field(default_factory=list)


# ───────────────────────────────────────────────────────────────────────────
# Credential resolution
# ───────────────────────────────────────────────────────────────────────────


def load_credentials(
    *,
    keychain_service: str,
    envelope_path: str,
) -> ClaudeCodeCloudCredentials:
    """Read the macOS Keychain envelope and extract the OAuth bearer."""
    security_bin = shutil.which("security")
    if security_bin is None:
        raise ClaudeCodeCloudWalkerError(
            "auth_missing",
            "macOS 'security' binary not found on PATH; this plugin only "
            "supports macOS Keychain credentials today.",
        )
    try:
        result = subprocess.run(  # noqa: S603 — fixed argv, no shell
            [security_bin, "find-generic-password", "-s", keychain_service, "-w"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ClaudeCodeCloudWalkerError(
            "auth_unreadable",
            f"failed to run 'security find-generic-password': {exc}",
        ) from exc
    if result.returncode != 0:
        raise ClaudeCodeCloudWalkerError(
            "auth_missing",
            f"Keychain entry {keychain_service!r} not found; "
            "run `claude auth login` first.",
        )
    raw = result.stdout.strip()
    if not raw:
        raise ClaudeCodeCloudWalkerError(
            "auth_malformed",
            f"Keychain entry {keychain_service!r} is empty.",
        )
    try:
        envelope = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ClaudeCodeCloudWalkerError(
            "auth_malformed",
            f"Keychain entry {keychain_service!r} is not valid JSON: {exc}",
        ) from exc
    token = _walk_envelope_path(envelope, envelope_path)
    if not isinstance(token, str) or not token:
        raise ClaudeCodeCloudWalkerError(
            "auth_malformed",
            f"Keychain envelope missing string at path {envelope_path!r}; "
            "re-run `claude auth login`.",
        )
    return ClaudeCodeCloudCredentials(access_token=token)


def _walk_envelope_path(envelope: object, path: str) -> object:
    cursor: object = envelope
    for segment in path.split("."):
        if not isinstance(cursor, dict):
            return None
        cursor = cursor.get(segment)
    return cursor


# ───────────────────────────────────────────────────────────────────────────
# HTTP walk — list sessions
# ───────────────────────────────────────────────────────────────────────────


async def list_all_session_summaries(
    *,
    client: httpx.AsyncClient,
    config: WalkerConfig,
    creds: ClaudeCodeCloudCredentials,
) -> list[dict[str, Any]]:
    """Walk ``GET /v1/code/sessions?beta=true`` cursor pagination until exhausted."""
    summaries: list[dict[str, Any]] = []
    cursor: str | None = None
    url = f"{config.api_base_url.rstrip('/')}/v1/code/sessions"
    headers = _request_headers(creds, config)
    seen_cursors: set[str] = set()
    while True:
        payload = await _fetch_one_list_page(
            client=client,
            url=url,
            headers=headers,
            limit=config.list_page_limit,
            cursor=cursor,
        )
        summaries.extend(_extract_summaries_from_page(payload))
        candidate = _next_cursor(payload)
        if candidate is None or candidate in seen_cursors:
            break
        seen_cursors.add(candidate)
        cursor = candidate
    return summaries


async def _fetch_one_list_page(
    *,
    client: httpx.AsyncClient,
    url: str,
    headers: dict[str, str],
    limit: int,
    cursor: str | None,
) -> dict[str, Any]:
    params: dict[str, str] = {"beta": "true", "limit": str(limit)}
    if cursor is not None:
        params["cursor"] = cursor
    response = await client.get(url, headers=headers, params=params)
    _raise_on_auth_failure(response)
    if response.status_code != 200:
        raise ClaudeCodeCloudWalkerError(
            "list_failed",
            f"GET /v1/code/sessions returned HTTP {response.status_code}: "
            f"{response.text[:200]}",
        )
    payload = response.json()
    if not isinstance(payload, dict):
        raise ClaudeCodeCloudWalkerError(
            "list_failed",
            "GET /v1/code/sessions response was not a JSON object",
        )
    return payload


def _extract_summaries_from_page(payload: dict[str, Any]) -> list[dict[str, Any]]:
    page = payload.get("data")
    if isinstance(page, list):
        return [item for item in page if isinstance(item, dict)]
    raise ClaudeCodeCloudWalkerError(
        "list_failed",
        "GET /v1/code/sessions response missing 'data' list",
    )


def _next_cursor(payload: dict[str, Any]) -> str | None:
    candidate = payload.get("next_cursor")
    if isinstance(candidate, str) and candidate:
        return candidate
    return None


# ───────────────────────────────────────────────────────────────────────────
# HTTP walk — fetch per-session events
# ───────────────────────────────────────────────────────────────────────────


async def fetch_session_envelope(
    *,
    client: httpx.AsyncClient,
    config: WalkerConfig,
    creds: ClaudeCodeCloudCredentials,
    summary: dict[str, Any],
) -> str:
    """Fetch a session's full event stream with cursor pagination + 429 backoff.

    Returns the JSON envelope string the plugin's ``parse_chunk`` consumes
    via the importer's ``dispatch_pushed`` seam.
    """
    session_id = _summary_string(summary, "id")
    events_url = (
        f"{config.api_base_url.rstrip('/')}/v1/code/sessions/{session_id}/events"
    )
    headers = _request_headers(creds, config)
    events = await _walk_events_pagination(
        client=client, url=events_url, headers=headers, config=config,
        session_id=session_id,
    )
    return _build_envelope(summary=summary, events=events)


async def _walk_events_pagination(
    *,
    client: httpx.AsyncClient,
    url: str,
    headers: dict[str, str],
    config: WalkerConfig,
    session_id: str,
) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    cursor: str | None = None
    seen_cursors: set[str] = set()
    while True:
        payload = await _fetch_one_events_page(
            client=client, url=url, headers=headers, config=config,
            session_id=session_id, cursor=cursor,
        )
        page = payload.get("data")
        if isinstance(page, list):
            events.extend(item for item in page if isinstance(item, dict))
        candidate = _next_cursor(payload)
        if candidate is None or candidate in seen_cursors:
            break
        seen_cursors.add(candidate)
        cursor = candidate
    return events


async def _fetch_one_events_page(
    *,
    client: httpx.AsyncClient,
    url: str,
    headers: dict[str, str],
    config: WalkerConfig,
    session_id: str,
    cursor: str | None,
) -> dict[str, Any]:
    params: dict[str, str] = {"beta": "true", "limit": str(config.events_page_limit)}
    if cursor is not None:
        params["cursor"] = cursor
    for attempt, delay in enumerate(config.rate_limit_backoff_seconds):
        response = await client.get(url, headers=headers, params=params)
        if response.status_code == 200:
            payload = response.json()
            if not isinstance(payload, dict):
                raise ClaudeCodeCloudWalkerError(
                    "fetch_failed",
                    f"events response for {session_id} was not a JSON object",
                )
            return payload
        _raise_on_auth_failure(response)
        if response.status_code == 429:
            logger.info(
                "claude_code_cloud: rate-limited on events %s (attempt %d); sleeping %ds",
                session_id, attempt + 1, delay,
            )
            await asyncio.sleep(delay)
            continue
        raise ClaudeCodeCloudWalkerError(
            "fetch_failed",
            f"GET /v1/code/sessions/{session_id}/events returned "
            f"HTTP {response.status_code}: {response.text[:200]}",
        )
    raise ClaudeCodeCloudWalkerError(
        "rate_limited",
        (
            f"GET /v1/code/sessions/{session_id}/events sustained HTTP 429 "
            f"across {len(config.rate_limit_backoff_seconds)} retries"
        ),
    )


def _build_envelope(
    *, summary: dict[str, Any], events: list[dict[str, Any]],
) -> str:
    envelope = {
        "external_session_id": _summary_string(summary, "id"),
        "session_summary": summary,
        "events": events,
    }
    return json.dumps(envelope, ensure_ascii=False)


# ───────────────────────────────────────────────────────────────────────────
# Concurrent dispatch
# ───────────────────────────────────────────────────────────────────────────


async def fetch_and_dispatch_concurrent(
    *,
    summaries: Iterable[dict[str, Any]],
    config: WalkerConfig,
    creds: ClaudeCodeCloudCredentials,
    dispatch_callable: Callable[[str], None],
    client: httpx.AsyncClient,
    report: WalkerReport,
) -> None:
    """Fetch + dispatch each session with bounded concurrency."""
    semaphore = asyncio.Semaphore(config.walker_concurrency)
    auth_expired = asyncio.Event()

    async def _one(summary: dict[str, Any]) -> None:
        if auth_expired.is_set():
            return
        async with semaphore:
            if auth_expired.is_set():
                return
            session_id = _summary_string(summary, "id")
            try:
                envelope = await fetch_session_envelope(
                    client=client, config=config, creds=creds, summary=summary,
                )
            except ClaudeCodeCloudWalkerError as exc:
                if exc.code == "auth_expired":
                    auth_expired.set()
                    raise
                report.errored_count += 1
                report.errors.append(f"{session_id}: {exc.code}: {exc}")
                return
            except httpx.RequestError as exc:
                # Transient network error (read timeout, connection drop, etc.)
                # on a single session's events call: log as per-session failure
                # and continue the walk rather than aborting the whole run.
                report.errored_count += 1
                report.errors.append(
                    f"{session_id}: network_error: {type(exc).__name__}: {exc}",
                )
                return
            try:
                dispatch_callable(envelope)
            except ValueError as exc:
                report.errored_count += 1
                report.errors.append(f"{session_id}: parse_error: {exc}")
                return
            report.fetched_count += 1

    tasks = [asyncio.create_task(_one(summary)) for summary in summaries]
    if not tasks:
        return
    try:
        await asyncio.gather(*tasks)
    except ClaudeCodeCloudWalkerError:
        for task in tasks:
            if not task.done():
                task.cancel()
        raise


# ───────────────────────────────────────────────────────────────────────────
# Helpers
# ───────────────────────────────────────────────────────────────────────────


def _request_headers(
    creds: ClaudeCodeCloudCredentials, config: WalkerConfig,
) -> dict[str, str]:
    return {
        _HEADER_AUTHORIZATION: f"Bearer {creds.access_token}",
        _HEADER_ANTHROPIC_VERSION: config.anthropic_version,
        _HEADER_ANTHROPIC_BETA: config.anthropic_beta,
        _HEADER_USER_AGENT: _USER_AGENT_CLAUDE_CODE_CLOUD,
    }


def _raise_on_auth_failure(response: httpx.Response) -> None:
    if response.status_code == 401:
        raise ClaudeCodeCloudWalkerError(
            "auth_expired",
            "api.anthropic.com returned HTTP 401; re-run `claude auth login`.",
        )


def _summary_string(summary: dict[str, Any], key: str) -> str:
    value = summary.get(key)
    if not isinstance(value, str) or not value:
        raise ClaudeCodeCloudWalkerError(
            "summary_malformed",
            f"session summary missing string {key!r}",
        )
    return value


__all__ = [
    "ClaudeCodeCloudCredentials",
    "ClaudeCodeCloudWalkerError",
    "WalkerConfig",
    "WalkerReport",
    "fetch_and_dispatch_concurrent",
    "fetch_session_envelope",
    "list_all_session_summaries",
    "load_credentials",
]
