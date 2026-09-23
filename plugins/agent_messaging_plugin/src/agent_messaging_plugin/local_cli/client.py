"""Minimal synchronous client for a solet's localhost bridge surface.

Speaks the `/api/v1/bridge/*` contract the stdio MCP bridge forwards to,
but as one-shot request/response rather than a persistent MCP session.

Deliberately NOT the `Forwarder` from ``mcp_bridge/``: that owns a
persistent bridge session (peer-registry registration, a long-poll event
loop, an MCP notification write-stream). A one-shot call needs none of
that — ``process/call`` only requires the bridge to exist — so this client
opens a bridge, calls, polls the result, and closes, never registering a
peer identity and never polluting the registry.
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable
from pathlib import Path
from types import TracebackType
from typing import Any, Final
from urllib.parse import quote

import httpx
from ananta.core.config.environment_config import EnvironmentConfig
from ananta.core.root_manifest import MANIFEST_FILENAME, load_manifest
from ananta.core.runtime.port_manager import read_port_file

BRIDGE_SERVICE_NAME: Final[str] = "bridge"
API_PREFIX: Final[str] = "/api/v1/bridge"

DEFAULT_REQUEST_TIMEOUT_S: Final[float] = 30.0
DEFAULT_POLL_TIMEOUT_S: Final[float] = 120.0
POLL_INTERVAL_S: Final[float] = 0.5

# Born-async job awaiting (second hop). A job's real work is external — a
# Google API round trip, an export — so it is polled far more slowly than the
# ms-scale action dispatch above; 0.5s here would be pure noise against the
# platform.
JOB_POLL_INTERVAL_S: Final[float] = 2.0
DEFAULT_JOB_TIMEOUT_S: Final[float] = 300.0
GET_JOB_PROCESS_KEY: Final[str] = "service_interface::job_service::get_job"

# Job statuses that mean "still working" (AsyncJobManager's ledger vocabulary;
# its terminal set is completed / error / cancelled).
NON_TERMINAL_JOB_STATUSES: Final[frozenset[str]] = frozenset({"queued", "processing"})

# A genesis-born clone rewrites root_manifest.yaml's solet_name to its own
# name, but an unmaterialized source tree still carries this literal
# placeholder -- so it is the signal to use the clone-dir basename.
_NAME_PLACEHOLDER: Final[str] = "solet"

# Action statuses that mean "still working". A completed action is also still
# settling until process/result carries the separately persisted result row.
NON_TERMINAL_STATUSES: Final[frozenset[str]] = frozenset(
    {"queued", "pending", "dispatched", "processing", "running"},
)


def _envelope_data_sections(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """The places a verb's own return value can sit in a CLI result payload.

    There is no universal ``data`` key across producers (measured 2026-08-14):
    a plugin verb's envelope puts its return directly under ``result.data``
    (g_suite's ``_success``), while a service-interface verb nests it one
    deeper under ``result.data.result`` (JobService's KEY_DATA/KEY_RESULT).
    Both measured shapes are checked and NOTHING else — an unrecognized shape
    yields no sections, so the caller falls through to its normal behavior
    rather than acting on a guess.
    """
    sections: list[dict[str, Any]] = []
    result = payload.get("result")
    if not isinstance(result, dict):
        return sections
    data = result.get("data")
    if isinstance(data, dict):
        sections.append(data)
        inner = data.get("result")
        if isinstance(inner, dict):
            sections.append(inner)
    return sections


def queued_job_id(payload: dict[str, Any]) -> str | None:
    """The job id of a ``{job_id, status: queued}`` dispatch, else None.

    BOTH keys are required. A payload carrying a ``job_id`` without
    ``status == "queued"`` is something else — ``get_job``'s own answer, for
    one — and treating it as a fresh dispatch would start an await loop on a
    job that already finished.
    """
    for section in _envelope_data_sections(payload):
        job_id = section.get("job_id")
        if (
            isinstance(job_id, str)
            and job_id
            and str(section.get("status", "")) == "queued"
        ):
            return job_id
    return None


def _extract_job_record(payload: dict[str, Any]) -> dict[str, Any] | None:
    """The job record from a ``get_job`` result payload, or None."""
    for section in _envelope_data_sections(payload):
        job = section.get("job")
        if isinstance(job, dict):
            return job
    return None


class SoletNotRunningError(RuntimeError):
    """The solet bridge is not reachable (no port file present)."""


class SoletIdentityError(RuntimeError):
    """The CLI could not determine WHICH solet it is installed under — its
    clone's ``root_manifest.yaml`` is present but unreadable, so falling back to
    the clone-dir basename could silently retarget a corrupt/mis-seeded clone.
    """


class BridgeCallError(RuntimeError):
    """A bridge HTTP call returned an error status or an unusable body."""


class RoleClaimRejectedError(RuntimeError):
    """A role claim the server REFUSED, carrying its stable failure code.

    Distinct from ``BridgeCallError`` on purpose. The reconnect loop catches
    transport errors and retries; a claim refusal must be classified by its
    CODE before anything decides whether to retry or die, because the two
    outcomes are opposite and indistinguishable from the HTTP status alone
    (400 spans permanent and transient codes; 503 is transient).

    The code survives only on this INFRA route. The MODEL_INITIATED
    ``/process/call`` path runs through the action queue poller, which
    overwrites every plugin failure code with the constant ``action_failed``
    and nulls ``data`` — so a taxonomy keyed on that path cannot discriminate
    at all. That is why the watcher's arm-claim uses this route.
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


class BridgeResultTimeoutError(RuntimeError):
    """A dispatched action did not reach a terminal state within the budget."""


def _clone_root() -> Path | None:
    """The solet clone root: the nearest ancestor of this module carrying
    ``root_manifest.yaml``. ``None`` when the CLI runs outside a clone.
    """
    for parent in Path(__file__).resolve().parents:
        if (parent / MANIFEST_FILENAME).is_file():
            return parent
    return None


def resolve_solet_name() -> str:
    """The solet this CLI is installed under — derived from the CLI's OWN
    resolved install location, never a caller flag or ambient env, so a bare
    per-solet symlink (``~/.local/bin/<name>`` -> ``<clone>/.venv/bin/...``)
    pins its own solet and reaches no sibling.

    Precedence: the clone's genesis-rewritten ``root_manifest.yaml`` name; else
    the clone-dir basename (the birth/clone convention is ``~/Workspace/<name>/``);
    else ``$SOLET_NAME`` (when run outside any clone). A root_manifest that
    is PRESENT but unreadable (malformed / schema-invalid) fails loud rather than
    silently retargeting a corrupt clone by its directory name.
    """
    root = _clone_root()
    if root is None:
        return EnvironmentConfig.solet_name()
    manifest_path = root / MANIFEST_FILENAME
    manifest, error = load_manifest(manifest_path)
    if manifest is not None:
        # Genesis rewrites this to the newborn's name; an unmaterialized source
        # tree keeps the literal placeholder -> use the clone-dir basename.
        if manifest.solet_name != _NAME_PLACEHOLDER:
            return manifest.solet_name
        return root.name
    if manifest_path.is_file():
        raise SoletIdentityError(
            f"root_manifest at {manifest_path} is present but unreadable ({error}) "
            "— refusing to guess the solet identity by clone-dir basename."
        )
    # ABSENT root_manifest -> the clone-dir basename is the name.
    return root.name


def resolve_base_url(solet_name: str | None = None) -> str:
    """Discover the running solet's bridge base URL from its port file.

    Args:
        solet_name: Test-only override. Production resolves identity from
            the CLI's install location (:func:`resolve_solet_name`) — no
            ``-H`` flag, no ambient env — so each installed command (and any
            symlink to it) reaches ONLY its own solet.

    Returns:
        ``http://127.0.0.1:<port>`` for the discovered bridge port.

    Raises:
        SoletNotRunningError: No bridge port file exists for the solet.
    """
    name = solet_name or resolve_solet_name()
    port = read_port_file(BRIDGE_SERVICE_NAME, name)
    if port is None:
        raise SoletNotRunningError(
            f"no bridge port file for solet '{name}' "
            f"(~/.ananta/runtime/{name}.{BRIDGE_SERVICE_NAME}.port). "
            "Is the solet running?",
        )
    return f"http://127.0.0.1:{port}"


class BridgeClient:
    """One-shot synchronous client over a solet's bridge HTTP surface."""

    def __init__(
        self,
        base_url: str,
        *,
        transport: httpx.BaseTransport | None = None,
        request_timeout_s: float = DEFAULT_REQUEST_TIMEOUT_S,
        caller_agent_session_id: str = "",
    ) -> None:
        self._client = httpx.Client(
            base_url=base_url.rstrip("/"),
            timeout=request_timeout_s,
            transport=transport,
        )
        self._bridge_id: str | None = None
        self._caller_agent_session_id = caller_agent_session_id

    def __enter__(self) -> BridgeClient:
        self.open()
        return self

    def __exit__(
        self,
        _exc_type: type[BaseException] | None,
        _exc: BaseException | None,
        _tb: TracebackType | None,
    ) -> None:
        self.close()

    def open(self) -> str:
        """Open a bridge session and remember its id.

        ``caller_agent_session_id`` rides the open body as ATTRIBUTION ONLY: the
        server stores the opaque key on its in-memory bridge state and resolves
        it against the peer registry when stamping a send's sender, so a
        CLI-originated message is attributed to the session that ran the command
        instead of ``System (Scheduler)`` (§34.6). It is NOT a registration —
        registering this one-shot bridge under the caller's identity would sweep
        the caller's own registry row by ``session_label`` and then delete it
        again at close, black-holing the session's receive path.
        """
        body: dict[str, Any] = {"parent_pid": os.getpid()}
        if self._caller_agent_session_id:
            body["caller_agent_session_id"] = self._caller_agent_session_id
        payload = self._post(f"{API_PREFIX}/open", body)
        bridge_id = payload.get("bridge_id")
        if not isinstance(bridge_id, str):
            raise BridgeCallError(f"bridge open returned no bridge_id: {payload!r}")
        self._bridge_id = bridge_id
        return bridge_id

    def close(self) -> None:
        """Close the bridge (best-effort) and dispose the HTTP client.

        Teardown never masks a result already obtained by the caller, so an
        HTTP error while closing is swallowed — the httpx client is closed
        regardless.
        """
        bridge_id, self._bridge_id = self._bridge_id, None
        try:
            if bridge_id is not None:
                self._post(f"{API_PREFIX}/{bridge_id}/close", {})
        except httpx.HTTPError:
            pass
        finally:
            self._client.close()

    def call_and_wait(
        self,
        process_key: str,
        arguments: dict[str, Any],
        *,
        reason: str | None = None,
        poll_timeout_s: float = DEFAULT_POLL_TIMEOUT_S,
    ) -> dict[str, Any]:
        """Dispatch a process and poll until its result payload is available."""
        dispatch = self.process_call(process_key, arguments, reason=reason)
        action_id = dispatch.get("action_id")
        if not isinstance(action_id, str):
            raise BridgeCallError(
                f"process_call returned no action_id: {dispatch!r}",
            )
        return self.wait_for_result(action_id, poll_timeout_s=poll_timeout_s)

    def wait_for_result(
        self,
        action_id: str,
        *,
        poll_timeout_s: float = DEFAULT_POLL_TIMEOUT_S,
    ) -> dict[str, Any]:
        """Poll until the action is terminal and its completed result is stored.

        The action-event row is marked completed immediately before the result
        row is written. A snapshot in that narrow window has
        ``status=completed`` but no ``result`` key; treating it as terminal
        makes ``solet-bridge call`` nondeterministically omit successful output.
        """
        deadline = time.monotonic() + poll_timeout_s
        while True:
            payload = self.process_result(action_id)
            status = str(payload.get("status", ""))
            completed_result_pending = (
                status == "completed" and "result" not in payload
            )
            if (
                status not in NON_TERMINAL_STATUSES
                and not completed_result_pending
            ):
                return payload
            if time.monotonic() >= deadline:
                raise BridgeResultTimeoutError(
                    f"action {action_id} still '{status}' "
                    f"after {poll_timeout_s:.0f}s",
                )
            time.sleep(POLL_INTERVAL_S)

    def await_job(
        self,
        job_id: str,
        *,
        job_timeout_s: float = DEFAULT_JOB_TIMEOUT_S,
        poll_timeout_s: float = DEFAULT_POLL_TIMEOUT_S,
        on_poll: Callable[[str, float], None] | None = None,
    ) -> dict[str, Any]:
        """Poll a born-async job by id until it reaches a terminal status.

        The SECOND hop. ``call_and_wait`` above waits for the DISPATCH to
        return, which for a born-async verb is only the ``{job_id, status:
        queued}`` handle — the real work has not started. This polls
        ``get_job`` until the job itself is terminal, and returns the job
        record (result and error payloads already attached by the verb).

        ``on_poll`` receives ``(status, elapsed_s)`` per poll so the caller can
        show progress without this module deciding where progress belongs.

        Raises ``BridgeResultTimeoutError`` when the job is still non-terminal
        at the deadline — the job keeps running; only the waiting stops.
        """
        deadline = time.monotonic() + job_timeout_s
        started = time.monotonic()
        status = "unknown"
        while True:
            payload = self.call_and_wait(
                GET_JOB_PROCESS_KEY,
                {"job_id": job_id},
                reason=f"await job {job_id}",
                poll_timeout_s=poll_timeout_s,
            )
            job = _extract_job_record(payload)
            if job is not None:
                status = str(job.get("status", "unknown"))
                if status not in NON_TERMINAL_JOB_STATUSES:
                    return job
            if on_poll is not None:
                on_poll(status, time.monotonic() - started)
            if time.monotonic() >= deadline:
                raise BridgeResultTimeoutError(
                    f"job {job_id} still '{status}' after {job_timeout_s:.0f}s",
                )
            time.sleep(JOB_POLL_INTERVAL_S)

    def process_call(
        self,
        process_key: str,
        arguments: dict[str, Any],
        *,
        reason: str | None = None,
    ) -> dict[str, Any]:
        """Zero-inference dispatch; returns the correlation envelope."""
        body: dict[str, Any] = {"process_key": process_key, "arguments": arguments}
        if reason is not None:
            body["reason"] = reason
        return self._post(f"{API_PREFIX}/{self._require_bridge()}/process/call", body)

    def process_result(self, action_id: str) -> dict[str, Any]:
        """Snapshot-read a dispatched action's stored result."""
        bridge_id = self._require_bridge()
        return self._get(f"{API_PREFIX}/{bridge_id}/process/result/{action_id}")

    def process_search(self, query: str, max_results: int) -> dict[str, Any]:
        """Discover process keys by semantic query."""
        bridge_id = self._require_bridge()
        return self._post(
            f"{API_PREFIX}/{bridge_id}/process/search",
            {"query": query, "max_results": max_results},
        )

    def process_schema(self, process_key: str) -> dict[str, Any]:
        """Fetch a process's argument schema."""
        bridge_id = self._require_bridge()
        return self._post(
            f"{API_PREFIX}/{bridge_id}/process/schema",
            {"process_key": process_key},
        )

    def health(self) -> dict[str, Any]:
        """Read the bridge health endpoint (no open bridge required)."""
        return self._get(f"{API_PREFIX}/health")

    def events(self, after: int) -> dict[str, Any]:
        """Long-poll the bridge event queue for events after ``after`` (cursor).

        Blocks server-side up to the bridge's ``long_poll_timeout_seconds``
        (~25s), returning immediately when events arrive or an empty batch on
        timeout. Returns ``{"events": [...], "next_cursor": <int>}``; a watcher
        re-issues with ``next_cursor`` to stream continuously. Server cursors
        start at 0 and the route returns events with ``cursor > after``, so a
        fresh stream starts at ``-1`` to not drop the first event.
        """
        bridge_id = self._require_bridge()
        return self._get(f"{API_PREFIX}/{bridge_id}/events?after={after}")

    def peer_register(
        self,
        *,
        agent_id: str,
        agent_instance_id: str,
        session_label: str,
        agent_session_id: str,
        watcher_declared: bool = False,
        operator_tmux_host: str = "",
    ) -> dict[str, Any]:
        """Register this bridge as a peer identity (the receive prerequisite).

        Peer/channel messages deliver to REGISTERED identities only; an
        anonymous bridge long-polls an empty queue. ``agent_session_id`` is the
        stable per-logical-session carrier the reconnect self-refresh and
        ``peer_claim_role`` key on (REL-07) — pass the launcher-exported value,
        never a PID. ``watcher_declared`` (MSG-04/identity-unification,
        2026-08-20): True for `solet-bridge watch`'s own registration, so
        ``BridgeBinding.is_watcher`` still reads true when this caller
        registers under a ledger ``AGENT_INSTANCE_ID`` that carries none of
        the legacy ``agi-watch-`` prefix it used to infer that from.
        """
        bridge_id = self._require_bridge()
        response = self._post_or_reject(
            f"{API_PREFIX}/{bridge_id}/peer/register",
            {
                "agent_id": agent_id,
                "agent_instance_id": agent_instance_id,
                "session_label": session_label,
                "parent_pid": os.getpid(),
                "agent_session_id": agent_session_id,
                "watcher_declared": watcher_declared,
                "operator_tmux_host": operator_tmux_host,
            },
        )
        if operator_tmux_host:
            qualified = response.get("operator_codex_host")
            expected = {
                "host": "tmux", "host_ref": operator_tmux_host,
                "agent_runtime": "codex", "qualification": "verified_local_watcher",
            }
            if not isinstance(qualified, dict) or any(
                qualified.get(key) != value for key, value in expected.items()
            ):
                raise BridgeCallError("service did not qualify the requested operator Codex host")
        return response

    def peer_inbox(
        self,
        *,
        include_important: bool,
        limit: int,
        after: str | None = None,
        role_after: str | None = None,
        observer: bool = False,
    ) -> dict[str, Any]:
        """Read this registered identity's durable inbox (catch-up on start).

        The two cursors are INDEPENDENT and run in OPPOSITE directions — see
        ``_drain_inbox``, which is the only caller that pages. ``after`` is an
        ISO-8601 timestamp advancing FORWARD through the instance section;
        ``role_after`` is an opaque token walking BACKWARD through the role
        section. Passing one does nothing to the other's section.
        """
        bridge_id = self._require_bridge()
        query = [
            f"include_important={str(include_important).lower()}",
            f"limit={limit}",
        ]
        if after:
            query.append(f"after={quote(after, safe='')}")
        if role_after:
            query.append(f"role_after={quote(role_after, safe='')}")
        if observer:
            query.append("observer=true")
        return self._get(
            f"{API_PREFIX}/{bridge_id}/peer/inbox?" + "&".join(query),
        )

    def peer_claim_role(self, *, name: str, takeover: bool = False) -> dict[str, Any]:
        """Claim NAME over the INFRA bridge route, preserving the failure code.

        Body carries the role name and the optional one-shot ``takeover``:
        every IDENTITY field is read from this bridge's REGISTERED binding
        (REL-07), which is why `peer_register` must precede this call.
        ``takeover`` is an authorization for THIS request, not an identity, so
        the body is the right place for it — it is stored nowhere and cannot be
        replayed by a later reconnect.

        A refusal arrives as a non-2xx whose body is ``{"code", "message"}``.
        It is raised as ``RoleClaimRejectedError`` rather than going through
        ``_unwrap`` — ``_unwrap`` would flatten it into ``BridgeCallError``,
        which the reconnect loop treats as transport churn and retries forever,
        silently converting a designed refusal into an infinite loop.
        """
        bridge_id = self._require_bridge()
        path = f"{API_PREFIX}/{bridge_id}/peer/claim_role"
        response = self._client.post(path, json={"name": name, "takeover": takeover})
        if response.status_code >= httpx.codes.BAD_REQUEST:
            code, message = self._claim_failure(response)
            raise RoleClaimRejectedError(code, message)
        body = response.json()
        if not isinstance(body, dict):
            raise BridgeCallError(f"{path} returned a non-object body: {body!r}")
        return body

    @staticmethod
    def _claim_failure(response: httpx.Response) -> tuple[str, str]:
        """Pull ``{"code", "message"}`` off a refusal, degrading honestly.

        A body that is not the expected shape yields an empty code, which the
        caller's taxonomy treats as UNKNOWN -> transient. Inventing a code here
        would let a malformed response masquerade as a permanent refusal and
        kill a watcher for a parse failure.
        """
        try:
            body = response.json()
        except ValueError:
            return "", response.text[:200]
        if not isinstance(body, dict):
            return "", str(body)[:200]
        code = body.get("code")
        message = body.get("message")
        return (
            code if isinstance(code, str) else "",
            message if isinstance(message, str) else str(body)[:200],
        )

    def _post_or_reject(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        """POST, surfacing a `{"code","message"}` refusal as RoleClaimRejectedError.

        `_unwrap` would flatten a refusal into BridgeCallError, which the
        reconnect loop treats as transport churn and retries forever — so a
        PERMANENT register refusal (§4.3.3a's live-session-id conflict) would
        become an infinite silent loop. That is the same D2 inversion the claim
        path already fixes, one layer earlier.
        """
        response = self._client.post(path, json=body)
        if response.status_code >= httpx.codes.BAD_REQUEST:
            code, message = self._claim_failure(response)
            raise RoleClaimRejectedError(code, message)
        parsed = response.json()
        if not isinstance(parsed, dict):
            raise BridgeCallError(f"{path} returned a non-object body: {parsed!r}")
        return parsed

    def _require_bridge(self) -> str:
        if self._bridge_id is None:
            raise BridgeCallError("bridge not open; call open() first")
        return self._bridge_id

    def _post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        return self._unwrap(self._client.post(path, json=body), path)

    def _get(self, path: str) -> dict[str, Any]:
        return self._unwrap(self._client.get(path), path)

    @staticmethod
    def _unwrap(response: httpx.Response, path: str) -> dict[str, Any]:
        if response.status_code >= httpx.codes.BAD_REQUEST:
            raise BridgeCallError(
                f"{path} failed ({response.status_code}): {response.text}",
            )
        body = response.json()
        if not isinstance(body, dict):
            raise BridgeCallError(f"{path} returned a non-object body: {body!r}")
        return body
