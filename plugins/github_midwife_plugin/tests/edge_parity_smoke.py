"""Slice G smoke — EDGE-parity + genesis-entrypoint importability.

Pins the "FATAL trap" the create-process skill warns about: every
EDGE-category `@platform_process` verb must have a matching
`EdgeProcessDefinition` entry in `get_edge_process_definitions()` (a
mismatch is a startup-fatal `process_registry.edge_process_mismatch`).
Also confirms the plugin's `entry-points` wiring resolves and that
`genesis.py`'s public entrypoints import cleanly.

Run directly: ``SOLET_NAME=<name> .venv/bin/python3
plugins/github_midwife_plugin/tests/edge_parity_smoke.py``.
"""

from __future__ import annotations

# ruff: noqa: E402
import sys
from importlib.metadata import entry_points
from pathlib import Path

_PLUGIN_ROOT = Path(__file__).resolve().parents[1]
_SRC = _PLUGIN_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from ananta.core.domain.enums import ProcessorPolicyCategory  # noqa: E402
from github_midwife_plugin.plugin import GithubMidwifePlugin  # noqa: E402

_CHECKS_RUN: list[str] = []


class SmokeFailureError(AssertionError):
    """Raised on any check failure; message is the failure detail."""


def _check(label: str, condition: bool, detail: str = "") -> None:
    _CHECKS_RUN.append(label)
    if not condition:
        raise SmokeFailureError(f"{label}: {detail}")


def _check_entry_point_resolves() -> None:
    eps = entry_points(group="ananta.plugins")
    matches = [ep for ep in eps if ep.name == "github_midwife_plugin"]
    _check("github_midwife_plugin is registered under the ananta.plugins entry-point group", len(matches) == 1, str(list(eps)))
    loaded = matches[0].load()
    _check(
        "the entry-point resolves to GithubMidwifePlugin",
        loaded is GithubMidwifePlugin,
        f"got {loaded!r}",
    )


def _check_plugin_instantiates_and_is_ready_after_prepare() -> None:
    plugin = GithubMidwifePlugin()
    _check("plugin name is github_midwife_plugin", plugin.name == "github_midwife_plugin", plugin.name)
    plugin.prepare_for_readiness()
    _check("prepare_for_readiness marks the plugin ready", plugin.is_ready(), "set_ready() was not called")


# verb name -> the @platform_process action-wrapper method attribute. BIRTH-ONLY
# since the 2026-07-20 split: the seed-factory MINT/PUBLISH verbs moved to
# seed_factory_plugin (covered by its own edge_parity_smoke). This plugin — the
# birth spine — exposes exactly the one genesis verb.
_EDGE_VERBS: dict[str, str] = {
    "birth_solet": "birth_solet_action",
}


def _check_every_edge_verb_is_declared_and_correctly_categorized() -> None:
    """The FATAL trap: every EDGE-category @platform_process verb needs a
    matching EdgeProcessDefinition, and vice versa -- the dict must not
    list a non-EDGE method or miss one (either triggers
    process_registry.edge_process_mismatch at startup). Pins the exact
    verb set so a stray or dropped entry fails here, not at boot.
    """
    plugin = GithubMidwifePlugin()
    edge_defs = plugin.get_edge_process_definitions()
    _check(
        "get_edge_process_definitions declares exactly the EDGE verb set (birth_solet only, post-split)",
        set(edge_defs.keys()) == set(_EDGE_VERBS),
        str(sorted(edge_defs.keys())),
    )

    for verb_name, method_attr in _EDGE_VERBS.items():
        action_method = getattr(plugin, method_attr)
        metadata = getattr(action_method, "_platform_process_metadata", None)
        if metadata is None:
            raise SmokeFailureError(f"{method_attr} carries @platform_process metadata: got None")
        _CHECKS_RUN.append(f"{method_attr} carries @platform_process metadata")
        _check(
            f"{method_attr}'s metadata name matches its EdgeProcessDefinition key",
            metadata.name == verb_name,
            metadata.name,
        )
        _check(
            f"{method_attr} is categorized EDGE (required for an EdgeProcessDefinition entry to be valid)",
            metadata.processor_policy_category == ProcessorPolicyCategory.EDGE,
            str(metadata.processor_policy_category),
        )
        _check(
            f"the EdgeProcessDefinition is declared for '{verb_name}' (parity)",
            verb_name in edge_defs,
            str(sorted(edge_defs)),
        )


def _check_return_value_schema_matches_birth_result_fields() -> None:
    """Pin against the exact class of drift the create-process skill warns
    about: `return_value_schema` must name every key `_birth_result_to_dict`
    actually returns, no more, no less.
    """
    plugin = GithubMidwifePlugin()
    metadata = plugin.birth_solet_action._platform_process_metadata  # noqa: SLF001
    schema_fields = set(metadata.return_value_schema.properties.keys())

    from ananta.interfaces.lifecycle_result_types import BirthResult, BirthStatus

    fake_result = BirthResult(
        status=BirthStatus.SUCCESS, solet_name="x", idempotency_key="y",
        dry_run=False, steps=(), new_solet_endpoint="", manifest_path="",
        iam_roles_created=(), rds_endpoint="", kms_key_arn="", message="",
    )
    returned_fields = set(GithubMidwifePlugin._birth_result_to_dict(fake_result).keys())  # noqa: SLF001
    _check(
        "return_value_schema properties exactly match _birth_result_to_dict's actual keys",
        schema_fields == returned_fields,
        f"schema={schema_fields!r} actual={returned_fields!r}",
    )


def _check_genesis_entrypoints_importable() -> None:
    from github_midwife_plugin import genesis

    for attr in ("run_genesis", "main", "GenesisError", "_resolve_clone_root"):
        _check(f"genesis.{attr} is importable", hasattr(genesis, attr), attr)


def _check_kb_process_json_exists_and_matches_process_key() -> None:
    """The dual-write pin: EVERY registered verb has a matching KB process
    JSON at the right path with the canonical process_key and all required
    keys (a missing JSON is a startup fail-fast; the create-process skill's
    core contract). Covers all three EDGE verbs.
    """
    import json
    from pathlib import Path

    processes_dir = Path(__file__).resolve().parents[1] / "knowledge_base" / "processes"
    for verb_name in _EDGE_VERBS:
        kb_json_path = processes_dir / f"{verb_name}.json"
        _check(f"the KB process JSON for '{verb_name}' exists", kb_json_path.is_file(), str(kb_json_path))
        payload = json.loads(kb_json_path.read_text())
        _check(
            f"the '{verb_name}' KB process JSON's process_key matches the runtime registration",
            payload.get("process_key") == f"plugin::github_midwife_plugin::{verb_name}",
            payload.get("process_key"),
        )
        for required_key in ("display_name", "description", "embedding_description", "result_processor_customizations", "error_processor_customizations"):
            _check(f"the '{verb_name}' KB process JSON has '{required_key}'", required_key in payload, str(payload.keys()))


def _check_kb_process_json_reflects_existing_clone_only() -> None:
    """Behavioral-claim pin (SF-D, 2026-07-18): acquisition mode
    (clone-of-pinned-upstream into an absent/empty target) was RETIRED, and
    the venv seam is now the explicit provision_venv birth variant. This is a
    behavioral pin, not a brittle exact-text match, so it survives future
    prose rewrites while catching a re-introduced stale claim: the JSON must
    describe existing-clone mode + the provision_venv variant, must describe
    acquisition as retired (never as an active mode), and must not claim
    genesis is unimplemented.
    """
    import json
    from pathlib import Path

    kb_json_path = (
        Path(__file__).resolve().parents[1]
        / "knowledge_base" / "processes" / "birth_solet.json"
    )
    payload = json.loads(kb_json_path.read_text())
    full_text = (
        str(payload.get("description", "")) + " "
        + str(payload.get("embedding_description", "")) + " "
        + json.dumps(payload.get("error_processor_customizations", {}))
    ).lower()

    _check(
        "the JSON describes existing-clone mode",
        "existing-clone mode" in full_text or "existing clone" in full_text,
        full_text,
    )
    _check(
        "the JSON describes the provision_venv birth variant (the explicit venv seam)",
        "provision_venv" in full_text,
        full_text,
    )
    _check(
        "the JSON describes acquisition mode as RETIRED (not an active mode)",
        "acquisition" in full_text and "retired" in full_text,
        full_text,
    )
    _check(
        "the JSON describes the newborn per-solet credential self-seed / isolation",
        ("self-seed" in full_text) or ("credential" in full_text and "isolation" in full_text),
        full_text,
    )
    _check(
        "the JSON does not claim genesis acquisition is unimplemented",
        "not yet implemented" not in full_text and "not yet wired" not in full_text,
        full_text,
    )


def main() -> int:
    try:
        _check_entry_point_resolves()
        _check_plugin_instantiates_and_is_ready_after_prepare()
        _check_every_edge_verb_is_declared_and_correctly_categorized()
        _check_return_value_schema_matches_birth_result_fields()
        _check_genesis_entrypoints_importable()
        _check_kb_process_json_exists_and_matches_process_key()
        _check_kb_process_json_reflects_existing_clone_only()
    except SmokeFailureError as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        print(f"  ({len(_CHECKS_RUN)} checks attempted before failure)", file=sys.stderr)
        return 1

    print(f"edge_parity_smoke OK: {len(_CHECKS_RUN)} checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
