"""Seed-side pre-runtime migrations and the post-runtime plugin cache refresh (design sections 5.1, 7.6).

``existing::migration.solet_rename`` is the 2026-08-13 rename boundary
(LaunchAgent plists and labels, the ``.zshrc`` wake CLI, the root manifest
key, and the ``HOMUNCULUS_*`` env keys inside ``~/.claude.json``'s MCP
servers); its "refuses while Claude Code is running" rule is a ``BLOCKED``
probe with the stable ``coding_agent_running`` reason.
``existing::migration.export_root_containment`` propagates an
already-chosen export root to connectors that lack it and is probe-only when
no root was ever chosen.  ``existing::runtime.plugin_cache_refresh`` compares
the installed Claude plugin cache copy's hooks against the shipped hooks and
reinstalls only on a non-empty diff.  The helpers at the bottom are shared
with ``existing_install_operations``.
"""

from __future__ import annotations

import json
import os
import plistlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from .coordination_hook_installation import hook_root_matches_expected
from .export_root_validation import BUSINESS_CONNECTOR_PLUGINS, CONFIG_KEY_EXPORT_ALLOWED_ROOTS, configure_export_root
from .setup_adapter_contract import AdapterRequest, JsonObject, JsonValue, evidence, planned_action, result
from .setup_adapter_runtime import Runtime, resolve_executable
from .setup_plugin_operations import plugin_list_rows, plugin_list_vector, plugin_row_visible

STRUCTURED_OUTPUT_LIMIT = 64 * 1024
_LEGACY_ENV = re.compile(r"^HOMUNCULUS_")
_LEGACY_LABEL = re.compile(r"^local\.homunculus\.")
_LEGACY_ARG = "--homunculus"
_LEGACY_WAKE = 'AGENT_WAKE_CLI="homunculus"'
_NEW_WAKE = 'AGENT_WAKE_CLI="solet-bridge"'
_SHIPPED_HOOKS = Path("plugins/github_midwife_plugin/claude_plugin/coordination-hooks/hooks")


# --- existing::migration.solet_rename --------------------------------------------


@dataclass(frozen=True, slots=True)
class PlistRename:
    old_path: Path
    new_path: Path
    old_label: str
    new_label: str
    loaded: bool


@dataclass(frozen=True, slots=True)
class RenamePlan:
    plists: tuple[PlistRename, ...]
    zshrc_count: int
    manifest_stale: bool
    claude_json_servers: tuple[str, ...]

    @property
    def empty(self) -> bool:
        return not self.plists and not self.zshrc_count and not self.manifest_stale and not self.claude_json_servers


def migration_solet_rename(request: AdapterRequest, runtime: Runtime) -> JsonObject:
    plan = _rename_plan(request, runtime)
    items = [facts_evidence("migration.solet_rename", {"plists": len(plan.plists), "zshrc": plan.zshrc_count, "root_manifest": plan.manifest_stale, "claude_json": len(plan.claude_json_servers)}, verified=plan.empty)]
    if request.phase == "probe":
        return _pending_or_verified(request, items, plan.empty, "Apply the rename migration.", _rename_actions(request, runtime, plan))
    if plan.empty:
        return result(request, status="applied", retry_safe=True, evidence_items=items)
    if plan.claude_json_servers and _claude_code_running(runtime) is not False:
        return blocked(request, "coding_agent_running", "A Claude Code process is running (or its state could not be determined); it rewrites ~/.claude.json on exit. Quit Claude Code, then re-run --yes.")
    _apply_plist_renames(runtime, plan)
    _apply_text_renames(request, runtime, plan)
    if plan.claude_json_servers:
        _apply_claude_json(runtime, plan.claude_json_servers)
    return result(request, status="applied", retry_safe=True, evidence_items=items)


def _pending_or_verified(request: AdapterRequest, items: list[JsonObject], verified: bool, pending_repair: str, actions: list[JsonObject]) -> JsonObject:
    if verified:
        return result(request, status="verified", evidence_items=items)
    if request.action_arrays_must_be_empty:
        return result(request, status="pending", evidence_items=items, repair=pending_repair)
    return result(request, status="pending", actions=actions, evidence_items=items, repair="Approve the exact rewrites shown.")


def _rename_actions(request: AdapterRequest, runtime: Runtime, plan: RenamePlan) -> list[JsonObject]:
    actions = [planned_action(action_id=f"rename.plist.{index}", title=f"Rewrite LaunchAgent {item.new_label}", mutation_kind="file_write", target=str(item.old_path), evidence_ref="migration.solet_rename") for index, item in enumerate(plan.plists)]
    if plan.zshrc_count:
        actions.append(planned_action(action_id="rename.zshrc", title="Rewrite AGENT_WAKE_CLI in ~/.zshrc", mutation_kind="file_write", target=str(runtime.home / ".zshrc"), evidence_ref="migration.solet_rename"))
    if plan.manifest_stale:
        actions.append(planned_action(action_id="rename.root_manifest", title="Rename the root manifest key", mutation_kind="file_write", target=str(request.target / "root_manifest.yaml"), evidence_ref="migration.solet_rename"))
    if plan.claude_json_servers:
        actions.append(planned_action(action_id="rename.claude_json", title="Rename HOMUNCULUS_* MCP env keys", mutation_kind="file_write", target=str(runtime.home / ".claude.json"), evidence_ref="migration.solet_rename"))
    return actions


def _rename_plan(request: AdapterRequest, runtime: Runtime) -> RenamePlan:
    zshrc_count = (read_text(runtime.home / ".zshrc") or "").count(_LEGACY_WAKE)
    manifest_stale = re.search(r"^homunculus_name:", read_text(request.target / "root_manifest.yaml") or "", re.M) is not None
    return RenamePlan(_plist_renames(request, runtime), zshrc_count, manifest_stale, _claude_json_servers(runtime.home))


def _plist_renames(request: AdapterRequest, runtime: Runtime) -> tuple[PlistRename, ...]:
    agents = runtime.home / "Library" / "LaunchAgents"
    if not agents.is_dir():
        return ()
    plans = (_plan_plist(path, request, runtime) for path in sorted(agents.glob("*.plist")))
    return tuple(plan for plan in plans if plan is not None)


def _claude_json_servers(home: Path) -> tuple[str, ...]:
    claude_json = read_json(home / ".claude.json")
    mcp = claude_json.get("mcpServers") if claude_json else None
    if not isinstance(mcp, dict):
        return ()
    servers: list[str] = []
    for name, config in cast(JsonObject, mcp).items():
        env = cast(JsonObject, config).get("env") if isinstance(config, dict) else None
        if isinstance(env, dict) and any(_LEGACY_ENV.match(str(key)) for key in cast(JsonObject, env)):
            servers.append(name)
    return tuple(servers)


def _plan_plist(path: Path, request: AdapterRequest, runtime: Runtime) -> PlistRename | None:
    payload = _load_plist(path)
    if payload is None:
        return None
    label = str(payload.get("Label", ""))
    arguments = _arguments(payload)
    if not _owned(label, arguments, request.target) or not _needs_rename(label, arguments, payload):
        return None
    new_label = _LEGACY_LABEL.sub("local.solet.", label)
    new_path = path.with_name(path.name.replace("local.homunculus.", "local.solet."))
    loaded = runtime.run(("/bin/launchctl", "print", f"gui/{os.getuid()}/{label}"), timeout_seconds=10).ok
    return PlistRename(path, new_path, label, new_label, loaded)


def _load_plist(path: Path) -> dict[str, object] | None:
    try:
        payload = plistlib.loads(path.read_bytes())
    except (OSError, plistlib.InvalidFileException, ValueError):
        return None
    return cast(dict[str, object], payload) if isinstance(payload, dict) else None


def _arguments(payload: dict[str, object]) -> list[str]:
    raw = payload.get("ProgramArguments")
    return [str(item) for item in cast(list[object], raw)] if isinstance(raw, list) else []


def _owned(label: str, arguments: list[str], target: Path) -> bool:
    return label.startswith(("local.homunculus.", "local.solet.")) or any(str(target) in argument for argument in arguments)


def _needs_rename(label: str, arguments: list[str], payload: dict[str, object]) -> bool:
    env = payload.get("EnvironmentVariables")
    env_keys = [str(key) for key in cast(dict[str, object], env)] if isinstance(env, dict) else []
    return any(_LEGACY_ENV.match(key) for key in env_keys) or _LEGACY_ARG in arguments or _LEGACY_LABEL.match(label) is not None


def _apply_plist_renames(runtime: Runtime, plan: RenamePlan) -> None:
    for item in plan.plists:
        payload = _renamed_payload(cast(dict[str, object], plistlib.loads(item.old_path.read_bytes())), item.new_label)
        if item.loaded:
            runtime.run(("/bin/launchctl", "bootout", f"gui/{os.getuid()}/{item.old_label}"), timeout_seconds=30)
        runtime.atomic_write(item.new_path, plistlib.dumps(payload).decode("utf-8"), mode=file_mode(item.old_path, 0o644))
        if item.new_path != item.old_path:
            item.old_path.unlink()
        if item.loaded:
            runtime.run(("/bin/launchctl", "bootstrap", f"gui/{os.getuid()}", str(item.new_path)), timeout_seconds=30)


def _renamed_payload(payload: dict[str, object], new_label: str) -> dict[str, object]:
    payload["Label"] = new_label
    env = payload.get("EnvironmentVariables")
    if isinstance(env, dict):
        typed = cast(dict[str, object], env)
        for key in [key for key in typed if _LEGACY_ENV.match(str(key))]:
            typed[_LEGACY_ENV.sub("SOLET_", str(key))] = typed.pop(key)
    arguments = payload.get("ProgramArguments")
    if isinstance(arguments, list):
        payload["ProgramArguments"] = ["--solet" if str(entry) == _LEGACY_ARG else entry for entry in cast(list[object], arguments)]
    return payload


def _apply_text_renames(request: AdapterRequest, runtime: Runtime, plan: RenamePlan) -> None:
    if plan.zshrc_count:
        zshrc = runtime.home / ".zshrc"
        runtime.atomic_write(zshrc, (read_text(zshrc) or "").replace(_LEGACY_WAKE, _NEW_WAKE), mode=file_mode(zshrc, 0o644))
    if plan.manifest_stale:
        manifest = request.target / "root_manifest.yaml"
        runtime.atomic_write(manifest, re.sub(r"^homunculus_name:", "solet_name:", read_text(manifest) or "", count=1, flags=re.M), mode=file_mode(manifest, 0o644))


def _apply_claude_json(runtime: Runtime, servers: tuple[str, ...]) -> None:
    path = runtime.home / ".claude.json"
    data = read_json(path) or {}
    mcp = cast(JsonObject, data["mcpServers"])
    for name in servers:
        env = cast(JsonObject, cast(JsonObject, mcp[name])["env"])
        for key in [key for key in env if _LEGACY_ENV.match(key)]:
            env[_LEGACY_ENV.sub("SOLET_", key)] = env.pop(key)
    runtime.atomic_write(path, json.dumps(data, indent=2, ensure_ascii=False), mode=file_mode(path, 0o600))


def _claude_code_running(runtime: Runtime) -> bool | None:
    outcome = runtime.run(("/usr/bin/pgrep", "-x", "claude"), timeout_seconds=10)
    if outcome.executable_missing or outcome.timed_out:
        return None
    if outcome.returncode == 0:
        return True
    return False if outcome.returncode == 1 else None


# --- existing::migration.export_root_containment ----------------------------------------


@dataclass(frozen=True, slots=True)
class ExportRootFacts:
    config_dir: Path
    installed: tuple[str, ...]
    distinct: tuple[str, ...]
    missing: tuple[str, ...]

    @property
    def chosen_root(self) -> str | None:
        return next(iter(self.distinct)) if len(self.distinct) == 1 else None

    def facts(self) -> dict[str, str | int | bool]:
        configured = self.chosen_root or ("none" if not self.distinct else "ambiguous")
        return {"configured_root": configured, "installed": len(self.installed), "missing": len(self.missing)}


def migration_export_root_containment(request: AdapterRequest, runtime: Runtime) -> JsonObject:
    """Runbook step 4a: propagate an already-chosen export root to connectors that lack it; probe-only when unset."""
    del runtime
    facts = _export_root_facts(request)
    if not facts.distinct:
        observed = facts.facts()
        observed["operator_action_required"] = "configure the export root via the seed update runbook step 4a"
        items = [facts_evidence("migration.export_root_containment", observed, verified=True)]
        return result(request, status="applied" if request.phase == "apply" else "verified", evidence_items=items)
    if len(facts.distinct) > 1:
        return blocked(request, "export_root_ambiguous", "Installed connectors disagree about the export root; reconcile profile/config/plugins by hand.")
    items = [facts_evidence("migration.export_root_containment", facts.facts(), verified=not facts.missing)]
    if request.phase == "probe":
        actions = [planned_action(action_id=f"export_root.{plugin}", title=f"Propagate the export root to {plugin}", mutation_kind="file_write", target=str(facts.config_dir / f"{plugin}.json"), evidence_ref="migration.export_root_containment") for plugin in facts.missing]
        return _pending_or_verified(request, items, not facts.missing, "Apply the export-root propagation.", actions)
    if facts.missing and facts.chosen_root is not None:
        configure_export_root(request.target, str(request.target / "profile"), facts.chosen_root, connector_plugins=facts.missing)
    return result(request, status="applied", retry_safe=True, evidence_items=items)


def _export_root_facts(request: AdapterRequest) -> ExportRootFacts:
    config_dir = request.target / "profile" / "config" / "plugins"
    installed = tuple(plugin for plugin in BUSINESS_CONNECTOR_PLUGINS if (request.target / "plugins" / plugin).is_dir())
    roots = {plugin: _connector_roots(config_dir / f"{plugin}.json") for plugin in installed}
    distinct = tuple(sorted({root for values in roots.values() for root in values}))
    missing = tuple(plugin for plugin, values in roots.items() if not values)
    return ExportRootFacts(config_dir, installed, distinct, missing)


def _connector_roots(config_path: Path) -> list[str]:
    config = read_json(config_path) or {}
    values = config.get(CONFIG_KEY_EXPORT_ALLOWED_ROOTS, [])
    return [str(item) for item in cast(list[JsonValue], values)] if isinstance(values, list) else []


# --- existing::runtime.plugin_cache_refresh -------------------------------------------------


@dataclass(frozen=True, slots=True)
class CacheFacts:
    executable: str
    selector: str
    visible: bool
    cache_root: Path | None
    current: bool

    def evidence(self) -> JsonObject:
        return facts_evidence("cache.coordination_hooks", {"selector": self.selector, "visible": self.visible, "cache_root": str(self.cache_root) if self.cache_root else "none", "diff_empty": self.current}, verified=self.current)


def plugin_cache_refresh(request: AdapterRequest, runtime: Runtime) -> JsonObject:
    executable = resolve_executable(runtime, "claude")
    if executable is None:
        return blocked(request, "claude_cli_missing", "Install the Claude CLI, then re-run.")
    facts = _cache_facts(request, runtime, executable)
    items = [facts.evidence()]
    if request.phase == "probe":
        if facts.current:
            return result(request, status="verified", evidence_items=items)
        if not facts.visible:
            return blocked(request, "plugin_not_visible", f"{facts.selector} is not installed; run the coding-agent hydration step.")
        actions = [planned_action(action_id="cache.reinstall", title=f"Uninstall and reinstall {facts.selector} so the cache copy matches the shipped hooks", mutation_kind="plugin_cache_write", target=str(facts.cache_root), evidence_ref="cache.coordination_hooks")]
        return _pending_or_verified(request, items, False, "Refresh the plugin cache.", actions)
    if facts.current:
        return result(request, status="applied", retry_safe=True, evidence_items=items)
    return _cache_reinstall(request, runtime, facts, items)


def _cache_facts(request: AdapterRequest, runtime: Runtime, executable: str) -> CacheFacts:
    marketplace = request.name.replace("_", "-")
    selector = f"coordination-hooks@{marketplace}"
    listed = runtime.run(plugin_list_vector("claude", marketplace, executable), timeout_seconds=10, output_limit=STRUCTURED_OUTPUT_LIMIT)
    rows = plugin_list_rows("claude", listed)
    visible = rows is not None and any(plugin_row_visible("claude", row, selector) for row in rows)
    cache_root = _cache_root(runtime, selector)
    current = visible and cache_root is not None and hook_root_matches_expected(cache_root / "hooks", request.target / _SHIPPED_HOOKS)
    return CacheFacts(executable, selector, visible, cache_root, current)


def _cache_reinstall(request: AdapterRequest, runtime: Runtime, facts: CacheFacts, items: list[JsonObject]) -> JsonObject:
    removed = runtime.run((facts.executable, "plugin", "uninstall", facts.selector), timeout_seconds=request.timeout_seconds, cwd=request.target)
    if not removed.ok:
        return blocked(request, "claude_plugin_uninstall_failed", "The Claude CLI refused the uninstall; inspect its output.")
    installed = runtime.run((facts.executable, "plugin", "install", facts.selector), timeout_seconds=request.timeout_seconds, cwd=request.target)
    if not installed.ok:
        return blocked(request, "claude_plugin_install_failed", "The Claude CLI refused the reinstall; inspect its output.")
    return result(request, status="applied", retry_safe=True, evidence_items=items)


def _cache_root(runtime: Runtime, selector: str) -> Path | None:
    registry = read_json(runtime.home / ".claude/plugins/installed_plugins.json") or {}
    plugins = registry.get("plugins")
    entries = cast(JsonObject, plugins).get(selector) if isinstance(plugins, dict) else None
    if not isinstance(entries, list):
        return None
    roots = [Path(str(cast(JsonObject, row)["installPath"])) for row in cast(list[JsonValue], entries) if isinstance(row, dict) and isinstance(cast(JsonObject, row).get("installPath"), str)]
    roots = [root for root in roots if root.is_dir()]
    return roots[0] if len(roots) == 1 else None


# --- shared helpers -------------------------------------------------------------------------


def facts_evidence(evidence_id: str, facts: dict[str, str | int | bool], *, verified: bool) -> JsonObject:
    return evidence(
        evidence_id=evidence_id,
        kind="existing_install",
        status="verified" if verified else "pending",
        summary=f"{evidence_id} observed",
        observed=[f"{key}={_text(value)}" for key, value in sorted(facts.items())],
        expected="verified",
        source=evidence_id,
    )


def _text(value: str | int | bool) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def blocked(request: AdapterRequest, error_kind: str, repair: str) -> JsonObject:
    return result(request, status="blocked", error_kind=error_kind, retry_safe=True, exit_code=None, repair=repair)


def read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None


def read_json(path: Path) -> JsonObject | None:
    text = read_text(path)
    if text is None:
        return None
    raw: object = json.loads(text)
    return cast(JsonObject, raw) if isinstance(raw, dict) else None


def file_mode(path: Path, default: int) -> int:
    try:
        return path.stat().st_mode & 0o777
    except FileNotFoundError:
        return default


__all__ = [
    "STRUCTURED_OUTPUT_LIMIT",
    "blocked",
    "facts_evidence",
    "file_mode",
    "migration_export_root_containment",
    "migration_solet_rename",
    "plugin_cache_refresh",
    "read_json",
    "read_text",
]
