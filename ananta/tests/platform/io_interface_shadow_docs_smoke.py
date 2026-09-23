#!/usr/bin/env python3
"""Smoke guidance away from the unbound io-interface process keys.

The disabled JSON retirement is deferred to iss_8f6e04e2. Killing mutations:
restore either unbound key or caller-owned session_id in a teaching surface,
or change either disabled decorator/comment in public.py.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
_UNBOUND_KEYS = (
    "service_interface::io_interface_service::post_message",  # wint:negative-fixture
    "service_interface::io_interface_service::deliver_artifact",  # wint:negative-fixture
)
_GUIDANCE_PATHS = (
    "ananta/knowledge_base/processes/discovery_service/execute_embeddings_search.json",
    "ananta/knowledge_base/processes/discovery_service/get_process_schema.json",
    "ananta/knowledge_base/processes/discovery_service/query_process_registry.json",
    "ananta/knowledge_base/processes/inference_service/process_error.json",
    "ananta/knowledge_base/processes/inference_service/process_results.json",
    "ananta/knowledge_base/responsiveness_and_checkins.md",
    "ananta/knowledge_base/scheduling_memory_driven.md",
    "ananta/src/ananta/services/inference_service/prompts/process_results_decision_tree.md",
)

# Teaching surfaces that exist in the ORIGIN checkout but are absent from a born
# clone AT SMOKE-RUN TIME, so they are asserted only when present.
#
# This is not leniency, and the distinction matters: everything in
# ``_GUIDANCE_PATHS`` above is read UNGUARDED, so a missing one is still a hard
# failure. Only the paths here are conditional, and each is conditional for a
# measured, structural reason rather than because it was inconvenient.
#
# ``profile/config/prompts/thinking_system_prompt.md`` — ``profile/config/`` is
# ``never_copy`` in the seed manifest (vault keys, operator identity, runtime
# state) and ``profile/config/prompts`` ships as an EMPTY ``create_dirs``
# scaffold that genesis populates. Measured on the r21 born-clone verdict
# (sealed 254700866ab0…): this smoke was 1 of 14 BLOCKING failures, raising
# FileNotFoundError on exactly this path after every other assertion had passed.
_ORIGIN_ONLY_GUIDANCE_PATHS = (
    "profile/config/prompts/thinking_system_prompt.md",
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


def _case_guidance_uses_resolved_plugin_pattern() -> None:
    print("\nCase 1: teaching surfaces use the resolved plugin pattern")
    for rel_path in _GUIDANCE_PATHS:
        text = (REPO_ROOT / rel_path).read_text(encoding="utf-8")
        _check("plugin::<namespace>::post_message" in text, f"{rel_path} names plugin namespace")
        _check("query_process_registry" in text, f"{rel_path} names namespace resolution")
        for unbound_key in _UNBOUND_KEYS:
            _check(unbound_key not in text, f"{rel_path} excludes {unbound_key}")
        _check("session_id AND message" not in text, f"{rel_path} excludes caller session_id")

    for rel_path in _ORIGIN_ONLY_GUIDANCE_PATHS:
        path = REPO_ROOT / rel_path
        if not path.is_file():
            print(f"  SKIP  {rel_path}: genesis-populated, absent pre-birth (not shipped by assembly)")
            continue
        text = path.read_text(encoding="utf-8")
        _check("plugin::<namespace>::post_message" in text, f"{rel_path} names plugin namespace")
        _check("query_process_registry" in text, f"{rel_path} names namespace resolution")
        for unbound_key in _UNBOUND_KEYS:
            _check(unbound_key not in text, f"{rel_path} excludes {unbound_key}")
        _check("session_id AND message" not in text, f"{rel_path} excludes caller session_id")

    for rel_path in (
        "ananta/knowledge_base/responsiveness_and_checkins.md",
        "ananta/knowledge_base/scheduling_memory_driven.md",
    ):
        text = (REPO_ROOT / rel_path).read_text(encoding="utf-8")
        _check("SESSION_AWARE binds the session" in text, f"{rel_path} forbids caller session ownership")
        _check("only when that namespace schema declares them" in text, f"{rel_path} scopes attachment extras")


def _case_plugin_shapes_are_distinct() -> None:
    print("\nCase 2: plugin-specific schemas control post_message extras")
    # agent_messaging_plugin is in every capability bundle, so its leg is
    # unconditional: a missing source here is a real failure, not an absence.
    agent_source = (REPO_ROOT / "plugins/agent_messaging_plugin/src/agent_messaging_plugin/plugin.py").read_text(
        encoding="utf-8"
    )
    _check("context_handling=ContextHandling.SESSION_AWARE" in agent_source, "agent_messaging binds session from context")
    _check('params.pop("attachments", None)' in agent_source, "agent_messaging strips attachment hints")
    _check('params.pop("job_result_ref", None)' in agent_source, "agent_messaging strips job-result hints")

    # discord_plugin is resolved PER PROFILE and is absent from macos-bizops. The
    # subject of this case is that plugin-specific schemas differ, so the roster is
    # intersected with the plugins actually present rather than hardcoded — the
    # repair the seed manifest's ★ standard already prescribes (and the same one
    # applied to hydration_guidance_convention_smoke). Where discord_plugin DOES
    # ship, every assertion below still runs; where it does not, the case keeps its
    # agent_messaging coverage instead of failing the whole clone.
    discord_path = REPO_ROOT / "plugins/discord_plugin/src/discord_plugin/plugin.py"
    if discord_path.is_file():
        discord_source = discord_path.read_text(encoding="utf-8")
        _check("context_handling=ContextHandling.SESSION_AWARE" in discord_source, "discord binds session from context")
        _check('"attachments": ParameterMetadata(' in discord_source, "discord declares optional attachments")
    else:
        print("  SKIP  discord_plugin: not in this profile's resolved plugin set")


def _case_disabled_design_record_stays() -> None:
    print("\nCase 3: disabled declarations retain their rationale")
    source = (REPO_ROOT / "ananta/src/ananta/services/io_interface_service/interfaces/public.py").read_text(
        encoding="utf-8"
    )
    _check(
        "is_enabled=False,  # Model addresses IO plugins directly (plugin::<ns>::post_message)" in source,
        "post_message remains disabled with direct-plugin rationale",
    )
    _check(
        "is_enabled=False,  # Internal-only — model addresses IO plugins directly" in source,
        "deliver_artifact remains disabled with direct-plugin rationale",
    )


def main() -> int:
    print("Smoke: io-interface post_message guidance")
    _case_guidance_uses_resolved_plugin_pattern()
    _case_plugin_shapes_are_distinct()
    _case_disabled_design_record_stays()
    print(f"\n{_passed} passed, {len(_failed)} failed")
    if _failed:
        for label in _failed:
            print(f"  - {label}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
