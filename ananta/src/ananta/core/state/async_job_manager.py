from __future__ import annotations

import json
import logging
from copy import deepcopy
from typing import TYPE_CHECKING

from ananta.constants import (
    CONTEXT_KEY_FLOW_ID,
    CONTEXT_KEY_SESSION_ID,
    FRAMEWORK_ASYNC_JOBS_TABLE,
    FRAMEWORK_NAMESPACE,
    NOTES_MAX_LENGTH,
)
from ananta.core.domain.enums import ActionStatus
from ananta.core.domain.status import is_status_match
from ananta.core.domain.types import ActionResult
from ananta.core.state.execution_token_context import get_current_flow_token_id
from ananta.core.state.job_completion_reach import record_completion_reach
from ananta.core.state.job_completion_route import (
    build_delivery_action,
    resolve_route,
)
from ananta.interfaces.state_service_protocol import StateServiceProtocol

if TYPE_CHECKING:
    from ananta.core.actions.action_factory import ActionFactory
    from ananta.core.state.flow_runtime_graph import FlowRuntimeGraph

logger = logging.getLogger(__name__)


def _optional_str(source: dict[str, object], key: str) -> str | None:
    """A non-empty string field, or None — "" and a wrong type read the same."""
    value = source.get(key)
    return value if isinstance(value, str) and value else None


class AsyncJobManager:
    def __init__(
        self,
        state_service: StateServiceProtocol,
        flow_runtime_graph: FlowRuntimeGraph | None = None,
    ) -> None:
        self.state_service = state_service
        self._namespace = FRAMEWORK_NAMESPACE
        self._table = FRAMEWORK_ASYNC_JOBS_TABLE
        # FlowRuntimeGraph is set after initialization via ServiceManager
        # (due to initialization order - AsyncJobManager created before FRG)
        self._flow_runtime_graph: FlowRuntimeGraph | None = flow_runtime_graph
        self._action_factory: ActionFactory | None = None

    def create_job(
        self,
        plugin_name: str,
        action_name: str,
        request_data: dict[str, object] | None = None,
        job_id: str | None = None,
        name: str | None = None,
        description: str | None = None,
        flow_id_trace: str | None = None,
        job_metadata: dict[str, object] | None = None,
    ) -> dict[str, object]:
        """Create a new job in the unified job ledger with optional payload.

        Args:
            plugin_name: Name of the plugin creating the job
            action_name: Name of the action being executed
            request_data: Optional request payload data
            job_id: Optional external job identifier (maps to external_id column)
            name: Optional user-friendly job name (defaults to ID if not provided)
            description: Optional job description (defaults to auto-generated)
            flow_id_trace: Optional flow ID for tracing

        Returns:
            Result dict with job_id and status
        """
        if not self.state_service:
            return {
                "action_status": "error",
                "error": {"message": "State service not available"},
            }

        request_payload = request_data or {}
        notes_error = self._validate_notes(request_payload)
        if notes_error:
            return notes_error

        provider_name = f"{plugin_name}.{action_name}"
        job_record = self._build_job_record(
            provider_name, request_payload, job_id, name, description, flow_id_trace, job_metadata
        )

        try:
            return self._create_job_with_payload(provider_name, job_record, request_payload)
        except Exception as e:
            logger.error("Error creating async job: %s", e)
            return {
                "action_status": "error",
                "error": {"message": f"Exception creating job: {e!s}"},
            }

    def _validate_notes(self, request_payload: dict[str, object]) -> dict[str, object] | None:
        """Validate notes field in request payload. Returns error dict or None."""
        notes_value = ""
        notes_candidate = request_payload.get("notes")
        if isinstance(notes_candidate, str):
            notes_value = notes_candidate.strip()

        if not notes_value:
            return {
                "action_status": "error",
                "error": {"message": "AsyncJobManager.create_job requires a non-empty notes field"},
            }

        if len(notes_value) > NOTES_MAX_LENGTH:
            return {
                "action_status": "error",
                "error": {
                    "message": f"notes exceeds maximum length of {NOTES_MAX_LENGTH} characters",
                    "details": {"notes_length": len(notes_value)},
                },
            }

        # Normalize notes value in payload
        request_payload["notes"] = notes_value

        return None

    def _build_job_record(
        self,
        provider_name: str,
        request_payload: dict[str, object],
        job_id: str | None,
        name: str | None,
        description: str | None,
        flow_id_trace: str | None,
        job_metadata: dict[str, object] | None = None,
    ) -> dict[str, object]:
        """Build job record for insertion."""
        notes_value = request_payload.get("notes", "")
        job_record: dict[str, object] = {
            "provider_type": "plugin",
            "provider_name": provider_name,
            "description": description or f"Async job for {provider_name}",
            "status": "queued",
            "progress_percent": 0,
            "notes": notes_value,
        }

        if job_id:
            job_record["external_id"] = job_id
        if name:
            job_record["name"] = name
        if flow_id_trace:
            job_record["flow_id_trace"] = flow_id_trace

        # Link to FRG token from execution context
        flow_token_id = get_current_flow_token_id()
        if flow_token_id:
            job_record["flow_token_id"] = flow_token_id

        if job_metadata:
            try:
                job_record["metadata"] = json.dumps(job_metadata)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"job_metadata must be JSON serializable: {exc}") from exc

        return job_record

    def _create_job_with_payload(
        self,
        provider_name: str,
        job_record: dict[str, object],
        request_payload: dict[str, object],
    ) -> dict[str, object]:
        """Create job record and write payload. Assumes state_service is available."""
        assert self.state_service is not None  # Caller must verify

        result = self.state_service.write_state(
            namespace=self._namespace,
            data={"table": self._table, "record": job_record},
        )

        if not is_status_match(result.get("action_status"), ActionStatus.COMPLETED):
            logger.error("Failed to create async job: %s", result)
            return {
                "action_status": "error",
                "error": {"message": f"Failed to create job: {result.get('error', 'Unknown')}"},
            }

        generated_id = self._extract_generated_id(result)
        if not generated_id:
            logger.error("Job created but no ID returned for %s", provider_name)
            return {
                "action_status": "error",
                "error": {"message": "Failed to get generated job ID"},
            }

        if request_payload:
            self._write_request_payload(generated_id, request_payload)

        logger.debug("Created async job %s for %s", generated_id, provider_name)
        return {
            "action_status": "completed",
            "data": {"job_id": generated_id, "status": "queued"},
        }

    def _write_request_payload(self, job_id: str, request_payload: dict[str, object]) -> None:
        """Write request payload for a job. Logs warning on failure."""
        assert self.state_service is not None

        payload_result = self.state_service.write_state(
            namespace=self._namespace,
            data={
                "table": "job_payload",
                "record": {
                    "job_id": job_id,
                    "payload_type": "request",
                    "payload_data": json.dumps(request_payload),
                    "sequence": 1,
                },
            },
        )

        if not is_status_match(payload_result.get("action_status"), ActionStatus.COMPLETED):
            logger.error(
                "Job %s created but payload write failed: %s",
                job_id,
                payload_result.get("error"),
            )

    def _extract_generated_id(self, result: ActionResult) -> str | None:
        data = result.get("data")
        if not isinstance(data, dict):
            return None

        result_obj = data.get("result")
        if isinstance(result_obj, dict):
            generated_id = result_obj.get("generated_id")
            if isinstance(generated_id, str):
                return generated_id
        return None

    def get_job(self, job_id: str) -> dict[str, object]:
        """Get a job by ID.

        Returns:
            Result dict with job record in data.job on success.
        """
        if not self.state_service:
            return {
                "action_status": "error",
                "error": {"message": "State service not available"},
            }

        try:
            result = self.state_service.read_state(
                namespace=self._namespace,
                query={"table": self._table, "filters": {"id": job_id}, "limit": 1},
            )

            if not is_status_match(result.get("action_status"), ActionStatus.COMPLETED):
                return {"action_status": "error", "error": {"message": f"Job not found: {job_id}"}}

            data = result.get("data")
            if not isinstance(data, dict):
                return {"action_status": "error", "error": {"message": f"Job not found: {job_id}"}}

            # State service returns records directly in data, not nested under result
            records = data.get("records")
            if not isinstance(records, list) or not records:
                return {"action_status": "error", "error": {"message": f"Job not found: {job_id}"}}

            job = records[0]
            if not isinstance(job, dict):
                return {
                    "action_status": "error",
                    "error": {"message": f"Invalid job record: {job_id}"},
                }

            return {"action_status": "completed", "data": {"job": job}}

        except Exception as e:
            logger.error("Error getting async job %s: %s", job_id, e)
            return {
                "action_status": "error",
                "error": {"message": f"Exception getting job: {e!s}"},
            }

    def get_job_payload(self, job_id: str, payload_type: str = "request") -> dict[str, object]:
        """Get payload data for a job.

        Args:
            job_id: The job ID to fetch payload for.
            payload_type: Type of payload to fetch ('request', 'result', or 'error').

        Returns:
            Result dict with payload data in data.payload on success.
        """
        if not self.state_service:
            return {
                "action_status": "error",
                "error": {"message": "State service not available"},
            }

        try:
            result = self.state_service.read_state(
                namespace=self._namespace,
                query={
                    "table": "job_payload",
                    "filters": {"job_id": job_id, "payload_type": payload_type},
                    "order_by": "sequence DESC",
                    "limit": 1,
                },
            )

            payload_record = self._extract_first_record(result)
            if payload_record is None:
                return {
                    "action_status": "error",
                    "error": {"message": f"No {payload_type} payload found for job {job_id}"},
                }

            payload_data = self._parse_payload_data(payload_record, job_id)
            return {"action_status": "completed", "data": {"payload": payload_data}}

        except Exception as e:
            logger.error("Error getting payload for job %s: %s", job_id, e)
            return {
                "action_status": "error",
                "error": {"message": f"Exception getting payload: {e!s}"},
            }

    def _extract_first_record(self, result: ActionResult) -> dict[str, object] | None:
        """Extract first record from state service result, or None if not found."""
        if not is_status_match(result.get("action_status"), ActionStatus.COMPLETED):
            return None

        data = result.get("data")
        if not isinstance(data, dict):
            return None

        # State service returns records directly in data, not nested under result
        records = data.get("records")
        if not isinstance(records, list) or not records:
            return None

        first_record = records[0]
        return first_record if isinstance(first_record, dict) else None

    def _parse_payload_data(
        self, payload_record: dict[str, object], job_id: str
    ) -> dict[str, object]:
        """Parse payload_data field from a payload record."""
        payload_data_raw = payload_record.get("payload_data", "{}")

        if isinstance(payload_data_raw, str):
            try:
                parsed = json.loads(payload_data_raw)
                return parsed if isinstance(parsed, dict) else {}
            except json.JSONDecodeError:
                logger.error("Invalid JSON in payload for job %s", job_id)
                return {}

        return payload_data_raw if isinstance(payload_data_raw, dict) else {}

    def _build_job_filters(
        self,
        plugin_name: str | None,
        action_name: str | None,
        status: str | None,
    ) -> dict[str, object]:
        """Build filters dict for job queries."""
        filters: dict[str, object] = {}

        if plugin_name and action_name:
            filters["provider_name"] = f"{plugin_name}.{action_name}"
        elif plugin_name or action_name:
            logger.error(
                "Partial plugin/action filtering not supported. "
                "Provide both plugin_name and action_name, or use status filter only."
            )

        if status:
            filters["status"] = status

        return filters

    def list_jobs(
        self,
        status: str | None = None,
        provider_name: str | None = None,
        limit: int = 10,
        order_by: str = "created_at DESC",
    ) -> dict[str, object]:
        """List jobs matching the specified filters.

        Args:
            status: Filter by job status (e.g., 'queued', 'processing', 'completed').
            provider_name: Filter by provider name (e.g., 'plugin_name.action_name').
            limit: Maximum number of jobs to return.
            order_by: Sort order (default: 'created_at DESC').

        Returns:
            Result dict with list of jobs in data.jobs on success.
        """
        if not self.state_service:
            return {
                "action_status": "error",
                "error": {"message": "State service not available"},
            }

        filters: dict[str, object] = {}
        if status:
            filters["status"] = status
        if provider_name:
            filters["provider_name"] = provider_name

        try:
            result = self.state_service.read_state(
                namespace=self._namespace,
                query={
                    "table": self._table,
                    "filters": filters,
                    "order_by": order_by,
                    "limit": limit,
                },
            )

            if not is_status_match(result.get("action_status"), ActionStatus.COMPLETED):
                return {
                    "action_status": "error",
                    "error": {"message": "Failed to query jobs", "details": result},
                }

            data = result.get("data")
            if not isinstance(data, dict):
                return {"action_status": "completed", "data": {"jobs": []}}

            # State service returns records directly in data, not nested under result
            records = data.get("records")
            if not isinstance(records, list):
                return {"action_status": "completed", "data": {"jobs": []}}

            # Filter to valid job dicts only
            jobs = [r for r in records if isinstance(r, dict)]
            return {"action_status": "completed", "data": {"jobs": jobs}}

        except Exception as e:
            logger.error("Error listing jobs: %s", e)
            return {
                "action_status": "error",
                "error": {"message": f"Exception listing jobs: {e!s}"},
            }

    def update_job(self, job_id: str, updates: dict[str, object]) -> dict[str, object]:
        """Update job in unified ledger with support for payload fields."""
        try:
            if not self.state_service:
                return {
                    "action_status": "error",
                    "error": {"message": "State service not available"},
                }

            ledger_updates, result_payload, error_payload = self._extract_update_fields(updates)

            if ledger_updates:
                error_result = self._update_ledger(job_id, ledger_updates)
                if error_result:
                    return error_result

            next_sequence = self._get_next_payload_sequence(job_id)

            if result_payload is not None:
                next_sequence = self._write_payload(job_id, "result", result_payload, next_sequence)

            if error_payload is not None:
                self._write_payload(job_id, "error", error_payload, next_sequence)

            new_status = updates.get("status")
            if new_status in ("completed", "error", "cancelled"):
                # Stamped BEFORE the continuation is submitted, deliberately:
                # _handle_completion_actions can raise (a missing flow_id in
                # metadata is the concrete case), and a job whose continuation
                # never ran is exactly the one a reader most needs to find.
                #
                # Lane W: this pre-stamp is ALSO the push path's failure
                # fallback. It says "unreached" until the delivery verb
                # measures a successful hand-off and upgrades it, so a push
                # that never lands leaves the drain marker exactly where a
                # reader will find it. Losing both the push and the marker is
                # the one outcome that must be impossible.
                record_completion_reach(
                    self.state_service,
                    self._namespace,
                    self._table,
                    job_id,
                    self._fetch_job_metadata(job_id),
                )
                if not self._route_completion_to_role(
                    job_id, str(new_status), result_payload, error_payload
                ):
                    self._handle_completion_actions(
                        job_id,
                        str(new_status),
                        result_payload,
                        error_payload,
                    )
                # PRESERVED unconditionally across BOTH paths: routing changes
                # who hears about the job, never whether its flow closes. A
                # routed completion still resolves its FRG token.
                self._resolve_job_token(job_id, success=(new_status == "completed"))

            return {"action_status": "completed", "data": {"job_id": job_id, "updated": True}}

        except Exception as e:
            logger.error(f"Error updating async job {job_id}: {e}")
            return {
                "action_status": "error",
                "error": {"message": f"Exception updating job: {str(e)}"},
            }

    def _extract_update_fields(
        self, updates: dict[str, object]
    ) -> tuple[dict[str, object], dict[str, object] | None, dict[str, object] | None]:
        """Separate ledger updates from payload data."""
        ledger_updates: dict[str, object] = {}
        result_payload: dict[str, object] | None = None
        error_payload: dict[str, object] | None = None

        ledger_fields = ("status", "status_reason", "progress_percent", "completed_at")
        for field in ledger_fields:
            if field in updates:
                ledger_updates[field] = updates[field]

        if "result" in updates and isinstance(updates["result"], dict):
            result_payload = updates["result"]
        if "error" in updates and isinstance(updates["error"], dict):
            error_payload = updates["error"]

        return ledger_updates, result_payload, error_payload

    def _update_ledger(
        self, job_id: str, ledger_updates: dict[str, object]
    ) -> dict[str, object] | None:
        """Update job ledger record. Returns error dict on failure, None on success."""
        result = self.state_service.update_state(
            namespace=self._namespace,
            query={"table": self._table, "filters": {"id": job_id}},
            updates=ledger_updates,
        )

        if not is_status_match(result.get("action_status"), ActionStatus.COMPLETED):
            logger.error(f"Failed to update async job ledger {job_id}: {result}")
            return {
                "action_status": "error",
                "error": {
                    "message": f"Failed to update job ledger: {result.get('error', 'Unknown error')}"
                },
            }
        return None

    def _get_next_payload_sequence(self, job_id: str) -> int:
        """Get next sequence number for job payload records."""
        sequence_result = self.state_service.read_state(
            namespace=self._namespace,
            query={
                "table": "job_payload",
                "filters": {"job_id": job_id},
                "order_by": "sequence DESC",
                "limit": 1,
            },
        )

        if not is_status_match(sequence_result.get("action_status"), ActionStatus.COMPLETED):
            return 1

        data = sequence_result.get("data")
        if not isinstance(data, dict):
            return 1

        # State service returns records directly in data, not nested under result
        records = data.get("records")
        if not isinstance(records, list) or not records:
            return 1

        last_record = records[0]
        if not isinstance(last_record, dict):
            return 1

        last_seq = last_record.get("sequence")
        return last_seq + 1 if isinstance(last_seq, int) else 1

    def _write_payload(
        self, job_id: str, payload_type: str, payload_data: dict[str, object], sequence: int
    ) -> int:
        """Write a payload record and return next sequence number."""
        payload_result = self.state_service.write_state(
            namespace=self._namespace,
            data={
                "table": "job_payload",
                "record": {
                    "job_id": job_id,
                    "payload_type": payload_type,
                    "payload_data": json.dumps(payload_data),
                    "sequence": sequence,
                },
            },
        )
        if not is_status_match(payload_result.get("action_status"), ActionStatus.COMPLETED):
            logger.error(f"Failed to write {payload_type} payload for {job_id}: {payload_result}")
            return sequence
        return sequence + 1

    def _parse_process_key(self, process_key: str) -> tuple[str, str, str]:
        """Parse process_key into provider parts."""
        parts = process_key.split("::")
        if len(parts) != 3:
            raise ValueError(
                f"Invalid process_key '{process_key}'. Expected provider_type::provider::function_name"
            )
        return parts[0], parts[1], parts[2]

    def set_action_factory(self, action_factory: ActionFactory) -> None:
        """Inject ActionFactory for automatic result routing."""
        self._action_factory = action_factory
        logger.debug("AsyncJobManager: ActionFactory reference injected")

    def _decode_metadata(self, metadata_value: object) -> dict[str, object]:
        if isinstance(metadata_value, dict):
            return metadata_value
        if isinstance(metadata_value, str) and metadata_value.strip():
            try:
                parsed = json.loads(metadata_value)
                return parsed if isinstance(parsed, dict) else {}
            except json.JSONDecodeError:
                logger.error("AsyncJobManager: Invalid metadata JSON; ignoring")
        return {}

    def _fetch_job_metadata(self, job_id: str) -> dict[str, object]:
        """Load job metadata dict from database."""
        result = self.state_service.read_state(
            namespace=self._namespace,
            query={"table": self._table, "filters": {"id": job_id}, "limit": 1},
        )
        if not is_status_match(result.get("action_status"), ActionStatus.COMPLETED):
            return {}

        data = result.get("data")
        if not isinstance(data, dict):
            return {}

        records = data.get("records")
        if not isinstance(records, list) or not records:
            return {}

        record = records[0]
        if not isinstance(record, dict):
            return {}

        return self._decode_metadata(record.get("metadata"))

    def _fetch_job_provider_name(self, job_id: str) -> str:
        """The job's ``provider_name`` (``plugin.verb``), or "" when unreadable.

        Envelope provenance only — it names what produced the result in the
        delivered message. Never load-bearing for routing, so an unreadable row
        degrades to "" instead of suppressing a delivery.
        """
        result = self.state_service.read_state(
            namespace=self._namespace,
            query={"table": self._table, "filters": {"id": job_id}, "limit": 1},
        )
        record = self._extract_first_record(result)
        if record is None:
            return ""
        provider_name = record.get("provider_name")
        return provider_name if isinstance(provider_name, str) else ""

    def _get_completion_handler(
        self, job_id: str, status: str
    ) -> tuple[dict[str, object], dict[str, object], str] | None:
        """Retrieve completion handler from job metadata.

        Returns tuple of (handler, metadata, handler_key) or None if not found.
        """
        metadata = self._fetch_job_metadata(job_id)
        if not metadata:
            return None

        handlers = metadata.get("completion_handlers")
        if not isinstance(handlers, dict):
            return None

        handler_key = "result" if status == "completed" else "error"
        handler = handlers.get(handler_key)
        if not isinstance(handler, dict):
            return None

        process_key = handler.get("process_key")
        if not isinstance(process_key, str) or not process_key:
            return None

        return handler, metadata, handler_key

    def _build_completion_arguments(
        self,
        handler: dict[str, object],
        job_id: str,
        status: str,
        result_payload: dict[str, object] | None,
        error_payload: dict[str, object] | None,
    ) -> dict[str, object]:
        """Build arguments dict from handler template."""
        template = handler.get("template", {})
        if not isinstance(template, dict):
            template = {}

        arguments = deepcopy(template)
        params = self._ensure_dict_field(arguments, "params")
        arguments["params"] = params

        payload_key = "job_payload" if status == "completed" else "job_error"
        payload_value = result_payload if status == "completed" else error_payload
        params[payload_key] = payload_value or {}
        params["job_id"] = job_id
        params["job_status"] = status

        return arguments

    def _ensure_dict_field(self, container: dict[str, object], field: str) -> dict[str, object]:
        """Ensure field in container is a dict, return it."""
        value = container.setdefault(field, {})
        if not isinstance(value, dict):
            value = {}
            container[field] = value
        return value

    def _populate_state_args(
        self,
        arguments: dict[str, object],
        metadata: dict[str, object],
        job_id: str,
    ) -> tuple[dict[str, object], str | None, str | None]:
        """Populate state arguments from metadata. Returns (state_args, session_id, flow_id)."""
        state_args = self._ensure_dict_field(arguments, "state")
        arguments["state"] = state_args

        session_id: str | None = None
        flow_id: str | None = None

        session_id_raw = metadata.get("session_id")
        if isinstance(session_id_raw, str) and session_id_raw:
            session_id = session_id_raw
            state_args.setdefault("session_id", session_id)

        flow_id_raw = metadata.get("flow_id")
        if isinstance(flow_id_raw, str) and flow_id_raw:
            flow_id = flow_id_raw
            state_args.setdefault("flow_id", flow_id)

        state_args.setdefault("job_id", job_id)
        return state_args, session_id, flow_id

    def _build_action_definition(
        self,
        handler: dict[str, object],
        process_key: str,
        job_id: str,
        status: str,
        arguments: dict[str, object],
        session_id: str | None,
        flow_id: str,
    ) -> dict[str, object]:
        """Build action definition from handler and arguments.

        Args:
            handler: Handler definition with name, description, notes
            process_key: The process key for the action
            job_id: The async job ID
            status: Job status (completed, failed, etc.)
            arguments: Action arguments
            session_id: Optional session ID
            flow_id: Required flow ID (all actions require flow context)

        Returns:
            Complete action definition with flow_id
        """
        provider_type, provider, function_name = self._parse_process_key(process_key)
        action_def: dict[str, object] = {
            "name": handler.get("name") or f"{function_name}_{job_id}",
            "description": handler.get("description") or f"Auto-routing for job {job_id}",
            "process_key": process_key,
            "process": {
                "provider_type": provider_type,
                "provider": provider,
                "function_name": function_name,
            },
            "arguments": arguments,
            "notes": handler.get("notes") or f"Async job {job_id} {status}",
            CONTEXT_KEY_FLOW_ID: flow_id,
        }

        if session_id:
            action_def[CONTEXT_KEY_SESSION_ID] = session_id

        return action_def

    def _enrich_result_data(self, user_prompt: dict[str, object], job_id: str, status: str) -> None:
        """Enrich result_data section in user prompt."""
        result_section = user_prompt.setdefault("result_data", {})
        if isinstance(result_section, dict):
            result_section.setdefault("job_id", job_id)
            result_section.setdefault("status", status)

    def _enrich_action_result(
        self,
        user_prompt: dict[str, object],
        status: str,
        payload_value: dict[str, object] | None,
    ) -> None:
        """Enrich action_result section in user prompt."""
        action_status = "COMPLETED" if status == "completed" else status.upper()
        action_result = user_prompt.setdefault("action_result", {})
        if not isinstance(action_result, dict):
            return

        action_result.setdefault("action_status", action_status)
        data_section = action_result.setdefault("data", {})
        if isinstance(data_section, dict) and payload_value:
            for key, value in payload_value.items():
                data_section.setdefault(key, value)

    def _enrich_prompt_block(
        self,
        params: dict[str, object],
        job_id: str,
        status: str,
        result_payload: dict[str, object] | None,
        error_payload: dict[str, object] | None,
    ) -> None:
        """Enrich prompt block with job data.

        Populates both prompt.user (for ad-hoc inference templates) and
        prompt.observation (for the standard observation pipeline used by
        plan continuation). The observation dict enables plan step matching
        when async jobs complete.
        """
        prompt_block = params.get("prompt")
        if not isinstance(prompt_block, dict):
            return

        user_prompt = prompt_block.setdefault("user", {})
        if not isinstance(user_prompt, dict):
            return

        payload_value = result_payload if status == "completed" else error_payload

        self._enrich_result_data(user_prompt, job_id, status)
        self._enrich_action_result(user_prompt, status, payload_value)

        if payload_value:
            user_prompt.setdefault("job_payload", payload_value)
        if error_payload and status == "error":
            user_prompt.setdefault("job_error", error_payload)

        # Enrich observation dict if present (enables plan step matching)
        observation = prompt_block.get("observation")
        if isinstance(observation, dict):
            self._enrich_action_result(observation, status, payload_value)

    def _submit_completion_action(
        self, action_def: dict[str, object], handler_key: str, job_id: str, process_key: str
    ) -> None:
        """Submit action definition to action factory."""
        try:
            assert self._action_factory is not None
            self._action_factory.submit_action_definition(action_def)
            logger.debug(
                "AsyncJobManager: Submitted %s handler for job %s via %s",
                handler_key,
                job_id,
                process_key,
            )
        except Exception as exc:
            logger.error(
                "AsyncJobManager: Failed to submit completion handler for job %s: %s",
                job_id,
                exc,
                exc_info=True,
            )

    def _route_completion_to_role(
        self,
        job_id: str,
        status: str,
        result_payload: dict[str, object] | None,
        error_payload: dict[str, object] | None,
    ) -> bool:
        """Push this completion to its durable role, REPLACING the continuation.

        True means the delivery action was submitted and the caller must NOT
        also submit the plugin's continuation. False means no push route
        applied and today's behaviour stands unchanged.

        Replace, not duplicate, and only for a bridge-origin flow that carries a
        routing role. For such a flow the continuation does not reach the
        originator at all: with no inference-vertex binding it resolves DEFAULT,
        forwards to the ``sys:autonomic`` frontier holder, and — when that slot
        is vacant — DEFERS into the shared durable no-loss queue. Duplicating
        would therefore hand every routed completion to a future ``sys:autonomic``
        claimant's first drain as well, re-driving work whose owner was already
        told. Channel flows and role-less bridge flows are untouched.

        ERRORS route exactly like results: an owner wants a failure at least as
        much as a success, and the status travels in the envelope.
        """
        if not self._action_factory:
            return False
        metadata = self._fetch_job_metadata(job_id)
        route = resolve_route(self.state_service, self._namespace, metadata, status)
        if route is None:
            return False
        role, flow_id = route
        action_def = build_delivery_action(
            role=role,
            job_id=job_id,
            provider_name=self._fetch_job_provider_name(job_id),
            status=status,
            payload=result_payload if status == "completed" else error_payload,
            flow_id=flow_id,
            session_id=_optional_str(metadata, "session_id"),
        )
        try:
            self._action_factory.submit_action_definition(action_def)
        except Exception:  # noqa: BLE001 — fall back to the continuation, never drop
            logger.error(
                "AsyncJobManager: failed to submit role delivery for job %s to "
                "role %s; falling back to the completion continuation and "
                "leaving the unreached stamp in place",
                job_id,
                role,
                exc_info=True,
            )
            return False
        logger.info(
            "AsyncJobManager: job %s completion routed to role %s "
            "(continuation replaced)",
            job_id,
            role,
        )
        return True

    def _handle_completion_actions(
        self,
        job_id: str,
        status: str,
        result_payload: dict[str, object] | None,
        error_payload: dict[str, object] | None,
    ) -> None:
        """Submit configured completion or error actions when jobs finish."""
        if status not in {"completed", "error"}:
            return

        if not self._action_factory:
            return

        handler_data = self._get_completion_handler(job_id, status)
        if handler_data is None:
            return

        handler, metadata, handler_key = handler_data
        process_key = handler.get("process_key")
        assert isinstance(process_key, str)  # Validated in _get_completion_handler

        arguments = self._build_completion_arguments(
            handler, job_id, status, result_payload, error_payload
        )
        _, session_id, flow_id = self._populate_state_args(arguments, metadata, job_id)

        # Fail fast: flow_id is required for all actions
        if not flow_id:
            raise ValueError(
                f"Async job {job_id} completion handler missing flow_id in metadata - "
                "all actions require flow context"
            )

        action_def = self._build_action_definition(
            handler, process_key, job_id, status, arguments, session_id, flow_id
        )

        params = arguments.get("params", {})
        if isinstance(params, dict):
            self._enrich_prompt_block(params, job_id, status, result_payload, error_payload)

        self._submit_completion_action(action_def, handler_key, job_id, process_key)

    def _resolve_job_token(self, job_id: str, success: bool = True) -> None:
        """Resolve the FRG token linked to this job when it reaches terminal state.

        Token resolution triggers flow completion check via FlowRuntimeGraph.
        Fails fast if FRG not available or job not linked to token.
        """
        from ananta.error_handling import FrameworkError

        if not self._flow_runtime_graph:
            raise FrameworkError(
                message="FlowRuntimeGraph not initialized - cannot resolve job token",
                error_code="async_job_manager.frg_not_initialized",
                details={"job_id": job_id},
            )

        # Get this job's flow_token_id
        job_result = self.state_service.read_state(
            namespace=self._namespace,
            query={"table": self._table, "filters": {"id": job_id}, "limit": 1},
        )

        if not is_status_match(job_result.get("action_status"), ActionStatus.COMPLETED):
            raise FrameworkError(
                message=f"Failed to read job {job_id} for token resolution",
                error_code="async_job_manager.job_read_failed",
                details={"job_id": job_id, "result": dict(job_result)},
            )

        data = job_result.get("data")
        if not isinstance(data, dict):
            raise FrameworkError(
                message=f"Invalid job data structure for job {job_id}",
                error_code="async_job_manager.invalid_job_data",
                details={"job_id": job_id, "data": data},
            )

        # State service returns records directly in data, not nested under result
        records = data.get("records")
        if not isinstance(records, list) or not records:
            raise FrameworkError(
                message=f"Job {job_id} not found in database",
                error_code="async_job_manager.job_not_found",
                details={"job_id": job_id},
            )

        job = records[0]
        if not isinstance(job, dict):
            raise FrameworkError(
                message=f"Invalid job record format for job {job_id}",
                error_code="async_job_manager.invalid_record",
                details={"job_id": job_id, "record": job},
            )

        flow_token_id = job.get("flow_token_id")
        if not flow_token_id:
            # Job not linked to a token - this is valid for jobs created
            # outside action context (e.g., direct API calls)
            return

        # Resolve the token (triggers flow completion check)
        result_summary = {"job_id": job_id, "success": success}
        self._flow_runtime_graph.complete_token(
            str(flow_token_id), success=success, result_summary=result_summary
        )

    def _extract_count(self, result: dict[str, object]) -> int | None:
        """Extract count from SQL query result, returns None on error."""
        if not is_status_match(result.get("action_status"), ActionStatus.COMPLETED):
            return None

        data = result.get("data", {})
        if not isinstance(data, dict):
            return None

        records = data.get("records", [])
        if not isinstance(records, list) or not records:
            return None

        if not isinstance(records[0], list | tuple) or len(records[0]) == 0:
            return None

        count = records[0][0]
        return int(count) if count is not None else 0

    def get_most_recent_job(self) -> str | None:
        """Get the ID of the most recently created job."""
        if not self.state_service:
            logger.error("State service not available for job ID resolution")
            return None

        try:
            result = self.state_service.read_state(
                namespace=self._namespace,
                query={
                    "table": self._table,
                    "filters": {},
                    "order_by": "created_at DESC",
                    "limit": 1,
                },
            )

            if not is_status_match(result.get("action_status"), ActionStatus.COMPLETED):
                logger.error("Failed to query job ledger table: %s", result)
                return None

            data = result.get("data")
            if not isinstance(data, dict):
                logger.error("No jobs found in job ledger table")
                return None

            # State service returns records directly in data, not nested under result
            records = data.get("records")
            if not isinstance(records, list) or not records:
                logger.error("No jobs found in job ledger table")
                return None

            job = records[0]
            if not isinstance(job, dict):
                return None

            job_id = job.get("id")
            if not isinstance(job_id, str):
                logger.error("Job record missing id field: %s", job)
                return None

            return job_id

        except Exception as e:
            logger.error("Error getting most recent job: %s", e)
            return None

    def get_latest_job(
        self,
        plugin_name: str | None = None,
        action_name: str | None = None,
        status: str | None = None,
    ) -> dict[str, object]:
        """Return the most recently created job matching the optional filters.

        Note: plugin_name and action_name are combined into provider_name filter.
        """
        if not self.state_service:
            return {
                "action_status": "error",
                "error": {"message": "State service not available"},
            }

        filters = self._build_job_filters(plugin_name, action_name, status)

        try:
            result = self.state_service.read_state(
                namespace=self._namespace,
                query={
                    "table": self._table,
                    "filters": filters,
                    "order_by": "created_at DESC",
                    "limit": 1,
                },
            )

            job_record = self._extract_first_record(result)
            if job_record is None:
                return {
                    "action_status": "error",
                    "error": {
                        "message": "No jobs found matching filters",
                        "details": {
                            "plugin_name": plugin_name,
                            "action_name": action_name,
                            "status": status,
                        },
                    },
                }

            return {"action_status": "completed", "data": {"job": job_record}}

        except Exception as exc:
            logger.error("Error querying latest job: %s", exc)
            return {
                "action_status": "error",
                "error": {"message": f"Exception retrieving latest job: {exc}"},
            }
