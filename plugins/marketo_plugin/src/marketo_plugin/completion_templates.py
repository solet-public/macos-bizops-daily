"""Completion-handler template builders for AsyncJobManager routing.

Mirrors comfyui_image_generation_plugin's completion_templates.py — the one
production instance of this shape (D0.3 doctrine §5). AsyncJobManager stamps
``params.job_payload``/``job_error``/``job_id``/``job_status`` onto the
built arguments itself (``_build_completion_arguments``); this module only
supplies the inference prompt template naming what to do with them.
"""
from __future__ import annotations

from typing import Literal


def build_output_schema() -> dict[str, object]:
    """Structured schema for instructing inference service to call post_message."""
    return {
        "$schema": "http://json-schema.org/draft-07/schema#",
        "type": "object",
        "required": ["reasoning", "actions"],
        "properties": {
            "reasoning": {"type": "string"},
            "actions": {
                "type": "array",
                "minItems": 1,
                "maxItems": 1,
                "items": {
                    "type": "object",
                    "required": ["process_key", "reason", "arguments"],
                    "properties": {
                        "process_key": {
                            "type": "string",
                            "description": (
                                "The active IO plugin's post_message process key "
                                "(plugin::<io_plugin>::post_message)"
                            ),
                            "pattern": "^plugin::.+::post_message$",
                        },
                        "reason": {"type": "string"},
                        "arguments": {
                            "type": "object",
                            "required": ["session_id", "message"],
                            "properties": {
                                "session_id": {"type": "string"},
                                "message": {"type": "string"},
                            },
                        },
                    },
                },
            },
        },
    }


def build_completion_template(
    handler_type: Literal["result", "error"],
    verb_label: str,
) -> dict[str, object]:
    """Create the inference template AsyncJobManager routes a job's outcome through."""
    if handler_type == "result":
        instructions = [
            f"A Marketo {verb_label} job completed successfully.",
            "The job result carries a workspace TSV file path, row_count, columns, "
            "and a truncated flag (true when more records may exist beyond what "
            "was written).",
            "Confirm the export: state the file path and row_count plainly — this "
            "is an internal workspace path the operator already has access to, not "
            "a secret to withhold.",
            "If truncated is true, say the pull is INCOMPLETE and more records may "
            "exist beyond what was written.",
            "Reply with action using the active IO plugin's post_message "
            "(process_key: plugin::<io_plugin>::post_message).",
        ]
    else:
        instructions = [
            f"A Marketo {verb_label} job ended with an error.",
            "Explain what went wrong in plain terms using the error code and message.",
            "Suggest concrete remediation for known Marketo error classes: "
            "marketo.auth_failed (check OAuth credentials), marketo.permission_denied "
            "or marketo.partition_access_denied (run check_setup for the exact gap "
            "and which admin screen fixes it), marketo.rate_limited (retry after a "
            "short delay), marketo.invalid_params (fix the request and retry).",
            "Reply with action using the active IO plugin's post_message "
            "(process_key: plugin::<io_plugin>::post_message).",
        ]

    return {
        "params": {
            "model": {"temperature": 0.4, "max_tokens": 2048},
            "prompt": {
                "user": {
                    "instructions": instructions,
                    "output_schema": build_output_schema(),
                    "result_data": {},
                    "action_result": {},
                    "flow_input": {"original_input": ""},
                },
            },
        },
    }


def build_job_metadata(session_id: str, flow_id: str, verb_label: str) -> dict[str, object]:
    """Metadata stored with an AsyncJob for automatic completion routing."""
    return {
        "session_id": session_id,
        "flow_id": flow_id,
        "completion_handlers": {
            "result": {
                "process_key": "service_interface::inference_service::process_results",
                "template": build_completion_template("result", verb_label),
                "notes": f"Marketo {verb_label} completed",
            },
            "error": {
                "process_key": "service_interface::inference_service::process_error",
                "template": build_completion_template("error", verb_label),
                "notes": f"Marketo {verb_label} failed",
            },
        },
    }
