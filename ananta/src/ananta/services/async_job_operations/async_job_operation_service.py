"""Async Job Operation Service.

Provides centralized asynchronous job management functionality extracted from StateService.
Handles creation, retrieval, and updates of async job records through database operations.
"""

import logging

from ananta.constants import NOTES_MAX_LENGTH
from ananta.core.domain.types import ActionResult
from ananta.services.database_operations import DatabaseOperationService

logger = logging.getLogger(__name__)


class AsyncJobOperationService:
    """Service for managing asynchronous job operations.

    Provides focused async job CRUD operations through database delegation.
    Extracted from StateService to achieve better separation of concerns.
    """

    def __init__(self, database_operation_service: DatabaseOperationService) -> None:
        """Initialize async job operation service.

        Args:
            database_operation_service: Service for database operations delegation
        """
        self._database_operation_service = database_operation_service
        logger.debug("AsyncJobOperationService initialized with database delegation")

    def create_async_job(
        self,
        job_id: str,
        provider_type: str,
        provider: str,
        action_name: str,
        request_data: dict[str, object],
        name: str | None = None,
        description: str | None = None,
    ) -> ActionResult:
        """Create a new async job entry using unified job ledger.

        Args:
            job_id: External identifier for the job (optional, system generates internal id)
            provider_type: Type of provider ("service_interface", "plugin", or "external_api")
            provider: Name of the provider handling the job
            action_name: Name of the action being executed
            request_data: Job request parameters and metadata
            name: Optional user-friendly job name (uses standard 'name' field, defaults to ID if not provided)
            description: Optional job description (defaults to auto-generated description)

        Returns:
            ActionResult indicating success or failure
        """
        provider_name = f"{provider}.{action_name}"
        logger.debug(f"Creating async job: {job_id} (provider_name={provider_name})")

        notes_value = self._extract_notes(request_data)
        validation_error = self._validate_notes(notes_value, provider_name)
        if validation_error:
            return validation_error

        job_record = self._build_job_record(
            job_id, provider_type, provider_name, description, notes_value, name
        )

        result = self._database_operation_service.write_state(
            namespace="core",
            enhanced_data={"table": "job", "record": job_record},
        )

        if result.get("action_status") == "completed" and request_data:
            self._write_job_payload(result, request_data)

        return result

    def _extract_notes(self, request_data: dict[str, object]) -> str:
        """Extract notes value from request data.

        Args:
            request_data: Job request parameters

        Returns:
            Stripped notes string or empty string
        """
        notes_candidate = request_data.get("notes")
        if isinstance(notes_candidate, str):
            return notes_candidate.strip()
        return ""

    def _validate_notes(self, notes_value: str, provider_name: str) -> ActionResult | None:
        """Validate notes field.

        Args:
            notes_value: Extracted notes string
            provider_name: Provider name for error context

        Returns:
            Error ActionResult if invalid, None if valid
        """
        if not notes_value:
            return self._notes_error(
                "MISSING_NOTES",
                "Async job request_data must include a non-empty notes field",
                provider_name,
            )

        if len(notes_value) > NOTES_MAX_LENGTH:
            return self._notes_error(
                "NOTES_TOO_LONG",
                f"notes exceeds maximum length of {NOTES_MAX_LENGTH} characters",
                provider_name,
                notes_length=len(notes_value),
            )

        return None

    def _notes_error(
        self, code: str, message: str, provider_name: str, **extra_details: object
    ) -> ActionResult:
        """Build notes validation error result.

        Args:
            code: Error code
            message: Error message
            provider_name: Provider name for context
            **extra_details: Additional details to include

        Returns:
            Error ActionResult
        """
        from datetime import UTC, datetime

        from ananta.core.domain.types import ErrorDetail

        details: dict[str, object] = {"provider_name": provider_name}
        details.update(extra_details)

        error_detail: ErrorDetail = {
            "type": "ValidationError",
            "code": code,
            "message": message,
            "details": details,
            "severity": "error",
            "timestamp": datetime.now(UTC).isoformat(),
        }
        return {"action_status": "error", "error": error_detail}

    def _build_job_record(
        self,
        job_id: str,
        provider_type: str,
        provider_name: str,
        description: str | None,
        notes_value: str,
        name: str | None,
    ) -> dict[str, object]:
        """Build job record dict.

        Args:
            job_id: External job identifier
            provider_type: Type of provider
            provider_name: Provider.action name
            description: Optional description
            notes_value: Validated notes value
            name: Optional job name

        Returns:
            Job record dict for database insertion
        """
        job_record: dict[str, object] = {
            "external_id": job_id if job_id else None,
            "provider_type": provider_type,
            "provider_name": provider_name,
            "description": description if description else f"Async job for {provider_name}",
            "status": "queued",
            "progress_percent": 0,
            "notes": notes_value,
        }
        if name:
            job_record["name"] = name
        return job_record

    def _write_job_payload(self, job_result: ActionResult, request_data: dict[str, object]) -> None:
        """Write job payload after successful job creation.

        Args:
            job_result: Result from job creation containing generated_id
            request_data: Request data to store as payload
        """
        generated_id = self._extract_generated_id(job_result)
        if not generated_id:
            return

        import json

        payload_result = self._database_operation_service.write_state(
            namespace="core",
            enhanced_data={
                "table": "job_payload",
                "record": {
                    "job_id": generated_id,
                    "payload_type": "request",
                    "payload_data": json.dumps(request_data),
                    "sequence": 1,
                },
            },
        )
        if payload_result.get("action_status") != "completed":
            logger.error(f"Job {generated_id} created but payload write failed: {payload_result}")

    def _extract_generated_id(self, job_result: ActionResult) -> str | None:
        """Extract generated_id from job creation result.

        Args:
            job_result: Result from job creation

        Returns:
            Generated ID string or None if not found
        """
        data = job_result.get("data", {})
        result_obj = data.get("result", {})
        if not isinstance(result_obj, dict):
            return None
        generated_id = result_obj.get("generated_id")
        return str(generated_id) if generated_id else None

    def get_async_job(self, job_id: str) -> ActionResult:
        """Retrieve async job by job ID.

        Args:
            job_id: Unique identifier for the job to retrieve

        Returns:
            ActionResult with job data or error information
        """

        return self._database_operation_service.read_state(
            namespace="core",
            query={
                "table": "job",
                "filters": {"id": job_id},
                "limit": 1,
            },
        )

    def update_async_job(self, job_id: str, updates: dict[str, object]) -> ActionResult:
        """Update async job with new data.

        Args:
            job_id: Unique identifier for the job to update
            updates: Dictionary of fields to update

        Returns:
            ActionResult indicating update success or failure
        """
        logger.debug(f"Updating async job: {job_id} with {len(updates)} field(s)")

        # Only update ledger fields - payload writes are handled by AsyncJobManager
        ledger_fields = {
            "status",
            "status_reason",
            "progress_percent",
            "expected_completion_at",
            "completed_at",
            "provider_type",
            "provider_name",
            "name",
            "description",
            "group_id",
            "conversation_id",
            "metadata",
        }

        ledger_updates = {key: value for key, value in updates.items() if key in ledger_fields}

        # Warn about unknown fields
        unknown_fields = set(updates.keys()) - ledger_fields
        if unknown_fields:
            logger.error(f"Unknown fields in job update will be ignored: {unknown_fields}")

        # Update ledger if there are ledger updates
        if ledger_updates:
            result = self._database_operation_service.update_state(
                namespace="core",
                query={"table": "job", "filters": {"id": job_id}},
                updates=ledger_updates,
            )

            if result.get("action_status") != "completed":
                logger.error(f"Failed to update async job ledger {job_id}: {result}")
                return result

        return {
            "action_status": "completed",
            "data": {"job_id": job_id, "updated": True},
        }
