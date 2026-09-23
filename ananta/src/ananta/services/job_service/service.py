"""Job Service Implementation.

Provides asynchronous job tracking and retrieval operations.
"""

import json
import logging
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, cast

from ananta.core.domain.constants import (
    KEY_ACTION_STATUS,
    KEY_DATA,
    KEY_RESULT,
    STATUS_COMPLETED,
    STATUS_ERROR,
)
from ananta.core.domain.timestamps import to_naive_utc
from ananta.core.state.job_completion_reach import (
    COMPLETION_REACH_KEY,
    REACH_BRIDGE_DISPATCH_NO_RETURN_PATH,
)
from ananta.interfaces.state_service_protocol import StateServiceProtocol
from ananta.services.job_service.interfaces.public import JobServiceAPI

if TYPE_CHECKING:
    from ananta.core.state.async_job_manager import AsyncJobManager

logger = logging.getLogger(__name__)

_TERMINAL_STATUSES: tuple[str, ...] = ("completed", "error", "cancelled")
"""Job statuses AsyncJobManager treats as terminal (``update_job``)."""

_MAX_UNREACHED_LIMIT = 100
"""Page cap for the unreached-completions read, mirroring query_ordered's."""

_TERMINAL_TOKEN_STATES: frozenset[str] = frozenset(
    {"completed", "failed", "cancelled", "aborted"}
)
"""FRG ``TokenState`` values that count as resolved (flow_runtime_graph)."""

_SWEEPABLE_STATUS = "processing"
"""The only status the staleness sweep will terminate.

Deliberately NOT ``queued``: a queued job may simply be waiting for a worker
that has not picked it up yet, which is a backlog, not a death. ``processing``
means a worker claimed the job and then stopped reporting.
"""

_SWEPT_STATUS_REASON_PREFIX = "swept_stale"
"""Marks a failure the SWEEP wrote, never one a worker reported.

A swept job must not be readable as a genuine worker-reported failure — the
distinction is the difference between "the work failed" and "nobody ever came
back to tell us", and the second must never be laundered into the first.
"""


def _payload_sequence(record: dict[str, Any]) -> int:
    """The payload row's ``sequence``, or -1 when it is missing/unparseable.

    -1 sorts BELOW every real sequence (which start at 0), so a malformed row
    can never win the "latest payload" comparison against a well-formed one.
    """
    raw = record.get("sequence")
    if isinstance(raw, int):
        return raw
    if isinstance(raw, str) and raw.strip().lstrip("-").isdigit():
        return int(raw)
    return -1


def _decode_json_object(payload_data: object) -> dict[str, Any] | None:
    """Decode a stored JSON column, which AsyncJobManager writes as a string.

    Tolerates an already-decoded dict (the in-memory bootstrap backend round-
    trips JSON columns as dicts). Returns None for anything that is not a JSON
    object, rather than inventing a shape the writer never wrote.
    """
    if isinstance(payload_data, dict):
        return cast(dict[str, Any], payload_data)
    if isinstance(payload_data, str) and payload_data.strip():
        try:
            parsed = json.loads(payload_data)
        except json.JSONDecodeError:
            logger.error("stored JSON column is not valid JSON; returning None")
            return None
        if isinstance(parsed, dict):
            return cast(dict[str, Any], parsed)
    return None


class JobService(JobServiceAPI):
    """Job service for tracking and retrieving asynchronous jobs."""

    def __init__(
        self,
        state_service: StateServiceProtocol,
        async_job_manager: "AsyncJobManager | None" = None,
    ):
        """Initialize job service.

        Args:
            state_service: State service instance for database access
            async_job_manager: Manager owning the terminal-transition path. The
                staleness sweep needs it and REFUSES to run without it, rather
                than writing a terminal status behind the manager's back — a
                direct ledger write would skip completion routing and leave the
                FRG token unresolved, which is the very defect the sweep exists
                to clear. Optional so every existing read-only construction
                keeps working untouched.
        """
        self.state_service = state_service
        self.async_job_manager = async_job_manager
        self.namespace = "core"

    def get_latest_job(
        self,
        plugin_name: str | None = None,
        action_name: str | None = None,
        status: str | None = None,
    ) -> dict[str, Any]:
        """Retrieve the most recently created asynchronous job.

        Service interface methods receive individual kwargs (action_processor pattern).
        Accepts optional filters for plugin_name, action_name, and status.

        Args:
            plugin_name: Filter to jobs from specific plugin
            action_name: Filter to jobs from specific action
            status: Filter to jobs with specific status

        Returns:
            ActionResult dict with job record or None if not found
        """
        try:
            job = self._query_latest_job(plugin_name, action_name, status)

            return {
                KEY_ACTION_STATUS: STATUS_COMPLETED,
                KEY_DATA: {
                    KEY_RESULT: {
                        "job": job,
                    }
                },
                "actions": [],
            }
        except Exception as e:
            logger.error(f"Failed to retrieve latest job: {e}", exc_info=True)
            return {
                KEY_ACTION_STATUS: STATUS_ERROR,
                KEY_DATA: {
                    KEY_RESULT: {
                        "error": str(e),
                        "job": None,
                    }
                },
                "actions": [],
            }

    def get_job(self, job_id: str) -> dict[str, Any]:
        """Retrieve one asynchronous job by its identifier.

        Service interface methods receive individual kwargs (action_processor pattern).

        Args:
            job_id: Identifier of the job to fetch (the ``job_id`` a born-async
                dispatch returned in its ``{job_id, status: queued}`` envelope)

        Returns:
            ActionResult dict with the job record, or ``job: None`` when no row
            carries that id
        """
        try:
            job = self._query_job_by_id(job_id)

            return {
                KEY_ACTION_STATUS: STATUS_COMPLETED,
                KEY_DATA: {
                    KEY_RESULT: {
                        "job": job,
                    }
                },
                "actions": [],
            }
        except Exception as e:
            logger.error(f"Failed to retrieve job {job_id!r}: {e}", exc_info=True)
            return {
                KEY_ACTION_STATUS: STATUS_ERROR,
                KEY_DATA: {
                    KEY_RESULT: {
                        "error": str(e),
                        "job": None,
                    }
                },
                "actions": [],
            }

    def list_unreached_job_completions(self, limit: int = 20) -> dict[str, Any]:
        """List finished jobs whose completion had no channel to arrive on.

        Service interface methods receive individual kwargs (action_processor pattern).

        Args:
            limit: Maximum jobs to return, newest first (1-100)

        Returns:
            ActionResult dict with the matching jobs and a count
        """
        try:
            jobs = self._query_unreached_completions(limit)

            return {
                KEY_ACTION_STATUS: STATUS_COMPLETED,
                KEY_DATA: {
                    KEY_RESULT: {
                        "jobs": jobs,
                        "count": len(jobs),
                    }
                },
                "actions": [],
            }
        except Exception as e:
            logger.error(f"Failed to list unreached job completions: {e}", exc_info=True)
            return {
                KEY_ACTION_STATUS: STATUS_ERROR,
                KEY_DATA: {
                    KEY_RESULT: {
                        "error": str(e),
                        "jobs": None,
                        "count": None,
                    }
                },
                "actions": [],
            }

    def _query_unreached_completions(self, limit: int) -> list[dict[str, Any]]:
        """Terminal jobs stamped as dispatched by a bridge, newest first.

        The stamp is written by ``AsyncJobManager._record_completion_reach``
        into the job's ``metadata`` JSON. A JSON-subfield predicate is not
        expressible in the state filter grammar (equality / ``= ANY`` /
        null-test / range only), so terminal status is filtered at the store
        and the stamp is matched in Python — the same read-then-route split
        ``_query_latest_job`` already uses for its provider-name prefix.

        An UNSTAMPED job is never included: absent means unmeasured, and a
        list of jobs that "might" have been unreachable would be a guess
        wearing the same name as a measurement.
        """
        if limit < 1 or limit > _MAX_UNREACHED_LIMIT:
            raise ValueError(
                f"limit must be between 1 and {_MAX_UNREACHED_LIMIT}; got {limit}"
            )
        rows = self._read_jobs({"status": list(_TERMINAL_STATUSES)})
        unreached = [
            r for r in rows
            if (_decode_json_object(r.get("metadata")) or {}).get(COMPLETION_REACH_KEY)
            == REACH_BRIDGE_DISPATCH_NO_RETURN_PATH
        ]
        unreached.sort(key=lambda r: (to_naive_utc(r["created_at"]), str(r["id"])), reverse=True)
        return [self._attach_payloads(r) for r in unreached[:limit]]

    def detect_unresolved_completion_tokens(self, limit: int = 20) -> dict[str, Any]:
        """Report finished jobs whose flow token was never resolved.

        Service interface methods receive individual kwargs (action_processor pattern).

        Args:
            limit: Maximum jobs to return, newest first (1-100)

        Returns:
            ActionResult dict with the matching jobs and a count
        """
        try:
            jobs = self._query_unresolved_completion_tokens(limit)

            return {
                KEY_ACTION_STATUS: STATUS_COMPLETED,
                KEY_DATA: {
                    KEY_RESULT: {
                        "jobs": jobs,
                        "count": len(jobs),
                    }
                },
                "actions": [],
            }
        except Exception as e:
            logger.error(f"Failed to detect unresolved completion tokens: {e}", exc_info=True)
            return {
                KEY_ACTION_STATUS: STATUS_ERROR,
                KEY_DATA: {
                    KEY_RESULT: {
                        "error": str(e),
                        "jobs": None,
                        "count": None,
                    }
                },
                "actions": [],
            }

    def _query_unresolved_completion_tokens(self, limit: int) -> list[dict[str, Any]]:
        """Terminal jobs whose ``flow_token_id`` is still non-terminal.

        READ-ONLY BY DESIGN, and that is the whole point. ``update_job`` writes
        the ledger's terminal status BEFORE it calls ``_handle_completion_actions``
        and ``_resolve_job_token``, all inside one try — so a handler that raises
        leaves a row that READS completed while nothing was submitted and its
        token never resolved. Such a row does not look stuck, which is exactly
        why the staleness sweep (built for jobs that never finished) cannot find
        it.

        This verb reports those rows and changes nothing. Resolving the token
        here would manufacture a completion no worker ever produced; the row is
        evidence of a defect, and the defect is what wants fixing.
        """
        if limit < 1 or limit > _MAX_UNREACHED_LIMIT:
            raise ValueError(
                f"limit must be between 1 and {_MAX_UNREACHED_LIMIT}; got {limit}"
            )
        rows = [
            r for r in self._read_jobs({"status": list(_TERMINAL_STATUSES)})
            if isinstance(r.get("flow_token_id"), str) and r["flow_token_id"]
        ]
        unresolved = [
            r for r in rows if self._token_is_unresolved(str(r["flow_token_id"]))
        ]
        unresolved.sort(key=lambda r: (to_naive_utc(r["created_at"]), str(r["id"])), reverse=True)
        return unresolved[:limit]

    def _token_is_unresolved(self, token_id: str) -> bool:
        """Whether this flow token EXISTS and sits in a non-terminal state.

        Both negative cases answer False on purpose. A MISSING token row is
        absent evidence, not evidence of the defect. An unreadable ``state`` is
        a different data problem, and reporting it here would file it under the
        wrong defect. Only a token that is present and demonstrably unresolved
        earns a place in the detector's output.
        """
        result = self.state_service.query_state(
            self.namespace, {"table": "flow_tokens", "filters": {"id": token_id}}
        )
        if result.get(KEY_ACTION_STATUS) != STATUS_COMPLETED:
            raise RuntimeError(f"token query did not complete: {result.get('error')!r}")
        data_obj = result.get(KEY_DATA)
        if not isinstance(data_obj, dict):
            raise RuntimeError(f"token query returned malformed data: {data_obj!r}")
        records = data_obj.get("records")
        if not isinstance(records, list) or not records:
            return False
        record = records[0]
        if not isinstance(record, dict):
            raise RuntimeError(f"token query returned a non-dict record: {record!r}")
        state = record.get("state")
        if not isinstance(state, str):
            logger.warning(
                "flow token %s has a non-string state %r; not counted as unresolved",
                token_id,
                state,
            )
            return False
        return state not in _TERMINAL_TOKEN_STATES

    def sweep_stale_jobs(
        self,
        max_age_minutes: int,
        plugin_name: str | None = None,
        limit: int = 20,
    ) -> dict[str, Any]:
        """Fail jobs stuck in ``processing`` past a caller-supplied window.

        Service interface methods receive individual kwargs (action_processor pattern).

        Args:
            max_age_minutes: How stale a processing job must be to be swept.
                REQUIRED — no default window is invented here, because the right
                window is per-plugin knowledge (a 20-second Sheets write and a
                40-minute export are both healthy) and a platform-wide guess
                would terminate live work.
            plugin_name: Sweep only this plugin's jobs
            limit: Maximum jobs to sweep in one call (1-100)

        Returns:
            ActionResult dict with the swept job ids and a count
        """
        try:
            swept = self._sweep_stale(max_age_minutes, plugin_name, limit)

            return {
                KEY_ACTION_STATUS: STATUS_COMPLETED,
                KEY_DATA: {
                    KEY_RESULT: {
                        "swept": swept,
                        "count": len(swept),
                    }
                },
                "actions": [],
            }
        except Exception as e:
            logger.error(f"Failed to sweep stale jobs: {e}", exc_info=True)
            return {
                KEY_ACTION_STATUS: STATUS_ERROR,
                KEY_DATA: {
                    KEY_RESULT: {
                        "error": str(e),
                        "swept": None,
                        "count": None,
                    }
                },
                "actions": [],
            }

    def _sweep_stale(
        self, max_age_minutes: int, plugin_name: str | None, limit: int
    ) -> list[dict[str, Any]]:
        """Terminate stale ``processing`` rows through the manager's own path.

        Staleness is measured on ``updated_at``, which is the honest signal: a
        live worker calls ``update_status`` for progress, so its row keeps
        moving. A row that has not moved is one nobody is reporting on.

        Each sweep goes through ``AsyncJobManager.update_job``, so the job's
        ERROR completion handler fires and its FRG token resolves — that
        resolution is the point, since an unresolved token is what leaves the
        originating flow open forever. DISCLOSED CONSEQUENCE: every swept job
        therefore submits a continuation action, so a large sweep is an
        inference burst; ``limit`` is what bounds it, and it is why the cap is
        low by default rather than unbounded.
        """
        if self.async_job_manager is None:
            raise RuntimeError(
                "sweep_stale_jobs requires an AsyncJobManager; refusing to write a "
                "terminal status directly, which would skip completion routing and "
                "leave the flow token unresolved"
            )
        if max_age_minutes < 1:
            raise ValueError(f"max_age_minutes must be at least 1; got {max_age_minutes}")
        if limit < 1 or limit > _MAX_UNREACHED_LIMIT:
            raise ValueError(
                f"limit must be between 1 and {_MAX_UNREACHED_LIMIT}; got {limit}"
            )

        cutoff = datetime.now(UTC).replace(tzinfo=None) - timedelta(minutes=max_age_minutes)
        filters: dict[str, object] = {
            "status": _SWEEPABLE_STATUS,
            "updated_at": {"op": "lt", "value": cutoff},
        }
        rows = self._read_jobs(filters)
        if plugin_name:
            prefix = f"{plugin_name}."
            rows = [r for r in rows if str(r.get("provider_name", "")).startswith(prefix)]
        rows.sort(key=lambda r: (to_naive_utc(r["created_at"]), str(r["id"])))

        swept: list[dict[str, Any]] = []
        for row in rows[:limit]:
            job_id = str(row["id"])
            reason = (
                f"{_SWEPT_STATUS_REASON_PREFIX}: no progress reported for over "
                f"{max_age_minutes} minute(s); last update {row.get('updated_at')!s}. "
                "Terminated by the staleness sweep, NOT reported by the worker."
            )
            result = self.async_job_manager.update_job(
                job_id, {"status": "error", "status_reason": reason}
            )
            swept.append(
                {
                    "job_id": job_id,
                    "provider_name": row.get("provider_name"),
                    "last_updated_at": str(row.get("updated_at")),
                    "status_reason": reason,
                    "update_accepted": result.get("action_status") == STATUS_COMPLETED,
                }
            )
        return swept

    def _attach_payloads(self, job: dict[str, Any]) -> dict[str, Any]:
        """Return the job row with its stored ``result`` / ``error`` payloads.

        MEASURED 2026-08-14 (live ``get_latest_job`` call): the ``core__job``
        row carries NO ``result`` and NO ``error`` column — its keys are
        id/provider_name/status/metadata/timestamps and friends. Payloads are
        written to the separate ``core__job_payload`` table by
        ``AsyncJobManager._write_payload``, one row per payload_type with an
        incrementing ``sequence``. A caller handed only the ledger row
        therefore cannot see the outcome it came for, which is the entire
        point of fetching a finished job.

        The two keys are added under exactly the names the job-retrieval
        guidance already uses, and are ``None`` when no payload of that type
        was ever written (a queued or running job, or a job that failed
        before producing one).
        """
        enriched = dict(job)
        enriched["result"] = self._latest_payload(str(job["id"]), "result")
        enriched["error"] = self._latest_payload(str(job["id"]), "error")
        return enriched

    def _latest_payload(self, job_id: str, payload_type: str) -> dict[str, Any] | None:
        """Highest-``sequence`` payload of ``payload_type`` for a job, or None.

        FAIL-FAST on a malformed envelope, matching :meth:`_read_jobs`: a
        failed payload read must not be spelled the same way as "this job
        produced no payload".
        """
        result = self.state_service.query_state(
            self.namespace,
            {
                "table": "job_payload",
                "filters": {"job_id": job_id, "payload_type": payload_type},
            },
        )
        if result.get(KEY_ACTION_STATUS) != STATUS_COMPLETED:
            raise RuntimeError(f"payload query did not complete: {result.get('error')!r}")
        data_obj = result.get(KEY_DATA)
        if not isinstance(data_obj, dict):
            raise RuntimeError(f"payload query returned malformed data: {data_obj!r}")
        records = data_obj.get("records")
        if not isinstance(records, list):
            raise RuntimeError(f"payload query returned malformed records: {records!r}")
        rows = [r for r in records if isinstance(r, dict)]
        if not rows:
            return None
        latest = max(rows, key=lambda r: _payload_sequence(r))
        return _decode_json_object(latest.get("payload_data"))

    def _query_job_by_id(self, job_id: str) -> dict[str, Any] | None:
        """The job row carrying ``job_id``, or None.

        FAIL-FAST on a blank id: an empty filter value would read as "no job
        found" while actually meaning "the caller never supplied an id" — the
        two answers must not be spelled the same way. A well-formed id that
        matches nothing is the legitimate ``None``.

        ``id`` is the primary key, so at most one row can match; the read still
        goes through :meth:`_read_jobs`, which validates row shape and raises
        rather than letting a malformed envelope masquerade as an empty answer.
        """
        if not job_id or not job_id.strip():
            raise ValueError("job_id is required and must be a non-empty string")
        rows = self._read_jobs({"id": job_id})
        if not rows:
            return None
        return self._attach_payloads(rows[0])

    def _query_latest_job(
        self,
        plugin_name: str | None,
        action_name: str | None,
        status: str | None,
    ) -> dict[str, Any] | None:
        """Most-recently-created job matching the filters, via read-then-route.

        The exact ``provider_name`` and ``status`` filters are equality and go to
        ``query_state``; the ``provider_name LIKE 'plugin.%'`` prefix-match is NOT
        expressible in the equality/``=ANY``/``is_null`` grammar, so it is applied
        in Python after the read. ``ORDER BY created_at DESC LIMIT 1`` becomes a
        Python max by ``(created_at, id)`` — ``created_at`` coerced to a VALUE
        (never compared as an ISO spelling), with ``id`` as a deterministic
        tie-break (the raw query had no secondary sort, so equal-``created_at``
        ties were nondeterministic). No ``is_deleted`` filter — the raw query had
        none, so the behavior is preserved.
        """
        filters, provider_prefix = self._build_job_filters(plugin_name, action_name, status)
        rows = self._read_jobs(filters)
        if provider_prefix is not None:
            rows = [
                r for r in rows
                if str(r.get("provider_name", "")).startswith(provider_prefix)
            ]
        if not rows:
            return None
        rows.sort(key=lambda r: (to_naive_utc(r["created_at"]), str(r["id"])), reverse=True)
        return self._attach_payloads(rows[0])

    @staticmethod
    def _build_job_filters(
        plugin_name: str | None, action_name: str | None, status: str | None
    ) -> tuple[dict[str, object], str | None]:
        """Split the request into equality filters + an optional Python prefix.

        Returns ``(filters, provider_prefix)``: ``filters`` are grammar-expressible
        equality predicates; ``provider_prefix`` (when set) is the ``plugin.``
        prefix the caller applies in Python (the LIKE branch).
        """
        filters: dict[str, object] = {}
        if status:
            filters["status"] = status
        if plugin_name and action_name:
            filters["provider_name"] = f"{plugin_name}.{action_name}"
            return filters, None
        if plugin_name:
            return filters, f"{plugin_name}."
        return filters, None

    def _read_jobs(self, filters: dict[str, object]) -> list[dict[str, Any]]:
        """Read ``core__job`` rows matching the equality filters (uncapped).

        FAIL-FAST: a non-completed or malformed envelope (or a non-dict record)
        RAISES — ``get_latest_job`` catches it into a ``STATUS_ERROR`` result, so a
        DB error can never masquerade as a valid "no job found". An empty list is
        returned ONLY for a valid completed result with zero matching rows.
        """
        result = self.state_service.query_state(
            self.namespace, {"table": "job", "filters": filters}
        )
        if result.get(KEY_ACTION_STATUS) != STATUS_COMPLETED:
            raise RuntimeError(f"job query did not complete: {result.get('error')!r}")
        data_obj = result.get(KEY_DATA)
        if not isinstance(data_obj, dict):
            raise RuntimeError(f"job query returned malformed data: {data_obj!r}")
        records = data_obj.get("records")
        if not isinstance(records, list):
            raise RuntimeError(f"job query returned malformed records: {records!r}")
        return [self._validated_job_row(r) for r in records]

    @staticmethod
    def _validated_job_row(record: object) -> dict[str, Any]:
        """Require the fields the prefix-filter + ``(created_at, id)`` sort read.

        FAIL-FAST: a record missing a string ``id`` / string ``provider_name`` /
        coercible ``created_at`` RAISES — it must not be silently dropped by the
        downstream ``provider_name`` prefix-filter (which would read as a valid
        "no job found").
        """
        if not isinstance(record, dict):
            raise RuntimeError(f"job query returned a non-dict record: {record!r}")
        if not isinstance(record.get("id"), str):
            raise RuntimeError(f"job record missing a string 'id': {record!r}")
        if not isinstance(record.get("provider_name"), str):
            raise RuntimeError(f"job record missing a string 'provider_name': {record!r}")
        if not isinstance(record.get("created_at"), (str, datetime)):
            raise RuntimeError(f"job record has a non-coercible 'created_at': {record!r}")
        return cast(dict[str, Any], record)
