"""Seed-side pre-runtime migrations and the post-runtime plugin cache refresh (design sections 5.1, 7.6).

``existing::migration.solet_rename`` is the 2026-08-13 rename boundary
(LaunchAgent plists and labels, the ``.zshrc`` wake CLI, the root manifest
key, and the ``HOMUNCULUS_*`` env keys inside ``~/.claude.json``'s MCP
servers); its "refuses while Claude Code is running" rule is a ``BLOCKED``
probe with the stable ``coding_agent_running`` reason.
``existing::migration.export_root_containment`` propagates an
already-chosen export root to connectors that lack it and is probe-only when
no root was ever chosen.  ``existing::runtime.plugin_cache_refresh`` compares
the installed Claude plugin cache copy's hooks against the shipped hooks; the
refresh is settled only when that diff is empty, the tracked hook manifest is
pinned to ``<clone>/.venv/bin/python3``, and the coordination receipt reads
back against the cache and the checkout (create's own probe predicates,
iss_fa27466f).  Otherwise it pins a still-bare manifest, reinstalls when the
cache is or will be stale, and republishes the receipt.  When the plugin is
not visible at all it registers this clone's own marketplace first
(``coordination_marketplace``, iss_c9a7b626).  The helpers at the bottom are shared with
``existing_install_operations``.
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
from .coordination_marketplace import HOOK_MANIFEST, pin_action, pin_needed, receipt_action
from .coordination_marketplace import register as register_marketplace
from .coordination_marketplace import registration as marketplace_registration
from .export_root_validation import BUSINESS_CONNECTOR_PLUGINS, CONFIG_KEY_EXPORT_ALLOWED_ROOTS, configure_export_root
from .setup_adapter_contract import REPAIR_LIMIT, AdapterRequest, JsonObject, JsonValue, evidence, planned_action, result
from .setup_adapter_runtime import CommandOutcome, Runtime, describe_outcome, executable_fallback_directories, resolve_executable
from .setup_plugin_operations import _claude_receipt_is_current, _manifest_has_absolute_python, _patch_hook_manifest, _publish_claude_receipt, plugin_list_rows, plugin_row_visible, run_plugin_list
from .target_reconciliation import PLIST_PARSE_ERRORS

STRUCTURED_OUTPUT_LIMIT = 64 * 1024
_LEGACY_ENV = re.compile(r"^HOMUNCULUS_")
_LEGACY_LABEL = re.compile(r"^local\.homunculus\.")
_LEGACY_ARG = "--homunculus"
_LEGACY_WAKE = 'AGENT_WAKE_CLI="homunculus"'
_NEW_WAKE = 'AGENT_WAKE_CLI="solet-bridge"'
_SHIPPED_HOOKS = Path("plugins/github_midwife_plugin/claude_plugin/coordination-hooks/hooks")
#: iss_646b54b6: the update never installs the Claude CLI (install consent, rul_e45fb5b3); it names the command.
REPAIR_CLAUDE_CLI = (
    "The Claude Code CLI (`claude`) is not on PATH or in {directories}, and the update never installs it. "
    "Install it yourself: `brew install --cask claude-code`. Confirm `command -v claude` prints a path in the shell you run "
    "solet-manager from, then run `solet-manager update {name} --dry-run` again."
)


# --- existing::migration.solet_rename --------------------------------------------


class PlistChangedError(RuntimeError):
    """A plist the probe parsed can no longer be parsed when the rename is applied."""


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
    try:
        return _solet_rename(request, runtime)
    except PlistChangedError as exc:
        return blocked(request, "probe_drift", f"{exc}, so nothing was rewritten. Preview again.")


def _solet_rename(request: AdapterRequest, runtime: Runtime) -> JsonObject:
    plan = _rename_plan(request, runtime)
    items = [facts_evidence("migration.solet_rename", {"plists": len(plan.plists), "zshrc": plan.zshrc_count, "root_manifest": plan.manifest_stale, "claude_json": len(plan.claude_json_servers)}, verified=plan.empty)]
    if request.phase == "probe":
        return _pending_or_verified(request, items, plan.empty, "Apply the rename migration.", _rename_actions(request, runtime, plan))
    if plan.empty:
        return result(request, status="applied", retry_safe=True, evidence_items=items)
    if plan.claude_json_servers and (running := _claude_code_running(runtime)) is not False:
        return blocked(request, "coding_agent_running", _coding_agent_repair(running))
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
    except PLIST_PARSE_ERRORS:
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
    payloads = [_renamed_payload(_reloaded_plist(item), item.new_label) for item in plan.plists]
    for item, payload in zip(plan.plists, payloads, strict=True):
        if item.loaded:
            runtime.run(("/bin/launchctl", "bootout", f"gui/{os.getuid()}/{item.old_label}"), timeout_seconds=30)
        runtime.atomic_write(item.new_path, plistlib.dumps(payload).decode("utf-8"), mode=file_mode(item.old_path, 0o644))
        if item.new_path != item.old_path:
            item.old_path.unlink()
        if item.loaded:
            runtime.run(("/bin/launchctl", "bootstrap", f"gui/{os.getuid()}", str(item.new_path)), timeout_seconds=30)


def _reloaded_plist(item: PlistRename) -> dict[str, object]:
    """The plist as it reads now; every plist is re-read before the first is rewritten, so one that changed since the probe stops the rename with nothing written."""
    payload = _load_plist(item.old_path)
    if payload is None:
        raise PlistChangedError(f"the LaunchAgent plist {item.old_path} no longer parses as it did at the probe")
    return payload


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


def _claude_code_running(runtime: Runtime) -> bool | str:
    """``True``/``False`` when ``pgrep`` answered; otherwise why it could not (iss_67d2597e)."""
    outcome = runtime.run(("/usr/bin/pgrep", "-x", "claude"), timeout_seconds=10)
    if outcome.executable_missing:
        return "pgrep missing"
    if outcome.timed_out:
        return "pgrep timed out"
    if outcome.returncode in (0, 1):
        return outcome.returncode == 0
    return f"pgrep exited {outcome.returncode}"


def _coding_agent_repair(running: bool | str) -> str:
    if isinstance(running, str):
        return f"Claude Code's state could not be determined ({running}); it rewrites ~/.claude.json on exit if it is running. Check for a running Claude Code yourself and quit it, then re-run --yes."
    return "A Claude Code process is running; it rewrites ~/.claude.json on exit. Quit Claude Code, then re-run --yes."


# --- existing::migration.export_root_containment ----------------------------------------


@dataclass(frozen=True, slots=True)
class ExportRootFacts:
    config_dir: Path
    installed: tuple[str, ...]
    distinct: tuple[str, ...]
    missing: tuple[str, ...]
    #: Each installed connector with the roots it holds, in ``installed`` order (iss_67d2597e).
    roots: tuple[tuple[str, tuple[str, ...]], ...] = ()

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
        return blocked(request, "export_root_ambiguous", _export_root_repair(request, facts))
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
    return ExportRootFacts(config_dir, installed, distinct, missing, tuple((plugin, tuple(values)) for plugin, values in roots.items()))


def _export_root_repair(request: AdapterRequest, facts: ExportRootFacts) -> str:
    """The refusal names every connector and the roots it holds; when that does not fit ``REPAIR_LIMIT`` each entry is ellipsized, never dropped."""
    head = "Installed connectors disagree about the export root: "
    tail = f". Edit export_allowed_roots in this clone's profile/config/plugins/<plugin>.json so every connector holds one root, then run solet-manager update {request.name} --dry-run again."
    budget = REPAIR_LIMIT - len(head) - len(tail)
    entries = [f"{plugin}={', '.join(values) or '(none)'}" for plugin, values in facts.roots]
    if len("; ".join(entries)) > budget:
        share = max(budget // len(entries) - 2, 2)
        entries = [entry if len(entry) <= share else f"{entry[: share - 1]}…" for entry in entries]
    return f"{head}{'; '.join(entries)}{tail}"


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
    #: iss_c9a7b626 N4: why ``claude plugin list --json`` gave no rows (``None`` when it did); never read as "not visible".
    list_failure: str | None = None
    #: iss_fa27466f: every Python hook in the tracked manifest runs ``<clone>/.venv/bin/python3`` (create's probe predicate).
    pinned: bool = False
    #: iss_fa27466f: the coordination receipt reads back against this cache and the checkout (create's probe predicate).
    received: bool = False

    @property
    def settled(self) -> bool:
        return self.current and self.pinned and self.received

    def evidence(self) -> JsonObject:
        return facts_evidence("cache.coordination_hooks", {"selector": self.selector, "visible": self.visible, "cache_root": str(self.cache_root) if self.cache_root else "none", "diff_empty": self.current, "hooks_pinned": self.pinned, "receipt_current": self.received}, verified=self.settled)


def plugin_cache_refresh(request: AdapterRequest, runtime: Runtime) -> JsonObject:
    executable = resolve_executable(runtime, "claude")
    if executable is None:
        return blocked(request, "claude_cli_missing", REPAIR_CLAUDE_CLI.format(name=request.name, directories=", ".join(executable_fallback_directories(runtime))))
    facts = _cache_facts(request, runtime, executable)
    if facts.list_failure is not None:
        return blocked(request, "claude_plugin_list_failed", f"`claude plugin list --json` did not answer ({facts.list_failure}); the update never plans a registration off a failed read. Run it yourself, fix what it reports, then run `solet-manager update {request.name} --dry-run` again.")
    items = [facts.evidence()]
    if facts.settled:
        if request.phase == "probe":
            return result(request, status="verified", evidence_items=items)
        return result(request, status="applied", retry_safe=True, evidence_items=items)
    if not facts.visible:
        return _register_marketplace(request, runtime, facts, items)
    return _settle(request, runtime, facts, items)


def _settle(request: AdapterRequest, runtime: Runtime, facts: CacheFacts, items: list[JsonObject]) -> JsonObject:
    """iss_fa27466f: visible but unsettled; pin a bare manifest, reinstall a stale (or about to be stale) cache, republish the receipt."""
    pin = pin_needed(request)
    refusal = _settle_refusal(request, facts, pin)
    if refusal is not None:
        return refusal
    pinning = pin is True
    # Pinning rewrites the tracked manifest, so the cache copy of it goes stale and is reinstalled too.
    reinstall = pinning or not facts.current
    if request.phase == "probe":
        return _pending_or_verified(request, items, False, "Pin the hook interpreter, refresh the plugin cache and publish the coordination receipt.", _settle_actions(request, facts, pinning, reinstall))
    failed = _apply_settle(request, runtime, facts, pinning, reinstall)
    return failed if failed is not None else result(request, status="applied", retry_safe=True, evidence_items=items)


def _settle_refusal(request: AdapterRequest, facts: CacheFacts, pin: bool | tuple[str, str]) -> JsonObject | None:
    """An unreadable manifest, or one with no bare hook that binds another interpreter; never rewritten."""
    if isinstance(pin, tuple):
        return blocked(request, pin[0], pin[1])
    if pin or facts.pinned:
        return None
    manifest = request.target / HOOK_MANIFEST
    return blocked(request, "hook_interpreter_foreign", f"{manifest} binds its Python hooks to an interpreter other than {request.target / '.venv/bin/python3'}; the update never rewrites another interpreter's pin. Inspect `grep -n '\"command\"' {manifest}`, then run `solet-manager update {request.name} --dry-run` again.")


def _settle_actions(request: AdapterRequest, facts: CacheFacts, pinning: bool, reinstall: bool) -> list[JsonObject]:
    actions = [pin_action(request.target)] if pinning else []
    if reinstall:
        actions.append(planned_action(action_id="cache.reinstall", title=f"Uninstall and reinstall {facts.selector} so the cache copy matches the shipped hooks", mutation_kind="plugin_cache_write", target=str(facts.cache_root), evidence_ref="cache.coordination_hooks"))
    actions.append(receipt_action(request.target))
    return actions


def _apply_settle(request: AdapterRequest, runtime: Runtime, facts: CacheFacts, pinning: bool, reinstall: bool) -> JsonObject | None:
    """Create's order: pin, reinstall, publish; the first refusal, or ``None`` once the receipt is published."""
    if pinning and (pin_error := _patch_hook_manifest(request.target / HOOK_MANIFEST, request.target, runtime)) is not None:
        return blocked(request, "hook_manifest_invalid", pin_error)
    refused = _cache_reinstall(request, runtime, facts) if reinstall else None
    if refused is not None:
        return refused
    receipt_error = _publish_claude_receipt(request, runtime, facts.executable, request.name.replace("_", "-"), facts.selector)
    if receipt_error is not None:
        return blocked(request, "coordination_receipt_invalid", f"The coordination receipt was not published ({receipt_error}); inspect `claude plugin list`, then run `solet-manager update {request.name} --dry-run` again.")
    return None


def _register_marketplace(request: AdapterRequest, runtime: Runtime, facts: CacheFacts, items: list[JsonObject]) -> JsonObject:
    """iss_c9a7b626: the plugin is not visible; register this clone's own marketplace and install, or refuse exactly."""
    plan = marketplace_registration(request, runtime, facts.selector)
    if plan.error_kind is not None:
        return blocked(request, plan.error_kind, cast(str, plan.repair))
    if request.phase == "probe":
        return _pending_or_verified(request, items, False, f"Register this clone's marketplace and install {facts.selector}.", plan.actions(request.target))
    failed = register_marketplace(request, runtime, facts.executable, plan)
    if failed is not None:
        return blocked(request, failed[0], f"A registration step failed ({failed[0]}: {failed[1]}); inspect `claude plugin marketplace list` and `claude plugin list`, then run `solet-manager update {request.name} --dry-run` again.")
    return result(request, status="applied", retry_safe=True, evidence_items=items)


def _cache_facts(request: AdapterRequest, runtime: Runtime, executable: str) -> CacheFacts:
    marketplace = request.name.replace("_", "-")
    selector = f"coordination-hooks@{marketplace}"
    _vector, listed = run_plugin_list(runtime, "claude", marketplace, executable)
    rows = plugin_list_rows("claude", listed)
    if rows is None:
        return CacheFacts(executable, selector, False, None, False, list_failure=_list_failure(listed))
    visible = any(plugin_row_visible("claude", row, selector) for row in rows)
    cache_root = _cache_root(runtime, selector)
    current = visible and cache_root is not None and hook_root_matches_expected(cache_root / "hooks", request.target / _SHIPPED_HOOKS)
    pinned = _manifest_has_absolute_python(request.target / HOOK_MANIFEST, request.target)
    received = current and _claude_receipt_is_current(request, runtime, executable, marketplace, selector)
    return CacheFacts(executable, selector, visible, cache_root, current, pinned=pinned, received=received)


def _list_failure(listed: CommandOutcome) -> str:
    """Exit code, timeout/truncation and stderr of a ``plugin list`` that yielded no parseable rows."""
    state = describe_outcome(listed)
    if listed.ok:
        suffix = ", stdout truncated" if listed.stdout_truncated else ", stdout is not a JSON list"
        head, separator, stderr = state.partition("; stderr: ")
        state = f"{head}{suffix}{separator}{stderr}"
    return state


def _cache_reinstall(request: AdapterRequest, runtime: Runtime, facts: CacheFacts) -> JsonObject | None:
    """Uninstall and reinstall the plugin; the refusal, or ``None`` once the CLI reinstalled it."""
    removed = runtime.run((facts.executable, "plugin", "uninstall", facts.selector), timeout_seconds=request.timeout_seconds, cwd=request.target)
    if not removed.ok:
        return blocked(request, "claude_plugin_uninstall_failed", f"The Claude CLI refused the uninstall ({describe_outcome(removed)}); inspect `claude plugin list`, then run `solet-manager update {request.name} --dry-run` again.")
    installed = runtime.run((facts.executable, "plugin", "install", facts.selector), timeout_seconds=request.timeout_seconds, cwd=request.target)
    if not installed.ok:
        return blocked(request, "claude_plugin_install_failed", f"The Claude CLI refused the reinstall ({describe_outcome(installed)}); inspect `claude plugin list`, then run `solet-manager update {request.name} --dry-run` again.")
    return None


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


class NotTextError(ValueError):
    """A file this adapter reads as UTF-8 text holds other bytes, as a binary plist does; it is never read as absent or rewritten."""

    def __init__(self, path: Path) -> None:
        super().__init__(f"{path} is not UTF-8 text")
        self.path = path


def read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except UnicodeDecodeError as exc:
        raise NotTextError(path) from exc


def not_text_blocked(request: AdapterRequest, exc: NotTextError) -> JsonObject:
    """The one blocked result for a managed file that is not text: its origin cannot be told, so it is left alone and the repair says how to make it text."""
    return blocked(
        request,
        "managed_block_unknown_origin",
        f"{exc.path} is not UTF-8 text, so the update will not read or rewrite it. For a binary plist run `plutil -convert xml1` on it, otherwise convert it to UTF-8 text or replace it by hand, then preview again.",
    )


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
    "NotTextError",
    "blocked",
    "facts_evidence",
    "file_mode",
    "migration_export_root_containment",
    "migration_solet_rename",
    "not_text_blocked",
    "plugin_cache_refresh",
    "read_json",
    "read_text",
]
