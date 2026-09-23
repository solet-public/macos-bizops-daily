"""Slice D — no-parent profile materialization (Layer 1).

Adapts `macos_midwife_plugin/config_generator.py`'s writer pattern to
genesis's no-parent situation: `config_generator.py` reads baseline
per-plugin configs from a *running parent's*
`source_profile_dir/config/plugins` (own-copy per convention — the
birther is designed around a live parent supplying the baseline;
genesis has no parent). This module reads the plugin-owned, checked-in
`profile_baseline/*.json` templates (Slice A) instead — never a
parent's `profile/` path.

Scope (Slice D): `manifest.yaml`, `service_bindings.json`,
`starting_action_definitions.json`, the per-plugin configs copied from
`profile_baseline/` (+ `plugin_config_overrides`), and boot-required global
identity/prompt files. `default_thinking_plugin` still owns its optional
per-plugin prompt override, but the core prompt formatter hard-requires
`profile/config/prompts/system.json` when real inference/memory paths run, so
genesis materializes a conservative default here.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import yaml

from .constants import PROFILE_BASELINE_SUBDIR, PROFILE_TEMPLATES_SUBDIR

# The plugin whose boot-time seed loader reads the entries file this module
# materializes (finding F9). Its `prepare_for_readiness` calls
# `seed_loader.auto_seed_entries_from_file`, which reads
# `<app_home>/config/plugins/default_address_book_plugin/entries.json`.
_ADDRESS_BOOK_PLUGIN = "default_address_book_plugin"

# Placeholder the static templates carry where the newborn's own name belongs
# (e.g. the pgvector_service_db database/db_schema, or the postgres connect
# `user`). Substituted with the EXPLICIT newborn name at write time -- keeps the
# templates static and operator-identity-free (finding F11, Architect
# 2026-07-11; extended to plugin_config_overrides for per-role isolation,
# 2026-07-12).
_SOLET_NAME_PLACEHOLDER = "${SOLET_NAME}"

# The state plugin whose materialized config MUST connect as this newborn's OWN
# role (per-solet isolation, operator override 2026-07-12) -- verified
# fail-loud after overrides are applied, for any profile that uses it.
_STATE_PLUGIN = "postgres_state_management_plugin"

# The knowledge plugin whose materialized config MUST carry the
# per-installation `knowledge_base_root` (the profile_baseline README's Slice D
# contract; cold-run finding D1, 2026-07-13). The plugin has no
# built-in default and raises `knowledge_base_root not configured` at
# readiness, so a missed injection crash-loops every newborn at first boot.
_KNOWLEDGE_PLUGIN = "default_knowledge_plugin"
_KNOWLEDGE_BASE_ROOT_KEY = "knowledge_base_root"
# The symlink-aggregation dir genesis's materialize_kb_symlinks step creates
# under the clone root -- the same value macos_midwife's knowledge_base_symlinker
# writes for its newborns.
_KNOWLEDGE_BASES_DIRNAME = "knowledge_bases"

# Runtime-REQUIRED empty directories a git clone cannot carry (git tracks no
# empty dirs). seed_manifest.yaml's `create_dirs` documents this same set as
# "Empty directories genesis populates" -- THIS is that population step; the
# assemble-time scaffold cannot survive the seal-commit -> clone round trip
# (cold-run finding D9, 2026-07-13: a missing profile/app crashed
# PlatformServicesManager at init_actions on every seed-born boot).
_RUNTIME_SCAFFOLD_DIRS: tuple[str, ...] = (
    "profile/app",
    "profile/config/plugins",
    "profile/config/prompts",
    "profile/config/disabled_plugins",
    "profile/data/logs",
)

# startup_sequence.seed_identity_memories hard-requires this file at boot
# (fail-loud, no default) -- cold-run finding D7, 2026-07-13. The
# original Slice D scope note deferred identity as "not a boot blocker" from
# default_thinking_plugin's graceful degradation, but seed_identity_memories
# is a DIFFERENT consumer that crashes the orchestrator without it.
_IDENTITY_FILENAME = "identity.json"
_GLOBAL_SYSTEM_PROMPT_FILENAME = "system.json"


class ConfigMaterializeError(RuntimeError):
    """Raised when a required input is missing or malformed."""


def load_profile(kb_root: Path, profile_name: str) -> dict[str, Any]:
    """Load `<kb_root>/profile_templates/<profile_name>.yaml` as a raw dict.

    Deliberately NOT `macos_midwife_plugin.profile_template_loader`'s
    typed `ProfileTemplate` dataclass (own-copy per convention) — this
    module works directly off the raw mapping since it needs the whole
    profile shape (plugins, service_bindings, starting_actions,
    plugin_config_overrides), not just the allowlist
    `profile_install.load_plugin_allowlist` reads.
    """
    path = kb_root / PROFILE_TEMPLATES_SUBDIR / f"{profile_name}.yaml"
    if not path.is_file():
        raise ConfigMaterializeError(f"profile template not found: {path}")
    raw: Any = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ConfigMaterializeError(
            f"profile template did not parse to a mapping: {path}"
        )
    return raw


def write_json(path: Path, data: Any) -> None:
    """Write `data` to `path` as pretty-printed JSON with a trailing newline."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n")


def write_runtime_manifest(target: Path, profile: dict[str, Any]) -> Path:
    """Write `<target>/profile/config/manifest.yaml`."""
    path = target / "profile" / "config" / "manifest.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "profile_name": profile.get("profile_name", ""),
        "plugins": list(profile.get("plugins") or []),
    }
    path.write_text(yaml.safe_dump(payload, sort_keys=False, default_flow_style=False))
    return path


def write_service_bindings(target: Path, profile: dict[str, Any]) -> Path:
    """Write `<target>/profile/config/service_bindings.json`."""
    path = target / "profile" / "config" / "service_bindings.json"
    write_json(path, dict(profile.get("service_bindings") or {}))
    return path


def write_starting_actions(target: Path, profile: dict[str, Any]) -> Path:
    """Write `<target>/profile/config/starting_action_definitions.json`."""
    path = target / "profile" / "config" / "starting_action_definitions.json"
    write_json(path, list(profile.get("starting_actions") or []))
    return path


def copy_baseline_plugin_configs(
    target: Path, kb_root: Path, profile: dict[str, Any],
) -> list[Path]:
    """Copy the checked-in `profile_baseline/*.json` templates, filtered to
    the profile's plugin allowlist — mirrors
    `config_generator.copy_profile_filtered_plugin_configs`'s filtering,
    reading from the plugin-owned baseline instead of a parent's
    `profile/config/plugins/`.
    """
    baseline_dir = kb_root / PROFILE_BASELINE_SUBDIR
    dest_dir = target / "profile" / "config" / "plugins"
    dest_dir.mkdir(parents=True, exist_ok=True)
    if not baseline_dir.is_dir():
        return []

    plugin_set = set(profile.get("plugins") or [])
    written: list[Path] = []
    for config_file in sorted(baseline_dir.glob("*.json")):
        if config_file.stem not in plugin_set:
            continue
        dst = dest_dir / config_file.name
        if dst.exists():
            continue
        raw = json.loads(config_file.read_text(encoding="utf-8"))
        write_json(dst, raw)
        written.append(dst)
    return written


def apply_profile_overrides(target: Path, profile: dict[str, Any], name: str) -> list[Path]:
    """Merge each `plugin_config_overrides` entry into the materialized config,
    substituting `${SOLET_NAME}` in override values with the newborn's
    actual name at write time.

    The `${SOLET_NAME}` substitution rides the SAME idiom as
    `write_address_book_entries` (one substitution mechanism, two writers): the
    per-solet-role ruling (2026-07-12) needs the newborn's
    `postgres_state_management_plugin` config to connect as its OWN role, so the
    profile declares `plugin_config_overrides.postgres_state_management_plugin.user:
    ${SOLET_NAME}` and it resolves here to the newborn name. Uses the
    EXPLICIT `name` argument (the newborn), NEVER os.environ["SOLET_NAME"]
    -- in verb-mode this runs in the PARENT's process. A single
    serialize/replace/parse keeps it value-agnostic (every string field is
    covered, not just known keys).
    """
    dest_dir = target / "profile" / "config" / "plugins"
    written: list[Path] = []
    overrides = profile.get("plugin_config_overrides") or {}
    for plugin_name, override in overrides.items():
        if not isinstance(override, dict):
            continue
        substituted = json.loads(json.dumps(override).replace(_SOLET_NAME_PLACEHOLDER, name))
        config_path = dest_dir / f"{plugin_name}.json"
        existing: dict[str, Any] = {}
        if config_path.is_file():
            parsed = json.loads(config_path.read_text(encoding="utf-8"))
            if isinstance(parsed, dict):
                existing = parsed
        existing.update(substituted)
        write_json(config_path, existing)
        written.append(config_path)
    return written


def create_runtime_scaffold_dirs(target: Path) -> list[Path]:
    """Idempotently create the runtime-required directory scaffold (D9).

    Returns the directories created THIS call (already-present ones are left
    alone) -- mirrors the create-only, never-clobber discipline of the other
    writers.
    """
    created: list[Path] = []
    for rel in _RUNTIME_SCAFFOLD_DIRS:
        path = target / rel
        if not path.is_dir():
            path.mkdir(parents=True, exist_ok=True)
            created.append(path)
    return created


def write_identity_config(target: Path, name: str) -> list[Path]:
    """Write-if-absent `profile/config/identity.json` with a minimal bootable
    identity (D7).

    Write-if-absent is the F10 vault-passphrase idiom: a genesis re-run never
    clobbers operator-customized identity strings. The default is one string
    naming the newborn (first letter capitalized, matching the seed convention) --
    the operator refines personality later; genesis's job is only that the
    newborn BOOTS.
    """
    path = target / "profile" / "config" / _IDENTITY_FILENAME
    if path.exists():
        return []
    display = name[:1].upper() + name[1:]
    write_json(path, {
        "identity": [f"My name is {display}. I am a friendly and professional assistant."],
    })
    return [path]


def write_global_system_prompt_config(target: Path, name: str) -> list[Path]:
    """Write-if-absent `profile/config/prompts/system.json`.

    The core prompt formatter loads this file without fallback, so genesis must
    provide a valid minimal shape even though richer prompt tuning is
    operator/profile work. A rerun preserves any local customization.
    """
    path = target / "profile" / "config" / "prompts" / _GLOBAL_SYSTEM_PROMPT_FILENAME
    if path.exists():
        return []
    display = name[:1].upper() + name[1:]
    write_json(path, {
        "prompt": {
            "system": (
                f"You are {display}, a local solet. Use the available "
                "platform services carefully, preserve operator privacy, and "
                "answer with clear operational judgment."
            ),
        },
    })
    return [path]


def inject_knowledge_base_root(target: Path, profile: dict[str, Any]) -> list[Path]:
    """Merge the per-installation `knowledge_base_root` into the materialized
    `default_knowledge_plugin` config, IFF the profile loads that plugin.

    The value is `<clone_root>/knowledge_bases` -- absolute, determined by
    where the user cloned the repo, so it can never live in the static
    checked-in baseline (profile_baseline README: "Slice D MUST set it").
    Merges onto whatever the baseline + overrides produced (other keys
    survive); runs AFTER `apply_profile_overrides` so no override can clobber
    it back out.
    """
    if _KNOWLEDGE_PLUGIN not in set(profile.get("plugins") or []):
        return []
    config_path = target / "profile" / "config" / "plugins" / f"{_KNOWLEDGE_PLUGIN}.json"
    existing: dict[str, Any] = {}
    if config_path.is_file():
        parsed = json.loads(config_path.read_text(encoding="utf-8"))
        if isinstance(parsed, dict):
            existing = parsed
    existing[_KNOWLEDGE_BASE_ROOT_KEY] = str(target / _KNOWLEDGE_BASES_DIRNAME)
    write_json(config_path, existing)
    return [config_path]


def _verify_knowledge_base_root_configured(target: Path, profile: dict[str, Any]) -> None:
    """Fail loud unless the materialized `default_knowledge_plugin` config
    carries the exact `knowledge_base_root` its readiness hook demands.

    Guards writer ordering the same way `_verify_postgres_user_isolated`
    guards the profile override: this exact key was a documented MUST that
    silently went unimplemented (cold-run D1) -- a genesis-time raise is the
    difference between the driving agent seeing the gap and the newborn
    crash-looping at first boot.
    """
    if _KNOWLEDGE_PLUGIN not in set(profile.get("plugins") or []):
        return
    config_path = target / "profile" / "config" / "plugins" / f"{_KNOWLEDGE_PLUGIN}.json"
    if not config_path.is_file():
        raise ConfigMaterializeError(
            f"{_KNOWLEDGE_PLUGIN} is in the profile's plugins but its config was not "
            f"materialized at {config_path} -- the newborn would crash-loop at "
            "readiness (knowledge_base_root not configured)."
        )
    parsed = json.loads(config_path.read_text(encoding="utf-8"))
    resolved = parsed.get(_KNOWLEDGE_BASE_ROOT_KEY) if isinstance(parsed, dict) else None
    expected = str(target / _KNOWLEDGE_BASES_DIRNAME)
    if resolved != expected:
        raise ConfigMaterializeError(
            f"{_KNOWLEDGE_PLUGIN} config `{_KNOWLEDGE_BASE_ROOT_KEY}` resolved to "
            f"{resolved!r}, expected {expected!r} -- the injection was clobbered or "
            "skipped; the newborn would crash-loop at readiness."
        )


def _verify_postgres_user_isolated(target: Path, profile: dict[str, Any], name: str) -> None:
    """Fail loud (per-role isolation, operator override 2026-07-12) unless the
    materialized `postgres_state_management_plugin` config connects as this
    newborn's OWN role (`user` == `name`).

    Runs only for profiles that actually USE the state plugin. Catches a
    profile that forgot the
    `plugin_config_overrides.postgres_state_management_plugin.user:
    ${SOLET_NAME}` declaration (user stays the retired shared `ananta`
    baseline) or a substitution that failed to resolve (`${...}` left literal)
    -- either would silently boot the newborn connecting as the wrong role.
    """
    if _STATE_PLUGIN not in set(profile.get("plugins") or []):
        return
    config_path = target / "profile" / "config" / "plugins" / f"{_STATE_PLUGIN}.json"
    if not config_path.is_file():
        raise ConfigMaterializeError(
            f"{_STATE_PLUGIN} is in the profile's plugins but its config was not "
            f"materialized at {config_path} -- cannot verify per-solet role "
            "isolation."
        )
    parsed = json.loads(config_path.read_text(encoding="utf-8"))
    resolved_user = parsed.get("user") if isinstance(parsed, dict) else None
    if resolved_user != name:
        raise ConfigMaterializeError(
            f"{_STATE_PLUGIN} config `user` resolved to {resolved_user!r}, expected "
            f"{name!r} (this solet's own role). Declare "
            f"`plugin_config_overrides.{_STATE_PLUGIN}.user: {_SOLET_NAME_PLACEHOLDER}` "
            "in the profile template so the newborn connects as its OWN role, not the "
            "retired shared `ananta` role (per-solet isolation, 2026-07-12)."
        )


def _validate_address_book_entry(entry: Any, index: int) -> dict[str, Any]:
    """Top-level shape check ONLY (Architect ruling R2, 2026-07-11) — the
    field-level contract (field_type/description/value) stays the consumer's
    (`default_address_book_plugin.seed_loader`) authority. This moves a
    profile-template typo from a newborn boot crash-loop (seed_loader raises
    at startup) to a genesis-time `ConfigMaterializeError` the driving agent
    actually sees.
    """
    if not isinstance(entry, dict):
        raise ConfigMaterializeError(f"address_book_entries[{index}] must be a mapping")
    name = entry.get("name")
    if not isinstance(name, str) or not name:
        raise ConfigMaterializeError(
            f"address_book_entries[{index}] missing a non-empty string 'name'"
        )
    address_type = entry.get("address_type")
    if not isinstance(address_type, str) or not address_type:
        raise ConfigMaterializeError(
            f"address_book_entries entry {name!r} missing a non-empty string 'address_type'"
        )
    field_entries = entry.get("entries")
    if not isinstance(field_entries, list) or not field_entries:
        raise ConfigMaterializeError(
            f"address_book_entries entry {name!r} must have a non-empty 'entries' list"
        )
    return entry


def write_address_book_entries(
    target: Path, profile: dict[str, Any], name: str
) -> list[Path]:
    """Materialize the address-book auto-seed file the newborn reads at boot,
    IFF the profile declares any `address_book_entries` (Architect ruling R1).

    Finding F9 (2026-07-11): `macos-free-solet` binds `embedding_service`
    to `openai_embeddings_plugin`, whose `prepare_for_readiness` fail-fasts
    unless the `openai_embeddings` address-book entry exists. Genesis
    materializes the plugin CONFIG file but the entry is Postgres DATA no
    genesis step created — so the newborn crash-looped. The address-book
    plugin's own sanctioned seed path reads
    `<app_home>/config/plugins/default_address_book_plugin/entries.json`
    (`app_home` = `<target>/profile`) and self-registers idempotently
    (`seed_loader.auto_seed_entries_from_file`). This writes that file from
    the profile template's declarative `address_book_entries` — the entries
    are profile-specified config (non-secret: a local endpoint + model name,
    never an api_key or vault reference), the same category as
    `service_bindings` / `starting_actions`.

    Writes nothing when the profile declares no entries (R1) — seed_loader
    debug-skips an absent file, the proven no-op path.
    """
    entries = profile.get("address_book_entries") or []
    if not isinstance(entries, list):
        raise ConfigMaterializeError("profile 'address_book_entries' must be a list")
    if not entries:
        return []
    validated = [_validate_address_book_entry(entry, index) for index, entry in enumerate(entries)]
    # Deterministic ${SOLET_NAME} substitution at write time (finding F11,
    # Architect 2026-07-11): template placeholders (e.g. pgvector_service_db's
    # database + db_schema) become the newborn's actual name here. Uses the
    # EXPLICIT `name` argument (the newborn), NEVER os.environ["SOLET_NAME"]
    # -- in verb-mode this runs in the PARENT's process, whose env name is the
    # parent's, not the newborn's. A single serialize/replace/parse keeps it
    # value-agnostic (every string field is covered, not just known keys).
    substituted = json.loads(json.dumps(validated).replace(_SOLET_NAME_PLACEHOLDER, name))
    path = target / "profile" / "config" / "plugins" / _ADDRESS_BOOK_PLUGIN / "entries.json"
    write_json(path, {"entries": substituted})
    return [path]


def materialize_profile(
    *, target: Path, kb_root: Path, profile_name: str, name: str,
) -> dict[str, list[Path]]:
    """Run every writer in the canonical order; return a paths-written map.

    `name` is the newborn solet's name (from `GenesisContext.name`),
    threaded to `write_address_book_entries` for the `${SOLET_NAME}`
    substitution -- the explicit newborn name, never the ambient env var
    (verb-mode runs in the parent's process).
    """
    profile = load_profile(kb_root, profile_name)
    written: dict[str, list[Path]] = {}
    # Scaffold FIRST: the runtime-required empty dirs a seed clone arrives
    # without (git tracks no empty dirs) -- cold-run D9.
    written["scaffold_dirs"] = create_runtime_scaffold_dirs(target)
    written["manifest"] = [write_runtime_manifest(target, profile)]
    written["service_bindings"] = [write_service_bindings(target, profile)]
    written["starting_actions"] = [write_starting_actions(target, profile)]
    # Order matters: copy the baseline configs FIRST, then apply overrides on top
    # (the postgres `user` override merges onto the copied baseline).
    written["plugin_configs"] = copy_baseline_plugin_configs(target, kb_root, profile)
    written["overrides"] = apply_profile_overrides(target, profile, name)
    # After overrides so nothing can clobber the injected per-installation value.
    written["knowledge_base_root"] = inject_knowledge_base_root(target, profile)
    written["address_book_entries"] = write_address_book_entries(target, profile, name)
    # Boot-required identity strings (write-if-absent) -- cold-run D7.
    written["identity"] = write_identity_config(target, name)
    # Boot-required global system prompt (write-if-absent) -- real inference /
    # memory formatter path hard-opens profile/config/prompts/system.json.
    written["global_system_prompt"] = write_global_system_prompt_config(target, name)
    # Fail loud if the postgres connect-user did not resolve to this newborn's
    # own role (per-solet isolation, 2026-07-12), or if the knowledge
    # plugin's per-installation root is missing/clobbered (cold-run D1).
    _verify_postgres_user_isolated(target, profile, name)
    _verify_knowledge_base_root_configured(target, profile)
    return written
