#!/usr/bin/env python3
"""B5 preservation smoke: the single ActionFactory result/error-processor merge
that assembles a local-inference (``process_results`` / ``process_error``)
prompt from the inference base template + a process's customizations.

Track B (frontier-first consolidation) deleted ``ProcessorPolicyService`` (B1),
the exposure filter + ``field_sensitivities`` consumers (B2), and the now-optional
``field_sensitivities`` declarations (B4). B5 proves those deletions did NOT sever
the preserved capability: ``ActionFactory._get_result_processor_from_customizations``
/ ``_get_error_processor_from_customizations`` (``action_factory.py``) remain the
SOLE merge of a process's ``result_processor_customizations`` /
``error_processor_customizations`` onto the inference base template, and the
``inference`` result-processor-kind route still attaches that merge.

This smoke is the HERMETIC half of the B5 acceptance (offline, no live solet / LM
Studio — the gate_smokes.txt contract). It exercises the REAL merge method over a
registry whose ``process_results`` / ``process_error`` base templates mirror the
shipping shape (``ananta/knowledge_base/processes/inference_service/process_results.json``:
``params.model`` + ``params.prompt.observation`` carrying ``<<RESULT>>`` + ``params.prompt.user.output_schema``
= the actions decode contract). The live LM-Studio end-to-end run — a real
result-processing inference turn through the running platform — is the SEPARATE
acceptance evidence reported to the coordinator; it is intentionally NOT
gate-registered because gate smokes must pass offline.

Behavior-preserving slice: this is a regression guard. It goes RED if the merge
stops preserving the base template's ``output_schema`` (the decode contract), stops
inheriting the base arguments (e.g. ``model``), stops injecting the customization
guidance into ``prompt.user``, stops preserving the ``<<RESULT>>`` observation
plumbing, or stops routing the ``inference`` kind through the merge.

Run:

    .venv/bin/python3 ananta/tests/core/actions/action_factory_result_processor_merge_smoke.py
"""

from __future__ import annotations

import copy
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))

from ananta.core.actions.action_factory import ActionFactory  # noqa: E402

_RESULTS_BASE = "service_interface::inference_service::process_results"
_ERROR_BASE = "service_interface::inference_service::process_error"

# An EDGE read verb that declares result/error customizations — the shape the
# ActionFactory merges onto the inference base template at action-creation time.
_VERB = "service_interface::knowledge_service::search"
# A verb with NO customizations — exercises the "(optional) customizations" arm.
_VERB_NO_CUSTOM = "service_interface::vault_service::store"

# A distinctive marker in the base ``model`` slot; asserting it survives the merge
# proves the merged processor INHERITS the base template arguments (deep-copied),
# not just a hand-built prompt.
_BASE_MODEL_MARKER = {"name": "<<BASE_MODEL_MARKER>>"}
# The base ``prompt.user.output_schema`` (the actions decode contract). Asserting it
# is preserved onto the customization-replaced user section proves the merge
# "assembles from the base template" rather than discarding it.
_BASE_OUTPUT_SCHEMA = {
    "type": "object",
    "required": ["actions"],
    "properties": {"actions": {"type": "array", "_marker": "<<BASE_ACTIONS_SCHEMA>>"}},
}
_RESULT_CUSTOM = {
    "action_label": "Knowledge base searched",
    "output_action_guidance": "Summarize the retrieved knowledge for the user.",
    "presentation_guidance": "A concise bulleted list.",
}
_ERROR_CUSTOM = {
    "action_label": "Knowledge base search failed",
    "output_action_guidance": "Explain the failure and suggest a retry.",
}

_failures: list[str] = []


def _check(condition: bool, message: str) -> None:
    status = "PASS" if condition else "FAIL"
    print(f"  [{status}] {message}")
    if not condition:
        _failures.append(message)


def _base_prompt() -> dict[str, object]:
    """A base ``action_definition_template.arguments.params.prompt`` mirroring the
    shipping ``process_results.json`` shape (observation w/ ``<<RESULT>>`` +
    user.output_schema = actions decode contract)."""
    return {
        "message_rendering": {"ephemeral": False, "history_kind": "output_event"},
        "observation": {"action_result": {"action_status": "completed", "data": "<<RESULT>>"}},
        "user": {
            "instructions": ["<<BASE_INSTRUCTIONS — replaced by customizations>>"],
            "output_schema": copy.deepcopy(_BASE_OUTPUT_SCHEMA),
            "flow_input": "<<<:service_interface::flow_service::get_flow_input_for_presentation()>>>",
        },
    }


def _make_factory() -> ActionFactory:
    """ActionFactory over a minimal-but-faithful registry: the two inference
    base templates plus one verb with customizations and one without. No helper
    is stubbed — the real merge + the real ``_validate_action_legacy`` gate run."""
    registry: dict[str, object] = {
        "processes": {
            _RESULTS_BASE: {
                "action_definition_template": {
                    "arguments": {
                        "params": {
                            "model": copy.deepcopy(_BASE_MODEL_MARKER),
                            "prompt": _base_prompt(),
                        },
                    },
                },
            },
            _ERROR_BASE: {
                "action_definition_template": {
                    "arguments": {
                        "params": {
                            "model": copy.deepcopy(_BASE_MODEL_MARKER),
                            "prompt": _base_prompt(),
                        },
                    },
                },
            },
            _VERB: {
                "result_processor_customizations": copy.deepcopy(_RESULT_CUSTOM),
                "error_processor_customizations": copy.deepcopy(_ERROR_CUSTOM),
            },
            _VERB_NO_CUSTOM: {},
        },
    }
    return ActionFactory(process_registry=registry)


def _dict_at(container: dict[str, object], key: str) -> dict[str, object]:
    value = container.get(key)
    return value if isinstance(value, dict) else {}


def _arguments(processor: dict[str, object]) -> dict[str, object]:
    return _dict_at(processor, "arguments")


def _params(processor: dict[str, object]) -> dict[str, object]:
    return _dict_at(_arguments(processor), "params")


def _prompt(processor: dict[str, object]) -> dict[str, object]:
    return _dict_at(_params(processor), "prompt")


def _user(processor: dict[str, object]) -> dict[str, object]:
    return _dict_at(_prompt(processor), "user")


def _observation(processor: dict[str, object]) -> dict[str, object]:
    return _dict_at(_prompt(processor), "observation")


def _observation_result_data(processor: dict[str, object]) -> object:
    return _dict_at(_observation(processor), "action_result").get("data")


def _instructions(processor: dict[str, object]) -> list[str]:
    instr = _user(processor).get("instructions", [])
    return [str(x) for x in instr] if isinstance(instr, list) else []


def test_result_processor_merge_assembles_from_base_and_customizations() -> None:
    """Load-bearing: process_results result-processor = base template ⊕ customizations."""
    factory = _make_factory()
    processor = factory._get_result_processor_from_customizations(_VERB)

    _check(processor is not None, "merge returns a processor when customizations + base template exist")
    if processor is None:
        return

    _check(
        processor.get("process_key") == _RESULTS_BASE,
        "merged processor targets the local-inference process_results base (kind=inference sink)",
    )
    _check(
        _params(processor).get("model") == _BASE_MODEL_MARKER,
        "merged processor INHERITS the base params (model marker survived the deepcopy)",
    )
    _check(
        _user(processor).get("output_schema") == _BASE_OUTPUT_SCHEMA,
        "base prompt.user.output_schema (actions decode contract) is PRESERVED across the merge",
    )

    instr = _instructions(processor)
    _check(
        _RESULT_CUSTOM["output_action_guidance"] in instr,
        "customization output_action_guidance is injected into prompt.user.instructions",
    )
    _check(
        any("A concise bulleted list." in line for line in instr),
        "customization presentation_guidance is injected as a 'Response format:' instruction",
    )

    _check(
        _observation_result_data(processor) == "<<RESULT>>",
        "base observation <<RESULT>> plumbing (runtime result-substitution point) is preserved",
    )
    _check(
        _observation(processor).get("summary") == _RESULT_CUSTOM["action_label"],
        "observation is enriched with the customization action_label summary",
    )
    _check(
        _observation(processor).get("process_key") == _VERB,
        "observation is enriched with the source process_key",
    )


def test_error_processor_merge_assembles_from_process_error_base() -> None:
    """Mirror path: error-processor = process_error base ⊕ error customizations."""
    factory = _make_factory()
    processor = factory._get_error_processor_from_customizations(_VERB)

    _check(processor is not None, "error merge returns a processor when error customizations + base exist")
    if processor is None:
        return
    _check(
        processor.get("process_key") == _ERROR_BASE,
        "merged error processor targets the local-inference process_error base",
    )
    _check(
        _ERROR_CUSTOM["output_action_guidance"] in _instructions(processor),
        "error customization output_action_guidance is injected into the error prompt",
    )


def test_optional_customizations_arm_returns_none() -> None:
    """A verb with NO customizations yields no merged processor (base-only path)."""
    factory = _make_factory()
    _check(
        factory._get_result_processor_from_customizations(_VERB_NO_CUSTOM) is None,
        "no result_processor_customizations -> no merged processor ('(optional) customizations' arm)",
    )
    _check(
        factory._get_error_processor_from_customizations(_VERB_NO_CUSTOM) is None,
        "no error_processor_customizations -> no merged error processor",
    )


def test_merge_requires_the_real_base_template() -> None:
    """The merge assembles FROM the base template: absent the base, it declines
    (it does not hand-build a prompt)."""
    registry: dict[str, object] = {
        "processes": {_VERB: {"result_processor_customizations": copy.deepcopy(_RESULT_CUSTOM)}},
    }
    factory = ActionFactory(process_registry=registry)
    _check(
        factory._get_result_processor_from_customizations(_VERB) is None,
        "merge declines when the process_results base template is absent (assembles FROM base, "
        "never hardcodes the prompt)",
    )


def test_inference_kind_routes_through_merge_bridge_and_none_unaffected() -> None:
    """Route: _validate_action_legacy attaches the merge for kind=inference and
    leaves bridge_delivery + None untouched (behavior-preserving)."""
    factory = _make_factory()

    inference_def: dict[str, object] = {"process_key": _VERB, "result_processor_kind": "inference"}
    factory._validate_action_legacy(inference_def)
    _check(
        inference_def.get("result_processor") is not None
        and isinstance(inference_def["result_processor"], dict)
        and inference_def["result_processor"].get("process_key") == _RESULTS_BASE,
        "kind=inference action is attached the merged process_results processor (route intact)",
    )

    bridge_def: dict[str, object] = {"process_key": _VERB, "result_processor_kind": "bridge_delivery"}
    factory._validate_action_legacy(bridge_def)
    _check(
        "result_processor" not in bridge_def,
        "kind=bridge_delivery is NOT routed through the merge (bridge owns delivery) — unaffected",
    )

    none_def: dict[str, object] = {"process_key": _VERB, "result_processor_kind": None}
    factory._validate_action_legacy(none_def)
    _check(
        none_def.get("result_processor") is None,
        "kind=None stays terminal (EDGE_SINK) — not routed through the merge — unaffected",
    )


def main() -> int:
    print("B5 action_factory result/error-processor merge preservation smoke")
    test_result_processor_merge_assembles_from_base_and_customizations()
    test_error_processor_merge_assembles_from_process_error_base()
    test_optional_customizations_arm_returns_none()
    test_merge_requires_the_real_base_template()
    test_inference_kind_routes_through_merge_bridge_and_none_unaffected()
    if _failures:
        print(f"\nFAIL: {len(_failures)} check(s) failed")
        return 1
    print("\nPASS: the single ActionFactory merge assembles local-inference prompts "
          "from base ⊕ customizations; inference-kind route intact; bridge/None unaffected")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
