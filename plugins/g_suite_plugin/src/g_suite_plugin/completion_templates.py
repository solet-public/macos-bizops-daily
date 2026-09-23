"""Job metadata and completion-template helpers for the D0.3 deferred-completion shape.

Modeled on comfyui_image_generation_plugin/completion_templates.py and
cosyvoice2_tts_plugin/completion_templates.py — same AsyncJobManager
completion-routing mechanic (workbench/2026-08-09_sync_verb_d03_deferred_completion_doctrine_syncverb-doctrine.md
§2), applied to Google Workspace verbs instead of image/audio generation.
"""

from __future__ import annotations

from typing import Literal

_VERB_LABELS: dict[str, str] = {
    "drive_download_file": "Drive file download",
    "drive_upload_file": "Drive file upload",
    "sheets_create_from_files": "spreadsheet creation from files",
}


def build_completion_template(
    handler_type: Literal["result", "error"],
    verb: str,
    source_process_key: str,
) -> dict[str, object]:
    """Build the inference prompt template AsyncJobManager attaches on completion."""
    label = _VERB_LABELS.get(verb, verb)
    if handler_type == "result":
        instructions = [
            f"A Google Workspace job ({label}) completed successfully.",
            "Write a brief, friendly message confirming the result.",
            "DO NOT mention blob keys, blob://, technical IDs, file paths, or internal "
            "system details.",
            "Reply with action using the active IO plugin's post_message "
            "(process_key: plugin::<io_plugin>::post_message).",
            "Set attachments to [] (empty list).",
        ]
    else:
        instructions = [
            f"A Google Workspace job ({label}) ended with an error.",
            "Explain what went wrong in simple terms the user can understand.",
            "DO NOT dump stack traces, blob keys, or internal system details.",
            "Reply with action using the active IO plugin's post_message "
            "(process_key: plugin::<io_plugin>::post_message).",
            "Set attachments to [] (empty list).",
        ]

    return {
        "params": {
            "model": {"temperature": 0.4, "max_tokens": 2048},
            "prompt": {
                "observation": {
                    "process_key": source_process_key,
                    "action_result": {},
                },
                "user": {
                    "instructions": instructions,
                    "result_data": {},
                    "action_result": {},
                    "flow_input": {"original_input": ""},
                },
            },
        },
    }


def build_job_metadata(
    session_id: str,
    flow_id: str,
    verb: str,
    process_key: str,
) -> dict[str, object]:
    """Metadata stored with the AsyncJob for automatic completion routing (D0.3 mechanic 1)."""
    return {
        "session_id": session_id,
        "flow_id": flow_id,
        "completion_handlers": {
            "result": {
                "process_key": "service_interface::inference_service::process_results",
                "template": build_completion_template("result", verb, process_key),
                "notes": f"g_suite_plugin {verb} completed",
            },
            "error": {
                "process_key": "service_interface::inference_service::process_error",
                "template": build_completion_template("error", verb, process_key),
                "notes": f"g_suite_plugin {verb} failed",
            },
        },
    }
