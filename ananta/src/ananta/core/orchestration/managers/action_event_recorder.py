import json
import logging
from datetime import UTC, datetime

from ananta.constants import NOTES_MAX_LENGTH
from ananta.core.actions.payload_bounds import check_action_parameters_size
from ananta.core.contexts.normalization import normalize_flow_id, normalize_session_id
from ananta.core.domain.types import ActionResult
from ananta.core.result_processing import ErrorProcessorKind, ResultProcessorKind
from ananta.core.state.execution_token_context import get_current_parent_token_id
from ananta.core.state.flow_runtime_graph import FlowRuntimeGraph, TokenOwnerType
from ananta.error_handling import FrameworkError
from ananta.services.state_service import StateService

from ..interfaces import IActionEventRecorder, IFlowManager

logger = logging.getLogger(__name__)


def _normalize_result_processor_kind(raw: object) -> str | None:
    """Coerce an action's result_processor_kind value into a stored string.

    Accepts ``None``, a :class:`ResultProcessorKind` member, or a string
    matching one of the enum values.  Raises :class:`FrameworkError` on
    any other value so corrupt data fails fast at the recorder boundary
    rather than at SQL ``CHECK`` time.
    """
    if raw is None:
        return None
    if isinstance(raw, ResultProcessorKind):
        return raw.value
    if isinstance(raw, str):
        try:
            return ResultProcessorKind(raw).value
        except ValueError as exc:
            allowed = sorted(k.value for k in ResultProcessorKind)
            raise FrameworkError(
                message=(
                    f"Invalid result_processor_kind {raw!r}; "
                    f"allowed values: {allowed}"
                ),
                error_code="action_recorder.invalid_result_processor_kind",
                details={"result_processor_kind": raw, "allowed": allowed},
            ) from exc
    raise FrameworkError(
        message="result_processor_kind must be None, str, or ResultProcessorKind",
        error_code="action_recorder.invalid_result_processor_kind_type",
        details={"result_processor_kind": raw, "type": type(raw).__name__},
    )


def _normalize_error_processor_kind(raw: object) -> str | None:
    """Coerce an action's error_processor_kind value into a stored string.

    Mirrors :func:`_normalize_result_processor_kind` for the failure
    side.  Accepts ``None``, a :class:`ErrorProcessorKind` member, or a
    matching string.
    """
    if raw is None:
        return None
    if isinstance(raw, ErrorProcessorKind):
        return raw.value
    if isinstance(raw, str):
        try:
            return ErrorProcessorKind(raw).value
        except ValueError as exc:
            allowed = sorted(k.value for k in ErrorProcessorKind)
            raise FrameworkError(
                message=(
                    f"Invalid error_processor_kind {raw!r}; "
                    f"allowed values: {allowed}"
                ),
                error_code="action_recorder.invalid_error_processor_kind",
                details={"error_processor_kind": raw, "allowed": allowed},
            ) from exc
    raise FrameworkError(
        message="error_processor_kind must be None, str, or ErrorProcessorKind",
        error_code="action_recorder.invalid_error_processor_kind_type",
        details={"error_processor_kind": raw, "type": type(raw).__name__},
    )


class ActionEventRecorder(IActionEventRecorder):
    def __init__(
        self,
        state_service: StateService,
        flow_manager: IFlowManager,
        flow_runtime_graph: FlowRuntimeGraph,
    ) -> None:
        self.state_service = state_service
        self.flow_manager = flow_manager
        self._flow_runtime_graph = flow_runtime_graph

    def _build_action_data(
        self,
        action: dict[str, object],
        sequence: int,
        depth: int,
        action_name: str,
        validated_flow_id: str,
        validated_session_id: str | None,
        validated_context_id: str | None,
    ) -> dict[str, object]:
        notes_value = action.get("notes")
        if not isinstance(notes_value, str) or not notes_value.strip():
            raise FrameworkError(
                message="Action definition missing required notes field",
                error_code="action_recorder.missing_notes",
                details={"action": action},
            )
        normalized_notes = notes_value.strip()[:NOTES_MAX_LENGTH]

        # Serialize parameters ONCE, then bound the resulting string before it
        # is persisted. Measuring the string json.dumps had to produce anyway
        # costs nothing extra and — load-bearing — involves no parse: the
        # 2026-08-15 outage was caused by an 82 MB payload whose json.loads
        # held the GIL for two hours, so any guard downstream of a parse has
        # already caused the outage it exists to prevent. See
        # ananta.core.actions.payload_bounds for the bound's derivation.
        serialized_parameters = json.dumps(action.get("parameters", {}))
        check_action_parameters_size(
            serialized_parameters,
            process_key=str(action.get("process_key", "")),
        )

        # Use validated IDs (already normalized to prevent empty string propagation)
        action_data: dict[str, object] = {
            "core__sessions_id": validated_session_id,
            "core__flows_id": validated_flow_id,
            "flow_id_trace": validated_flow_id,  # Denormalized flow_id for tracing queries
            "core__action_events_id": action.get("parent_id"),
            "context_id": validated_context_id,  # Platform context ID for OUTPUT events
            "sequence": sequence,
            "depth": depth,
            "name": action_name,
            "process_key": action.get("process_key"),
            "parameters": serialized_parameters,
            "notes": normalized_notes,
            "result_processor": (
                json.dumps(action.get("result_processor"))
                if action.get("result_processor")
                else None
            ),
            "result_processor_target": action.get("result_processor_target"),
            "error_processor": (
                json.dumps(action.get("error_processor")) if action.get("error_processor") else None
            ),
            "result_processor_kind": _normalize_result_processor_kind(
                action.get("result_processor_kind"),
            ),
            "error_processor_kind": _normalize_error_processor_kind(
                action.get("error_processor_kind"),
            ),
            "status": "queued",
        }

        # Persist compiler metadata (Phase 1 workflow compiler integration)
        # Schema defines these columns, so they should always be available
        if "compiled_version" in action:
            action_data["compiled_version"] = action["compiled_version"]
        if "validation_timestamp" in action:
            action_data["validation_timestamp"] = action["validation_timestamp"]

        if "job_result_ref" in action:
            action_data["job_result_ref"] = action["job_result_ref"]

        # Version-targeted dispatch (self-deployment plugin, addendum §K).
        # ``excluded_versions`` is a JSON list of SOLET_VERSION values
        # whose action queue pollers must skip the row. NULL/absent on every
        # action that doesn't need version targeting (backward compatible).
        excluded_versions = action.get("excluded_versions")
        if excluded_versions is not None:
            action_data["excluded_versions"] = json.dumps(excluded_versions)

        return action_data

    def _extract_generated_id(self, result: ActionResult) -> str:
        data = result.get("data", {})
        result_data = data.get("result", {})

        if isinstance(result_data, dict):
            generated_id = result_data.get("generated_id")
        elif isinstance(result_data, str):
            generated_id = result_data
        else:
            generated_id = data.get("generated_id")

        if not generated_id:
            raise FrameworkError(
                message="StateService did not return generated_id for action event",
                error_code="action_recorder.missing_generated_action_id",
                details={"result": result},
            )
        return str(generated_id)

    def _handle_temporary_name(self, action_name: str, generated_id: str) -> None:
        if action_name == "temp_name_will_be_replaced":
            self.state_service.update_state(
                namespace="core",
                query={"table": "action_events", "filters": {"id": generated_id}},
                updates={"name": generated_id},
            )

    def _validate_storage_result(self, result: ActionResult) -> None:
        action_status = result.get("action_status")
        status_value = getattr(action_status, "value", action_status)

        if status_value != "completed":
            raise FrameworkError(
                message="Failed to store action event in StateService",
                error_code="action_recorder.action_storage_failed",
                details={"result": result},
            )

    def _validate_flow_id(self, flow_id_raw: object) -> str:
        """Validate and normalize flow_id. Fail fast if missing or invalid.

        All actions require flow_id. Uses normalize_flow_id to ensure empty
        strings, "None", and other invalid values are rejected.

        Args:
            flow_id_raw: The raw flow_id value from action

        Returns:
            Normalized flow_id string

        Raises:
            FrameworkError: If flow_id is None, not a string, or invalid
        """
        if flow_id_raw is None:
            raise FrameworkError(
                message="Action requires flow_id - all actions must have flow context",
                error_code="action_recorder.flow_id_required",
                details={"flow_id": None},
            )
        if not isinstance(flow_id_raw, str):
            raise FrameworkError(
                message="flow_id must be a string",
                error_code="action_recorder.invalid_flow_id_type",
                details={"flow_id": flow_id_raw},
            )

        # Normalize to catch empty strings, "None", whitespace, etc.
        normalized = normalize_flow_id(flow_id_raw)
        if not normalized:
            raise FrameworkError(
                message=f"Invalid flow_id: '{flow_id_raw}' normalized to None",
                error_code="action_recorder.invalid_flow_id",
                details={"flow_id_raw": flow_id_raw},
            )
        return normalized

    def _validate_session_id(self, session_id_raw: object) -> str | None:
        """Validate and normalize session_id, returning None for invalid values.

        Uses normalize_session_id to ensure empty strings, "None", and other
        invalid values are converted to None rather than stored as corrupt data.
        """
        if session_id_raw is None:
            return None
        if not isinstance(session_id_raw, str):
            raise FrameworkError(
                message="session_id must be a string",
                error_code="action_recorder.invalid_session_id_type",
                details={"session_id": session_id_raw},
            )

        # Normalize to catch empty strings, "None", whitespace, etc.
        normalized = normalize_session_id(session_id_raw)
        if session_id_raw and not normalized:
            logger.error(
                f"Normalized invalid session_id '{session_id_raw}' to None - "
                "this may indicate a propagation issue upstream"
            )
        return normalized

    def _validate_parent_id(self, parent_id_raw: object) -> str | None:
        """Validate and return parent_id as string or None."""
        if parent_id_raw is None:
            return None
        if not isinstance(parent_id_raw, str):
            raise FrameworkError(
                message="parent_id must be a string or None",
                error_code="action_recorder.invalid_parent_id_type",
                details={"parent_id": parent_id_raw},
            )
        return parent_id_raw

    def _validate_context_id(self, context_id_raw: object) -> str | None:
        """Validate and normalize context_id for platform context event correlation.

        Context ID is optional but when present must be a valid string (typically ctx-...).
        Used by ActionProcessor to write OUTPUT events to the correct platform context.

        Args:
            context_id_raw: The raw context_id value from action

        Returns:
            Validated context_id string or None
        """
        if context_id_raw is None:
            return None
        if not isinstance(context_id_raw, str):
            raise FrameworkError(
                message="context_id must be a string or None",
                error_code="action_recorder.invalid_context_id_type",
                details={"context_id": context_id_raw},
            )
        # Basic validation - context_id should not be empty
        stripped = context_id_raw.strip()
        if not stripped:
            return None
        return stripped

    def _validate_action_name(self, action_name_raw: object) -> str:
        """Validate and return action name as string."""
        if not isinstance(action_name_raw, str):
            raise FrameworkError(
                message="action name must be a string",
                error_code="action_recorder.invalid_action_name_type",
                details={"action_name": action_name_raw},
            )
        return action_name_raw

    def store_action_event(self, action: dict[str, object]) -> str:
        """Store an action event. All actions require flow_id.

        Args:
            action: Action dict with required flow_id, optional context_id for platform mode

        Returns:
            The generated action ID

        Raises:
            FrameworkError: If flow_id is missing or invalid
        """
        # Validate and normalize IDs - flow_id is required, session_id and context_id are optional
        flow_id = self._validate_flow_id(action.get("flow_id"))
        session_id = self._validate_session_id(action.get("session_id"))
        context_id = self._validate_context_id(action.get("context_id"))

        # flow_id is guaranteed valid at this point (no else 1 fallback)
        sequence = self.flow_manager.get_next_sequence_in_flow(flow_id)

        parent_id = self._validate_parent_id(action.get("parent_id"))
        depth = self.calculate_action_depth(parent_id)

        action_name = self._validate_action_name(action.get("name", "temp_name_will_be_replaced"))
        action_data = self._build_action_data(
            action, sequence, depth, action_name, flow_id, session_id, context_id
        )

        result = self.state_service.write_state(
            namespace="core", data={"table": "action_events", "record": action_data}
        )

        self._validate_storage_result(result)
        generated_id = self._extract_generated_id(result)
        self._handle_temporary_name(action_name, generated_id)

        # Always create flow token since flow_id is required
        process_key_raw = action.get("process_key")
        process_key = str(process_key_raw) if process_key_raw else None
        self._create_flow_token(flow_id, generated_id, process_key)

        return generated_id

    def _create_flow_token(
        self,
        flow_id: str,
        action_id: str,
        process_key: str | None,
    ) -> None:
        """Create FRG token when an action is queued.

        Token links action to flow for completion tracking.
        Uses contextvars to get parent_token_id from result_processor context.
        """
        parent_token_id = get_current_parent_token_id()

        # Debug: Log parent token propagation
        logger.debug(
            f"FRG_TOKEN_CREATE: action_id={action_id}, process_key={process_key}, "
            f"parent_token_id={parent_token_id}"
        )

        token_id = self._flow_runtime_graph.create_token(
            flow_id=flow_id,
            owner_type=TokenOwnerType.PROCESS,
            owner_ref=action_id,
            process_key=process_key,
            parent_token_id=parent_token_id,
        )

        # Update action with its token ID
        self._update_action_token_id(action_id, token_id)

    def _update_action_token_id(self, action_id: str, token_id: str) -> None:
        """Set flow_token_id on action record after token creation."""
        self.state_service.update_state(
            namespace="core",
            query={"table": "action_events", "filters": {"id": action_id}},
            updates={"flow_token_id": token_id},
        )

    def update_action_completion(self, action_id: str, result: dict[str, object]) -> None:
        raw_id = action_id

        update_data = {
            "status": "completed" if result.get("status") == "completed" else "failed",
            "result_data": json.dumps(result),
            "completed_at": datetime.now(UTC).isoformat(),
            "error_message": result.get("error") if result.get("status") == "failed" else None,
        }

        self.state_service.update_state(
            namespace="core",
            query={"table": "action_events", "filters": {"id": raw_id}},
            updates=update_data,
        )

    def update_action_result(self, action_id: str, result: dict[str, object]) -> None:
        raw_id = action_id

        self.state_service.update_state(
            namespace="core",
            query={"table": "action_events", "filters": {"id": raw_id}},
            updates={"result_data": json.dumps(result)},
        )

    def update_action_error(self, action_id: str, error_message: str) -> None:
        raw_id = action_id

        update_data: dict[str, object] = {
            "status": "failed",
            "error_message": error_message,
            "completed_at": datetime.now(UTC).isoformat(),
        }

        self.state_service.update_state(
            namespace="core",
            query={"table": "action_events", "filters": {"id": raw_id}},
            updates=update_data,
        )

    def calculate_action_depth(self, parent_id: str | None) -> int:
        if not parent_id:
            return 0

        raw_parent_id = parent_id

        result = self.state_service.read_state(
            namespace="core", query={"table": "action_events", "filters": {"id": raw_parent_id}}
        )

        if result:
            parent_depth = result.get("depth", 0)
            # Safely convert parent_depth to int
            try:
                if parent_depth is None:
                    depth_int = 0
                elif isinstance(parent_depth, int):
                    depth_int = parent_depth
                else:
                    depth_int = int(str(parent_depth))
            except (ValueError, TypeError):
                depth_int = 0
            new_depth = depth_int + 1
            return new_depth

        return 0

    def get_action_status(self, action_id: str) -> str | None:
        raw_id = action_id

        result = self.state_service.read_state(
            namespace="core", query={"table": "action_events", "filters": {"id": raw_id}}
        )

        if result:
            status = result.get("status")
            return (
                status if isinstance(status, str) else (str(status) if status is not None else None)
            )

        return None
