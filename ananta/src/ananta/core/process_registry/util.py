"""
Process Registry Utility - Centralized operations for process registry management.

This utility centralizes common patterns for:
- Querying process registry records
- Syncing registry data
- Looking up process information
- Writing registry records

Eliminates duplicate code across ActionManager, EventOrchestrator, ActionValidator, etc.
"""

import logging

from ananta.constants import FRAMEWORK_NAMESPACE
from ananta.core.domain.status import is_status_match
from ananta.core.domain.types import ActionResult
from ananta.core.plugins.plugin_contracts import ActionStatus
from ananta.interfaces.state_service_protocol import StateServiceProtocol

logger = logging.getLogger(__name__)


class ProcessRegistryUtil:
    """Centralized utility for process registry operations."""

    def __init__(self, state_service: StateServiceProtocol):
        self.state_service = state_service

    def lookup_external_id_by_process_key(self, process_key: str) -> str | None:
        """Look up external_id for a given process_key."""
        if not self.state_service or not process_key:
            logger.error("State service not available or process_key empty for process lookup")
            return None

        try:
            result = self.state_service.read_state(
                namespace=FRAMEWORK_NAMESPACE,
                query={
                    "table": "process_registry",
                    "filters": {"process_key": process_key},
                    "limit": 1,
                },
            )

            external_id = self._extract_external_id_from_result(result)
            if external_id:
                return external_id

            return None

        except Exception as e:
            logger.error(f"Error looking up process '{process_key}': {e}")
            return None

    def _first_record(self, result: ActionResult | dict[str, object]) -> dict[str, object] | None:
        """The first row of a ``read_state`` result, or ``None`` if there is none.

        ``read_state`` returns ``data = {records, count, namespace, table}``.
        Every reader in this class previously looked for ``data["result"]``
        first, which that envelope has never carried (``count``/``update_state``
        carry a ``result`` key; ``read_state`` does not). The lookups therefore
        bailed at the missing key and reported "not found" for rows that were
        sitting in the table — see the class docstring on the three methods this
        replaces.
        """
        if not is_status_match(result.get("action_status"), ActionStatus.COMPLETED):
            return None

        data = result.get("data")
        if not isinstance(data, dict):
            return None

        records = data.get("records")
        if not isinstance(records, list) or not records:
            return None

        first = records[0]
        return first if isinstance(first, dict) else None

    def _extract_external_id_from_result(
        self, result: ActionResult | dict[str, object]
    ) -> str | None:
        """Extract external_id from a state service query result."""
        process_record = self._first_record(result)
        if process_record is None:
            return None

        external_id = process_record.get("external_id")
        if isinstance(external_id, str):
            return external_id

        return None

    def get_process_info(self, process_external_id: str) -> dict[str, str]:
        """Get process information by external_id."""
        try:
            if not self.state_service or not process_external_id:
                return {}

            # `external_id` was previously passed at the TOP LEVEL of the query
            # dict. The provider reads only query["table"], ["filters"],
            # ["limit"] and ["unbounded"] — so the predicate was silently
            # dropped and this scanned all 756 rows of core.process_registry
            # (measured 2026-08-15) to answer a single-row lookup. A filter at
            # the wrong nesting level is not a weak filter, it is no filter, and
            # nothing in the source makes that visible: this is why the census
            # classified the site as having "(no filters key)" rather than as
            # the inert-predicate bug it actually is.
            result = self.state_service.read_state(
                FRAMEWORK_NAMESPACE,
                {
                    "table": "process_registry",
                    "filters": {"external_id": process_external_id},
                    # external_id carries a unique constraint (see sync_records'
                    # conflict_columns), so the true bound is exactly one row.
                    "limit": 1,
                },
            )

            process_record = self._first_record(result)
            if process_record is None:
                return {}

            # Read off the ROW. The previous version read these keys off the
            # envelope itself, where none of them exist — so every `.get(key,
            # default)` returned its default and the method handed back
            # {"provider_type": "plugin", "provider": "", "function_name": ""}
            # for every input, including ones with no matching row at all. That
            # is worse than returning nothing: it is a well-formed answer
            # assembled entirely out of default arguments, and it looks exactly
            # like a successful lookup to every caller.
            provider_type = process_record.get("provider_type", "plugin")
            provider = process_record.get("provider", "")
            function_name = process_record.get("function_name", "")
            return {
                "provider_type": provider_type if isinstance(provider_type, str) else "plugin",
                "provider": provider if isinstance(provider, str) else "",
                "function_name": function_name if isinstance(function_name, str) else "",
            }

        except Exception as e:
            logger.error(f"Error getting process info for '{process_external_id}': {e}")
            return {}

    def query_by_process_key(self, process_key: str) -> dict[str, object] | None:
        """Query process registry for a process by process_key."""
        try:
            if not self.state_service or not process_key:
                return None

            result = self.state_service.read_state(
                FRAMEWORK_NAMESPACE,
                {"table": "process_registry", "filters": {"process_key": process_key}, "limit": 1},
            )

            # Same envelope correction as _first_record: this returned
            # data["result"], which read_state does not carry, so it returned
            # None for every process_key in the table.
            return self._first_record(result)

        except Exception as e:
            logger.error(f"Error querying process registry for '{process_key}': {e}")
            return None

    def sync_records(self, records: list[dict[str, object]]) -> bool:
        """Sync process registry records to the database using upsert.

        Uses upsert (INSERT ... ON CONFLICT) to ensure existing records are updated
        with new values (like embedding_description) rather than failing on conflict.
        """
        try:
            if not self.state_service or not records:
                logger.error("State service not available or no records to sync")
                return False

            success_count = 0
            error_count = 0

            for record in records:
                result = self.state_service.upsert_state(
                    namespace=FRAMEWORK_NAMESPACE,
                    data={
                        "table": "process_registry",
                        "record": record,
                        # Use external_id for conflict detection since it's the deterministic ID
                        # that gets generated from the process_key (and has a unique constraint)
                        "conflict_columns": ["external_id"],
                    },
                )
                if is_status_match(result.get("action_status"), ActionStatus.COMPLETED):
                    success_count += 1
                else:
                    error_count += 1
                    error_info = result.get("error")
                    error_msg = (
                        error_info.get("message", "unknown error")
                        if isinstance(error_info, dict)
                        else "unknown error"
                    )
                    logger.warning(
                        f"Failed to upsert process: {record.get('process_key')}: {error_msg}"
                    )

            logger.debug(
                f"Process registry sync: {success_count} succeeded, {error_count} failed"
            )
            return error_count == 0

        except Exception as e:
            logger.error(f"Error syncing process registry records: {e}")
            return False

    def write_single_record(self, record_data: dict[str, object]) -> bool:
        """Write a single process registry record using upsert semantics.

        Uses upsert (INSERT ... ON CONFLICT) on external_id to make the
        entire persist/refresh pathway idempotent.  When a process already
        exists the row is updated in-place instead of raising a
        UniqueViolation on the external_id constraint.
        """
        try:
            if not self.state_service or not record_data:
                logger.error("State service not available or no record data")
                return False

            result = self.state_service.upsert_state(
                namespace=FRAMEWORK_NAMESPACE,
                data={
                    "table": "process_registry",
                    "record": record_data,
                    "conflict_columns": ["external_id"],
                },
            )

            return is_status_match(result.get("action_status"), ActionStatus.COMPLETED)

        except Exception as e:
            logger.error(f"Error writing process registry record: {e}")
            return False

    def check_table_exists(self, error_message: str) -> bool:
        """Check if process_registry table exists based on error message."""
        return "no such table" in error_message and "process_registry" in error_message
