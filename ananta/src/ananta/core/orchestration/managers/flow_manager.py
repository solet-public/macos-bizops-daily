import json
import logging
from typing import cast

from ananta.error_handling import ActionNameCollisionError, FrameworkError
from ananta.services.state_service import StateService

from ..interfaces import IFlowManager

logger = logging.getLogger(__name__)


class FlowManager(IFlowManager):
    def __init__(self, state_service: StateService):
        self.state_service = state_service
        self.current_flow_id: str | None = None
        # CRITICAL FIX: Cache sequence numbers to avoid circular dependency with StateService
        # Key = flow_id, Value = next_sequence_number
        self._sequence_cache: dict[str, int] = {}

    def create_flow(
        self, session_id: str, trigger_type: str, trigger_data: dict[str, object], priority: int = 5
    ) -> str:
        flow_data = {
            "core__sessions_id": session_id,
            "trigger_type": trigger_type,
            "trigger_source": trigger_data.get("source", "unknown"),
            "trigger_data": json.dumps(trigger_data),
            "priority": priority,
            "status": "active",
        }

        result = self.state_service.write_state(
            namespace="core", data={"table": "flows", "record": flow_data}
        )

        if result.get("action_status") != "completed":
            raise FrameworkError(
                message="Failed to create flow in StateService",
                error_code="flow_manager.flow_creation_failed",
                details={"result": result},
            )

        # Type narrow: result is dict, get data field
        data = result.get("data")
        if not isinstance(data, dict):
            raise FrameworkError(
                message="StateService returned invalid data structure",
                error_code="flow_manager.invalid_data_structure",
                details={"result": result},
            )

        # Type narrow: data is dict, get result field
        inner_result = data.get("result")
        if not isinstance(inner_result, dict):
            raise FrameworkError(
                message="StateService returned invalid result structure",
                error_code="flow_manager.invalid_result_structure",
                details={"result": result},
            )

        # Type narrow: inner_result is dict, get generated_id
        flow_id_value = inner_result.get("generated_id")
        if not isinstance(flow_id_value, str):
            raise FrameworkError(
                message="StateService did not return generated_id for flow",
                error_code="flow_manager.missing_generated_flow_id",
                details={"result": result},
            )

        flow_id: str = flow_id_value
        self.current_flow_id = flow_id
        # Initialize sequence cache for new flow
        self._sequence_cache[flow_id] = 1
        return flow_id

    def get_flow_trigger_data(self, flow_id: str) -> dict[str, object]:
        result = self.state_service.read_state(
            namespace="core", query={"table": "flows", "filters": {"id": flow_id}}
        )

        # ActionResult is a TypedDict which is compatible with dict[str, object]
        result_dict: dict[str, object] = cast(dict[str, object], result)
        flow_record = self._extract_flow_record(result_dict)
        if not flow_record:
            return {}

        trigger_data_str = flow_record.get("trigger_data", "{}")
        try:
            if trigger_data_str is None:
                trigger_data_str = "{}"
            elif not isinstance(trigger_data_str, str):
                trigger_data_str = str(trigger_data_str)
            parsed_data = json.loads(trigger_data_str)
            if not isinstance(parsed_data, dict):
                return {}
            trigger_data: dict[str, object] = parsed_data
            return trigger_data
        except json.JSONDecodeError:
            return {}

    def get_next_sequence_in_flow(self, flow_id: str) -> int:
        if not flow_id:
            return 1

        # CRITICAL FIX: Use cached sequence to avoid StateService circular dependency
        # This prevents the architectural violation where StateService is busy processing
        # the console input action that triggered action creation
        if flow_id in self._sequence_cache:
            next_sequence = self._sequence_cache[flow_id]
            # Increment cache for next action
            self._sequence_cache[flow_id] = next_sequence + 1
            return next_sequence

        # If flow not in cache, initialize it (this should only happen for existing flows on startup)
        self._sequence_cache[flow_id] = 2  # Next action will be sequence 2
        return 1

    def validate_action_name_uniqueness(self, action: dict[str, object]) -> None:
        action_name_value = action.get("name")
        flow_id_value = action.get("flow_id")

        # Type narrow: ensure action_name and flow_id are strings
        if not isinstance(action_name_value, str) or not isinstance(flow_id_value, str):
            return

        action_name: str = action_name_value
        flow_id: str = flow_id_value

        # Note: read_state returns ActionResult (TypedDict with optional 'actions' field)
        existing_actions_data = self.state_service.read_state(
            namespace="core",
            query={
                "table": "action_events",
                "filters": {"core__flows_id": flow_id, "name": action_name},
            },
        )

        # ActionResult may have an 'actions' field containing the list of action records
        actions_field = existing_actions_data.get("actions")
        if not isinstance(actions_field, list):
            # No actions found or invalid format
            return

        # Filter for active actions (not failed or cancelled)
        active_actions: list[dict[str, object]] = []
        for a in actions_field:
            status = a.get("status")
            if status not in ["failed", "cancelled"]:
                active_actions.append(a)

        if active_actions:
            raise ActionNameCollisionError(
                action_name=action_name,
                flow_id=flow_id,
                details=f"Found {len(active_actions)} existing actions with same name",
            )

    def get_current_flow_id(self) -> str | None:
        return self.current_flow_id

    def set_current_flow_id(self, flow_id: str) -> None:
        self.current_flow_id = flow_id

    def get_flow_status(self, flow_id: str) -> str | None:
        result = self.state_service.read_state(
            namespace="core", query={"table": "flows", "filters": {"id": flow_id}}
        )

        # ActionResult is a TypedDict which is compatible with dict[str, object]
        result_dict: dict[str, object] = cast(dict[str, object], result)
        flow_record = self._extract_flow_record(result_dict)
        if not flow_record:
            return None

        status = flow_record.get("status")
        if isinstance(status, str):
            return status
        if status is not None:
            return str(status)
        return None

    def get_flow_session_id(self, flow_id: str) -> str | None:
        """Resolve the session that owns ``flow_id`` (``core__sessions_id``).

        The flows row persists past flow termination (status is set, never
        deleted), so this resolves even for a completed/forwarded flow — the
        property SUB-05's forwarded-vertex RESUBMIT relies on to re-enter the
        owning session's turn. Returns ``None`` if the flow is unknown.
        """
        result = self.state_service.read_state(
            namespace="core", query={"table": "flows", "filters": {"id": flow_id}}
        )
        result_dict: dict[str, object] = cast(dict[str, object], result)
        flow_record = self._extract_flow_record(result_dict)
        if not flow_record:
            return None

        session_id = flow_record.get("core__sessions_id")
        if isinstance(session_id, str):
            return session_id
        if session_id is not None:
            return str(session_id)
        return None

    def update_flow_status(self, flow_id: str, status: str) -> None:
        self.state_service.update_state(
            namespace="core",
            query={"table": "flows", "filters": {"id": flow_id}},
            updates={"status": status},
        )

    def _extract_first_record_from_list(self, records: object) -> dict[str, object] | None:
        """Extract first dict record from a list if present."""
        if not isinstance(records, list) or not records:
            return None
        first_record = records[0]
        if isinstance(first_record, dict):
            return first_record
        return None

    def _extract_flow_record(self, result: dict[str, object] | None) -> dict[str, object] | None:
        """Extract a flow record from heterogeneous StateService ActionResult formats."""
        if not isinstance(result, dict):
            return None

        data = result.get("data")
        if not isinstance(data, dict):
            return None

        # Format 1: Nested data.result.records (default plugin success response)
        inner_result = data.get("result")
        if isinstance(inner_result, dict):
            record = self._extract_first_record_from_list(inner_result.get("records"))
            if record:
                return record

        # Format 2: Direct data.records (bootstrap/state queries)
        record = self._extract_first_record_from_list(data.get("records"))
        if record:
            return record

        # Format 3: Single record returned directly in data
        if self._looks_like_flow_record(data):
            return data

        return None

    def _looks_like_flow_record(self, candidate: dict[str, object]) -> bool:
        """Heuristic to determine if dict appears to be a core__flows record."""
        required_keys = {"id", "status"}
        return required_keys.issubset(candidate.keys())
