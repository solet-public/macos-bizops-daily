import re
from typing import Any

from .models import ActionData

RELOAD_SAFE = True

CRON_PART_PATTERN = re.compile(r"^[\w\*/,\-]+$")
CRON_MACRO_PATTERN = re.compile(r"^@[A-Za-z]+$")

PLACEHOLDER_CRON_VALUES = {"test_value"}
DEFAULT_PLACEHOLDER_CRON = "0 * * * *"

SESSION_CONTEXT_REQUIRED_PROCESSOR_KINDS: frozenset[str] = frozenset({"inference"})

CRON_ACTION_CONTRACT_KB_HINT = (
    "21_scheduling_service/01_template_flow_record_lifecycle.md"
)


def normalize_tags(raw_tags: Any) -> list[str]:
    if not raw_tags:
        return []
    if isinstance(raw_tags, list):
        return [str(tag).strip() for tag in raw_tags if str(tag).strip()]
    if isinstance(raw_tags, str):
        parts = [part.strip() for part in raw_tags.split(",")]
        cleaned = [part for part in parts if part]
        return cleaned or [raw_tags.strip()]
    return []


def normalize_cron_expression(cron_expression: str) -> str:
    if not cron_expression:
        return ""
    cron = cron_expression.strip()
    if cron.lower() in PLACEHOLDER_CRON_VALUES:
        return DEFAULT_PLACEHOLDER_CRON
    return cron


def validate_cron_expression(cron_expression: Any) -> bool:
    """Basic validation for cron expressions or macros."""
    if not isinstance(cron_expression, str):
        return False

    cron = cron_expression.strip()
    if not cron:
        return False

    if CRON_MACRO_PATTERN.fullmatch(cron):
        return True

    parts = cron.split()
    if len(parts) not in {5, 6, 7}:
        return False

    return all(CRON_PART_PATTERN.fullmatch(part) for part in parts)


def _build_cron_action_violation_message(name: str, kind: str) -> str:
    return (
        f"Cron action_def for {name!r} declares result_processor_kind={kind!r} "
        f"which requires session context at fire time. Cron-fired actions have "
        f"no originating session; the dispatcher's source_namespace lookup will "
        f"fail with 'Empty source_namespace in flow trigger_data'. Declare the "
        f"cron-target verb as ProcessorPolicyCategory.EDGE_SINK and omit "
        f"result_processor_kind (or use the memory-tag heartbeat pattern). "
        f"See KB article: {CRON_ACTION_CONTRACT_KB_HINT}."
    )


def validate_cron_action_def(action: ActionData) -> None:
    """Validate a cron action_def at registration time.

    Cron-fired actions never run with an originating session. Any
    `result_processor_kind` in :data:`SESSION_CONTEXT_REQUIRED_PROCESSOR_KINDS`
    requires session context at fire time and will fail in the dispatcher's
    `_resolve_io_process_key` path. Customizations on the result processor are
    permissive: the validator only checks the kind because that is the field the
    runtime failure-mode binds to.

    Raises:
        ValueError: When `action.result_processor_kind` requires session context.
    """
    kind = action.result_processor_kind
    if kind in SESSION_CONTEXT_REQUIRED_PROCESSOR_KINDS:
        raise ValueError(_build_cron_action_violation_message(action.name, kind))


def validate_persisted_cron_action_def(action: dict[str, Any]) -> None:
    """Validate a persisted cron action_def at restoration time.

    Persisted action data is dict-shaped (from `ScheduleData.model_dump()`).
    Mirrors :func:`validate_cron_action_def` for the recurring and one-time
    restoration paths in `SchedulerManager`.

    Raises:
        ValueError: When the persisted `result_processor_kind` requires session
        context.
    """
    kind = action.get("result_processor_kind")
    if isinstance(kind, str) and kind in SESSION_CONTEXT_REQUIRED_PROCESSOR_KINDS:
        raw_name = action.get("name")
        name = raw_name if isinstance(raw_name, str) and raw_name else "<unknown>"
        raise ValueError(_build_cron_action_violation_message(name, kind))
