#!/usr/bin/env python3
"""Smoke test for the extended `_merge_process_json` contract.

Covers Step 1b/2.A.1 of the plugin-god-class remediation design record
§8.2.1 (dev-checkout workbench — not part of the shipped tree):

1. `_merge_process_json` lifts the 5 prose fields (`parameters`,
   `return_value_schema`, `complete_examples`, `error_cases`,
   `output_description`) from JSON into the registry entry. The existing
   merge contract (display_name / description / embedding_description /
   prompt_contract / customization fields) is preserved.
2. Structured customization fields (`field_sensitivities`, `blob_fields`,
   `retryable`) merge from JSON when the decorator omits them, via the
   pre-existing additive merge in `_merge_customization_fields`.
3. RELAXED 2026-07-15 (frontier-first consolidation): the post-merge
   both-blocks-required EDGE validator
   (`_validate_processor_customizations_post_merge`) is DELETED —
   customizations are optional on EDGE processes. Case 3 pins the deletion
   so a reintroduction has to consciously revisit the relaxed contract (see
   the frontier-first result-processing consolidation design record,
   dev-checkout workbench — not part of the shipped tree).

Project policy: no pytest. Exits 0 on success, 1 on first failure.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import cast

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))

from ananta.core.domain.enums import ProcessorPolicyCategory  # noqa: E402
from ananta.core.process_registry.kb_overlay_loader import (  # noqa: E402
    KnowledgeBaseOverlayLoader,
)

_passed = 0
_failed: list[str] = []


def _check(condition: object, label: str) -> None:
    global _passed
    if condition:
        _passed += 1
        print(f"  PASS  {label}")
    else:
        _failed.append(label)
        print(f"  FAIL  {label}")


def _make_builder() -> KnowledgeBaseOverlayLoader:
    """Construct a KnowledgeBaseOverlayLoader without its dependencies.

    Step 9.A decomposition moved `_merge_process_json` from
    `ProcessRegistryBuilder` to `KnowledgeBaseOverlayLoader`. The smoke
    only exercises the merge path, which does not touch the plugin
    manager or the invocation schema generator.
    """
    return KnowledgeBaseOverlayLoader.__new__(KnowledgeBaseOverlayLoader)


def _base_entry(category: object = ProcessorPolicyCategory.EDGE) -> dict[str, object]:
    """Return a minimal registry entry shaped like _build_plugin_process_entry."""
    return {
        "provider_type": "plugin",
        "provider": "fixture_plugin",
        "function_name": "fixture_action",
        "name": "fixture_action",
        "display_name": "old display",
        "description": "old description",
        "embedding_description": "old embedding",
        "parameters": {"old_param": {"description": "old", "required": True}},
        "parameter_schema": {"old_param": {"description": "old", "required": True}},
        "return_value_schema": None,
        "is_inference_capable": False,
        "is_enabled": True,
        "processor_policy_category": category,
        "result_processor_customizations": {
            "action_label": "old label",
            "field_sensitivities": [["legacy_field", 0.5]],
        },
        "error_processor_customizations": {
            "action_context": "old context",
            "retryable": False,
        },
    }


# ---------------------------------------------------------------------------
# Case 1 — merge contract covers all the new fields (one positive per field)
# ---------------------------------------------------------------------------


def _case_merge_prose_fields() -> None:
    print("\nCase 1: _merge_process_json lifts the 5 new prose fields from JSON")
    builder = _make_builder()
    entry = _base_entry()
    new_parameters = {
        "input_audio_files": {
            "description": "List of audio files",
            "required": True,
            "type": "LIST",
        }
    }
    new_return_value_schema = {
        "type": "object",
        "description": "Merged audio file blob",
        "properties": {"blob_id": {"type": "string"}},
    }
    new_complete_examples = [
        {"description": "merge two clips", "invocation": {}, "response": {}}
    ]
    new_error_cases = [
        {"condition": "fewer than 2 inputs", "error_response": {"code": "E1"}}
    ]
    new_output_description = "Concatenated audio file with blob ID"

    json_data: dict[str, object] = {
        "display_name": "Concatenate Audio",
        "description": "merged description",
        "embedding_description": "merged embedding " + "x" * 200,
        "parameters": new_parameters,
        "return_value_schema": new_return_value_schema,
        "complete_examples": new_complete_examples,
        "error_cases": new_error_cases,
        "output_description": new_output_description,
    }

    builder._merge_process_json(entry, json_data)

    _check(entry["display_name"] == "Concatenate Audio", "display_name override")
    _check(entry["description"] == "merged description", "description override")
    _check(entry["embedding_description"].startswith("merged embedding"), "embedding_description override")  # type: ignore[union-attr]
    _check(entry["parameters"] == new_parameters, "parameters lifted from JSON")
    _check(entry["parameter_schema"] == new_parameters, "parameter_schema kept in lockstep with parameters")
    _check(entry["return_value_schema"] == new_return_value_schema, "return_value_schema lifted")
    _check(entry["complete_examples"] == new_complete_examples, "complete_examples lifted")
    _check(entry["error_cases"] == new_error_cases, "error_cases lifted")
    _check(entry["output_description"] == new_output_description, "output_description lifted")


def _case_merge_falls_back_to_decorator_when_json_omits() -> None:
    print("\nCase 1b: missing JSON fields preserve the decorator-provided values")
    builder = _make_builder()
    entry = _base_entry()
    original_parameters = entry["parameters"]
    original_parameter_schema = entry["parameter_schema"]

    builder._merge_process_json(entry, {"display_name": "only_this"})

    _check(entry["display_name"] == "only_this", "display_name overridden")
    _check(entry["parameters"] is original_parameters, "parameters untouched when JSON omits")
    _check(
        entry["parameter_schema"] is original_parameter_schema,
        "parameter_schema untouched when JSON omits parameters",
    )
    _check(entry["return_value_schema"] is None, "return_value_schema untouched when JSON omits")
    _check("complete_examples" not in entry, "complete_examples not added when JSON omits")
    _check("error_cases" not in entry, "error_cases not added when JSON omits")
    _check("output_description" not in entry, "output_description not added when JSON omits")


def _case_merge_structured_customization_fields() -> None:
    print(
        "\nCase 1c: structured customization fields (field_sensitivities / blob_fields / retryable) "
        "fill in from JSON via existing additive merge"
    )
    builder = _make_builder()
    # Decorator omits both customizations entirely — the JSON must supply them.
    entry = _base_entry()
    entry["result_processor_customizations"] = {"action_label": "decorator label"}
    entry["error_processor_customizations"] = {"action_context": "decorator context"}

    json_data: dict[str, object] = {
        "result_processor_customizations": {
            "field_sensitivities": [["blob_id", 0.2], ["output_audio_file", 0.1]],
            "blob_fields": {"blob_id": "blob_id", "filename": "output_audio_file"},
        },
        "error_processor_customizations": {
            "retryable": True,
        },
    }

    builder._merge_process_json(entry, json_data)

    rpc_value = entry["result_processor_customizations"]
    assert isinstance(rpc_value, dict)
    rpc = cast(dict[str, object], rpc_value)
    _check(
        rpc.get("field_sensitivities") == [["blob_id", 0.2], ["output_audio_file", 0.1]],
        "field_sensitivities lifted from JSON when decorator omitted it",
    )
    _check(
        rpc.get("blob_fields") == {"blob_id": "blob_id", "filename": "output_audio_file"},
        "blob_fields lifted from JSON when decorator omitted it",
    )
    _check(
        rpc.get("action_label") == "decorator label",
        "decorator-defined action_label preserved (structured-field merge is additive)",
    )

    epc_value = entry["error_processor_customizations"]
    assert isinstance(epc_value, dict)
    epc = cast(dict[str, object], epc_value)
    _check(epc.get("retryable") is True, "retryable lifted from JSON when decorator omitted")
    _check(
        epc.get("action_context") == "decorator context",
        "decorator-defined action_context preserved",
    )


# ---------------------------------------------------------------------------
# Case 2 — post-merge validator accepts decorator that omits customizations
#           when the JSON supplies them
# ---------------------------------------------------------------------------


def _case_merge_fills_customizations_when_decorator_omits() -> None:
    print(
        "\nCase 2: _merge_process_json fills customizations from JSON when the "
        "decorator omitted them entirely"
    )
    builder = _make_builder()
    # Decorator-only build would have left these keys absent.
    entry = _base_entry()
    del entry["result_processor_customizations"]
    del entry["error_processor_customizations"]

    # JSON-driven merge fills them in.
    builder._merge_process_json(
        entry,
        {
            "result_processor_customizations": {
                "action_label": "filled by JSON",
                "field_sensitivities": [["x", 0.1]],
            },
            "error_processor_customizations": {
                "action_context": "filled by JSON",
                "retryable": True,
            },
        },
    )

    landed = entry.get("result_processor_customizations")
    assert isinstance(landed, dict)
    landed_dict = cast(dict[str, object], landed)
    _check(
        landed_dict.get("action_label") == "filled by JSON",
        "JSON-only result_processor_customizations landed on entry",
    )
    landed_error = entry.get("error_processor_customizations")
    _check(
        isinstance(landed_error, dict)
        and cast(dict[str, object], landed_error).get("retryable") is True,
        "JSON-only error_processor_customizations landed on entry",
    )


# ---------------------------------------------------------------------------
# Case 3 — the post-merge both-blocks FATAL is DELETED (relaxed 2026-07-15)
# ---------------------------------------------------------------------------


def _case_post_merge_validator_deleted() -> None:
    print(
        "\nCase 3: the post-merge both-blocks EDGE validator is deleted "
        "(customizations optional since the 2026-07-15 relax)"
    )
    builder = _make_builder()
    _check(
        not hasattr(builder, "_validate_processor_customizations_post_merge"),
        "no _validate_processor_customizations_post_merge on "
        "KnowledgeBaseOverlayLoader (reintroducing it must revisit the "
        "relaxed EDGE contract)",
    )


def main() -> int:
    print("Process registry merge smoke")
    print("====================================================")
    _case_merge_prose_fields()
    _case_merge_falls_back_to_decorator_when_json_omits()
    _case_merge_structured_customization_fields()
    _case_merge_fills_customizations_when_decorator_omits()
    _case_post_merge_validator_deleted()

    print("\n----------------------------------------------------")
    print(f"PASSED: {_passed}")
    print(f"FAILED: {len(_failed)}")
    if _failed:
        print("\nFailures:")
        for label in _failed:
            print(f"  - {label}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
