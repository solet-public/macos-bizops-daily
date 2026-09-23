"""Generated file extraction and formatting for APIStage.

Pure functions extracted from APIStage for scanning conversation
history and observations for generated file metadata.
"""

from __future__ import annotations

import json
import re
from typing import Any

from ananta.core.prompts.context import PromptContext


def find_blob_id(
    result_data: dict[str, Any],
    blob_id_fields: list[str],
) -> str | None:
    """Find blob_id from result data using known field names."""
    for field in blob_id_fields:
        if field in result_data:
            return str(result_data[field])
    return None


def infer_content_type(file_format: str) -> str:
    """Infer content type from file format."""
    audio_formats = {"wav", "mp3", "flac", "ogg", "aac"}
    image_formats = {"png", "jpg", "jpeg", "gif", "webp"}

    if file_format in audio_formats:
        return f"audio/{file_format}"
    if file_format in image_formats:
        return f"image/{file_format}"
    return ""


def infer_source_process(context_content: str) -> str:
    """Infer source process from context content."""
    source_patterns: dict[str, list[str]] = {
        "speech_synthesis": [
            "Speech synthesized",
            "synthesize_speech_from_string",
            "synthesize_speech_from_ssml",
            "synthesize_speech_from_script",
        ],
        "audio_generation": ["execute_numpy", "execute_ffmpeg"],
    }

    for source, patterns in source_patterns.items():
        if any(pattern in context_content for pattern in patterns):
            return source

    return "unknown"


def extract_file_info_from_result(
    result_data: dict[str, Any],
    blob_id_fields: list[str],
    context_content: str,
) -> dict[str, Any] | None:
    """Extract file info from an action result dict."""
    blob_id = find_blob_id(result_data, blob_id_fields)
    if not blob_id:
        return None

    file_format = result_data.get(
        "format", result_data.get("output_format", ""),
    )

    return {
        "blob_id": blob_id,
        "namespace": result_data.get("blob_namespace", ""),
        "format": file_format,
        "content_type": infer_content_type(file_format),
        "source": infer_source_process(context_content),
        "duration": result_data.get(
            "duration_seconds", result_data.get("duration_s"),
        ),
    }


# Field names that indicate file generation in action results
_BLOB_ID_FIELDS = [
    "blob_id", "audio_blob_key", "image_blob_id", "output_blob_id",
]


def extract_generated_files(
    messages: list[dict[str, str]],
) -> list[dict[str, Any]]:
    """Extract blob_ids from action results in conversation history messages.

    Secondary scan for file info in stored events. Action results are
    ephemeral (not stored in context events), so extract_file_from_observation
    handles the current turn. This method catches any legacy or edge-case
    data in conversation history.
    """
    generated_files: list[dict[str, Any]] = []
    seen_blob_ids: set[str] = set()

    for msg in messages:
        content = msg.get("content", "")
        role = msg.get("role", "")
        if role not in {"system", "user"}:
            continue

        if (
            "result:" not in content
            and "action_result:" not in content
            and '"blob_id"' not in content
        ):
            continue

        file_info = _try_extract_file_from_content(content)
        if file_info and file_info.get("blob_id") not in seen_blob_ids:
            seen_blob_ids.add(file_info["blob_id"])
            generated_files.append(file_info)

    return generated_files


def _try_extract_file_from_content(content: str) -> dict[str, Any] | None:
    """Try to parse action result JSON from a message and extract file info."""
    try:
        result_match = re.search(
            r"(?:action_result|result):\s*(\{[\s\S]*?\})"
            r"\s*(?:session_id:|flow_input:|$)",
            content,
        )
        if not result_match:
            return None
        result_json = result_match.group(1)
        result_data = json.loads(result_json)
        return extract_file_info_from_result(
            result_data, _BLOB_ID_FIELDS, content,
        )
    except (json.JSONDecodeError, AttributeError):
        return None


def extract_file_from_observation(
    ctx: PromptContext,
) -> dict[str, Any] | None:
    """Extract generated file info from the current observation's action_result.

    Action results are ephemeral (not stored in context events), so this
    is the only way to surface files generated in the current turn.
    """
    prompt_part = ctx.resolved_action_params.get("prompt", {})
    if not isinstance(prompt_part, dict):
        return None
    observation = prompt_part.get("observation", {})
    if not isinstance(observation, dict):
        return None
    action_result = observation.get("action_result")
    if not isinstance(action_result, dict):
        return None

    blob_id = find_blob_id(action_result, _BLOB_ID_FIELDS)
    if not blob_id:
        return None

    process_key = observation.get("process_key", "")
    file_format = action_result.get(
        "format", action_result.get("output_format", ""),
    )
    return {
        "blob_id": blob_id,
        "namespace": action_result.get("blob_namespace", ""),
        "format": file_format,
        "content_type": infer_content_type(str(file_format)),
        "source": str(process_key) if process_key else "",
        "duration": action_result.get(
            "duration_seconds", action_result.get("duration_s"),
        ),
    }


def extract_generated_files_from_history(
    ctx: PromptContext,
) -> list[dict[str, Any]]:
    """Extract generated files info from conversation history entries."""
    return extract_generated_files(ctx.conversation_history)


def determine_filename(
    file_info: dict[str, Any],
    fmt: str,
    source: str,
) -> str:
    """Determine filename from file info, format, and source.

    Priority:
    1. Stored name (name, filename, or original_name)
    2. Source-specific default (speech.wav, audio.mp3)
    3. Generic default (output.{fmt} or output)
    """
    stored_name = (
        file_info.get("name")
        or file_info.get("filename")
        or file_info.get("original_name")
    )
    if stored_name:
        return str(stored_name)

    if source == "speech_synthesis":
        return f"speech.{fmt}" if fmt else "speech.wav"
    if source == "audio_generation":
        return f"audio.{fmt}" if fmt else "audio.mp3"

    return f"output.{fmt}" if fmt else "output"


def format_single_file_line(file_info: dict[str, Any]) -> str:
    """Format a single file info dict as a summary line.

    NOTE: blob_id and namespace intentionally hidden from LLM context
    to prevent hallucination of internal identifiers.
    """
    fmt = file_info.get("format", "")
    source = file_info.get("source", "unknown")
    content_type = file_info.get("content_type", "")
    duration = file_info.get("duration")

    filename = determine_filename(file_info, fmt, source)

    type_hint = f" [{content_type}]" if content_type else ""
    duration_str = f" ({duration:.1f}s)" if duration else ""

    return f"- {filename}{type_hint}{duration_str}"


def format_generated_files_summary(
    files: list[dict[str, Any]],
) -> str:
    """Format generated files list for system message."""
    return "\n".join(format_single_file_line(f) for f in files)
