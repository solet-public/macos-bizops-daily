"""Pure helper functions for ACT-R memory record manipulation.

All functions are stateless — no service dependencies.
"""

import json
import logging
from datetime import UTC, datetime
from typing import Any

logger = logging.getLogger(__name__)


def map_event_type_to_role(event_type: str) -> str:
    if event_type == "user_input":
        return "User"
    if event_type == "assistant_response":
        return "Assistant"
    return "System"


def format_attachments_suffix(metadata: dict[str, Any] | Any) -> str:
    if not isinstance(metadata, dict):
        return ""
    attachments = metadata.get("attachments", [])
    if not attachments:
        return ""
    attachment_parts = [
        a.get("filename") or a.get("name") or a.get("original_name") or "file"
        for a in attachments
        if isinstance(a, dict)
    ]
    if attachment_parts:
        return f" [Attachments: {', '.join(attachment_parts)}]"
    return ""


def format_memory_record(record: dict[str, Any]) -> str:
    """Format a single memory record for LLM context."""
    timestamp = record.get("timestamp", "")
    event_type = record.get("event_type", "")
    source_namespace = record.get("source_namespace", "")
    content = record.get("content", "")
    metadata = record.get("metadata") or {}
    role = map_event_type_to_role(event_type)
    line = f"[{timestamp}] {role} ({source_namespace}): {content}"
    return line + format_attachments_suffix(metadata)


def parse_metadata(
    metadata_str: str | None, record_id: Any
) -> dict[str, Any] | None:
    if not metadata_str:
        return None
    try:
        result = json.loads(metadata_str)
        if isinstance(result, dict):
            return result
        return None
    except json.JSONDecodeError:
        logger.error(f"Failed to parse metadata for event {record_id}")
        return None


def parse_json_field(
    record: dict[str, Any], field: str, record_id: str | None = None
) -> None:
    """Parse a JSON string field in-place, defaulting to empty list on failure."""
    value = record.get(field)
    if not isinstance(value, str):
        return
    if not value.strip():
        record[field] = []
        return
    try:
        record[field] = json.loads(value)
    except json.JSONDecodeError:
        logger.error(f"Invalid JSON in {field} for memory {record_id}")
        record[field] = []


def parse_memory_json_fields(memory: dict[str, Any]) -> None:
    """Parse all JSON fields in a memory record."""
    memory_id = memory.get("id")
    parse_json_field(memory, "tags", memory_id)
    parse_json_field(memory, "retrieval_times", memory_id)
    parse_json_field(memory, "source_memory_ids", memory_id)


def create_memory_record(
    content: str,
    tags: list[str],
    source_file: str | None,
    session_id: str | None,
    now_iso: str,
) -> dict[str, Any]:
    return {
        "content": content,
        "retrieval_times": [now_iso],
        "strength": 0.0,
        "retrieval_count": 1,
        "memory_type": "episodic",
        "status": "active",
        "source_memory_ids": [],
        "source_file": source_file,
        "source_lines": None,
        "session_id": session_id,
        "tags": tags,
    }


def serialize_memory_for_storage(memory: dict[str, Any]) -> dict[str, Any]:
    """Serialize JSON fields for database storage."""
    record = dict(memory)
    for field in ("tags", "retrieval_times", "source_memory_ids"):
        if isinstance(record.get(field), list):
            record[field] = json.dumps(record[field])
    return record


def extract_generated_id(result: dict[str, Any], record: dict[str, Any]) -> str:
    data = result.get("data", {})
    if isinstance(data, dict):
        result_data = data.get("result", {})
        if isinstance(result_data, dict):
            generated_id = result_data.get("generated_id")
            if isinstance(generated_id, str):
                return generated_id
    record_id = record.get("id", "")
    return str(record_id) if record_id else ""


def filter_memories_by_all_tags(
    memories: list[dict[str, Any]], tags: list[str] | None
) -> list[dict[str, Any]]:
    """Return only memories carrying EVERY tag in ``tags`` (ALL semantics).

    Mirrors ``recall``'s documented tag filter (a record must have all specified
    tags, not any) so a tag-filtered export cannot leak a record that carries
    only a subset — e.g. exporting ``["agent_memory", "agent_memory:origin:X"]``
    returns origin X's projection records and never origin Y's. ``None`` or an
    empty ``tags`` list returns the input unchanged. Tag matching is exact
    membership, consistent with the store's ``find_memories_by_tag`` semantics.
    """
    if not tags:
        return list(memories)
    required = set(tags)
    return [m for m in memories if required.issubset(set(m.get("tags") or []))]


def normalize_tags(tags: list[str] | str | None) -> list[str]:
    """Normalize tags input, handling LLM sending wrong types."""
    if tags is None:
        return []
    if isinstance(tags, list):
        return [str(t).strip() for t in tags if t and str(t).strip()]
    stripped = tags.strip()
    if stripped:
        logger.error(f"tags received as string '{stripped}' instead of list - auto-converted")
        return [stripped]
    return []


def build_recall_filters(memory_type: str, include_archived: bool) -> dict[str, Any]:
    filter_dict: dict[str, Any] = {}
    if memory_type != "all":
        filter_dict["memory_type"] = memory_type
    if not include_archived:
        filter_dict["status"] = "active"
    return filter_dict


def parse_datetime_str(dt_str: str) -> datetime | None:
    try:
        dt = datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt
    except ValueError:
        return None


def is_review_due(next_review_str: str, now: datetime) -> bool:
    if not next_review_str:
        return False
    next_review = parse_datetime_str(next_review_str)
    return next_review is not None and next_review <= now


def parse_created_at(memory: dict[str, Any]) -> datetime | None:
    created_str = memory.get("created_at", "")
    if not created_str:
        return None
    try:
        created = datetime.fromisoformat(created_str.replace("Z", "+00:00"))
        if created.tzinfo is None:
            created = created.replace(tzinfo=UTC)
        return created
    except ValueError:
        return None


def build_learn_result(
    path: str,
    memory_ids: list[str],
    skipped_count: int,
    memorized_count: int,
    memorize: bool,
) -> dict[str, Any]:
    message = f"Learned {len(memory_ids)} memories"
    if skipped_count:
        message += f", skipped {skipped_count} files"
    if memorize:
        message += f", memorizing {memorized_count}"
    return {
        "path": str(path),
        "memories_created": len(memory_ids),
        "files_skipped": skipped_count,
        "memorized": memorized_count if memorize else None,
        "message": message,
    }


def update_audit_counts(
    strength: float, status: str, counts: dict[str, int]
) -> None:
    if strength < -1.0:
        counts["weak"] += 1
    if status == "active":
        counts["active"] += 1
    elif status == "completed":
        counts["completed"] += 1


def content_preview(content: str, max_len: int) -> str:
    if len(content) > max_len:
        return content[:max_len] + "..."
    return content
