"""Register this clone's own coordination-hooks marketplace during an update (iss_c9a7b626).

A pre-Manager clone that never ran the coding-agent hydration step has no
``.claude-plugin/marketplace.json`` and no Claude marketplace under its name, so
``existing::runtime.plugin_cache_refresh`` could only refuse ``plugin_not_visible``
and the operator had to rebuild the create path's three steps by hand.  When the
clone ships its own ``coordination-hooks`` plugin, the refresh now plans those
steps itself, installing the plugin the way create's ``hydration::claude.install_plugin``
does (iss_0744a64a): render the marketplace file with hydration's own renderer (only
when absent), pin every bare ``python3`` hook to ``<clone>/.venv/bin/python3`` with
create's own ``_patch_hook_manifest`` (only when a hook is still bare), ``claude plugin
marketplace add <clone>`` (only when the name is not yet registered; create adds
unconditionally), ``claude plugin install``, and create's own coordination-receipt
publish.  Each step is idempotent and scoped to this clone.  A same-named marketplace registered from anywhere else, this clone
registered under another name, a marketplace file hydration did not render, an
unreadable registry, or a marketplace path that leaves the clone through a symlink
is refused with the exact commands to inspect it; nothing is overwritten and
nothing is written outside the clone.
"""

from __future__ import annotations

import json
import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from solet_setup_contracts.hook_interpreter_pin import HookManifestError, instance_interpreter, pin_hook_interpreter

from .coordination_hook_installation import receipt_path
from .setup_adapter_contract import AdapterRequest, JsonObject, JsonValue, planned_action
from .setup_adapter_runtime import Runtime, describe_outcome
from .setup_plugin_operations import _patch_hook_manifest, _publish_claude_receipt
from .setup_shell_operations import render_claude_marketplace

__all__ = ["HOOK_MANIFEST", "MARKETPLACE_FILE", "PLUGIN_SOURCE", "Registration", "pin_action", "pin_needed", "receipt_action", "register", "registration"]

MARKETPLACE_FILE = Path(".claude-plugin/marketplace.json")
PLUGIN_SOURCE = "./plugins/github_midwife_plugin/claude_plugin/coordination-hooks"
_PLUGIN_MANIFEST = Path("plugins/github_midwife_plugin/claude_plugin/coordination-hooks/.claude-plugin/plugin.json")
_KNOWN_MARKETPLACES = Path(".claude/plugins/known_marketplaces.json")
HOOK_MANIFEST = Path("plugins/github_midwife_plugin/claude_plugin/coordination-hooks/hooks/hooks.json")
_PREVIEW_AGAIN = "then run `solet-manager update {name} --dry-run` again."


@dataclass(frozen=True, slots=True)
class Registration:
    """What registering this clone's marketplace needs, or why it is refused."""

    marketplace: str
    selector: str
    manifest: Path
    write_manifest: bool
    add_marketplace: bool
    pin_hooks: bool = False
    error_kind: str | None = None
    repair: str | None = None

    def actions(self, target: Path) -> list[JsonObject]:
        actions: list[JsonObject] = []
        if self.write_manifest:
            actions.append(planned_action(action_id="cache.render_marketplace", title=f"Render {MARKETPLACE_FILE} for marketplace {self.marketplace} from the hydration template", mutation_kind="file_write", target=str(self.manifest), evidence_ref="cache.coordination_hooks"))
        if self.pin_hooks:
            actions.append(pin_action(target))
        if self.add_marketplace:
            actions.append(planned_action(action_id="cache.register_marketplace", title=f"Register this clone as the Claude marketplace {self.marketplace}", mutation_kind="plugin_marketplace_write", target=str(target), evidence_ref="cache.coordination_hooks"))
        actions.append(planned_action(action_id="cache.install_coordination_hooks", title=f"Install and enable {self.selector}", mutation_kind="plugin_install", target=self.selector, evidence_ref="cache.coordination_hooks"))
        actions.append(receipt_action(target))
        return actions


def pin_action(target: Path) -> JsonObject:
    """Create's own ``claude.patch_hook_interpreter`` (``setup_plugin_operations._plugin_actions``), declaring the tracked write."""
    return planned_action(action_id="claude.patch_hook_interpreter", title=f"Bind every claude Python hook to {instance_interpreter(target)}", mutation_kind="plugin_manifest_write", target=str(target / HOOK_MANIFEST), evidence_ref="cache.coordination_hooks")


def receipt_action(target: Path) -> JsonObject:
    """Create's own ``claude.publish_coordination_receipt``."""
    return planned_action(action_id="claude.publish_coordination_receipt", title="Publish verified coordination hook ownership receipt", mutation_kind="coordination_receipt_write", target=str(receipt_path(target / "profile")), evidence_ref="cache.coordination_hooks")


def registration(request: AdapterRequest, runtime: Runtime, selector: str) -> Registration:
    """Decide the registration from the clone and ``~/.claude/plugins/known_marketplaces.json``; never writes."""
    marketplace = request.name.replace("_", "-")
    manifest = request.target / MARKETPLACE_FILE
    known_path = runtime.home / _KNOWN_MARKETPLACES
    known = _known_marketplaces(known_path)
    pin = pin_needed(request)
    refusal = _clone_refusal(request, marketplace, selector, manifest) or (pin if isinstance(pin, tuple) else None) or _registry_refusal(request, marketplace, selector, known_path, known)
    if refusal is not None:
        return Registration(marketplace, selector, manifest, write_manifest=False, add_marketplace=False, error_kind=refusal[0], repair=refusal[1])
    # ``_registry_refusal`` refuses an unreadable registry, so ``known`` is a mapping here.
    return Registration(marketplace, selector, manifest, write_manifest=not manifest.exists(), add_marketplace=marketplace not in cast(dict[str, Path | None], known), pin_hooks=pin is True)


def pin_needed(request: AdapterRequest) -> bool | tuple[str, str]:
    """Whether a hook is still bare ``python3`` (the create transform, byte for byte), or why the manifest cannot be read."""
    path = request.target / HOOK_MANIFEST
    try:
        return pin_hook_interpreter(path.read_bytes(), instance_interpreter(request.target)) is not None
    except (OSError, HookManifestError) as exc:
        return "hook_manifest_invalid", f"{path} is not a readable hook manifest ({exc}); the update never rewrites it. Inspect `git -C {shlex.quote(str(request.target))} diff -- {HOOK_MANIFEST}`, {_PREVIEW_AGAIN.format(name=request.name)}"


def _clone_refusal(request: AdapterRequest, marketplace: str, selector: str, manifest: Path) -> tuple[str, str] | None:
    """The clone must ship the plugin, the file must stay inside it, and any file there must be hydration's own for this name."""
    again = _PREVIEW_AGAIN.format(name=request.name)
    if not (request.target / _PLUGIN_MANIFEST).is_file():
        return "plugin_not_visible", f"{selector} is not installed and this clone ships no {_PLUGIN_MANIFEST}; the update cannot register it. Check `ls {shlex.quote(str(request.target / _PLUGIN_MANIFEST))}`, {again}"
    outside = _containment_refusal(request, manifest, again)
    if outside is not None:
        return outside
    quoted = shlex.quote(str(manifest))
    try:
        existing = _read_text(manifest)
    except _UnreadableError as exc:
        return "marketplace_manifest_foreign", f"{manifest} exists but is not a readable UTF-8 file ({exc}); the update never overwrites it. Move it aside yourself (`mv {quoted} {quoted}.bak`), {again}"
    if existing is not None and not _is_this_clones_manifest(existing, marketplace):
        return "marketplace_manifest_foreign", f"{manifest} exists but does not declare marketplace {marketplace!r} with coordination-hooks from {PLUGIN_SOURCE}; the update never overwrites it. Move it aside yourself (`mv {quoted} {quoted}.bak`), {again}"
    return None


def _containment_refusal(request: AdapterRequest, manifest: Path, again: str) -> tuple[str, str] | None:
    """N2: every component of the marketplace path must resolve to itself inside the clone; a symlink anywhere refuses."""
    clone = request.target.resolve()
    expected = clone / MARKETPLACE_FILE
    directory = request.target / MARKETPLACE_FILE.parent
    try:
        resolved = (directory.resolve(), manifest.resolve())
    except (OSError, RuntimeError) as exc:
        resolved = None
        detail = str(exc)
    else:
        detail = f"it resolves to {resolved[1]}"
    if resolved == (expected.parent, expected):
        return None
    return "marketplace_path_outside_clone", f"{manifest} does not resolve to {expected} ({detail}); the update writes only inside this clone and never through a symlink. Inspect `ls -ld {shlex.quote(str(directory))} {shlex.quote(str(manifest))}`, replace the symlink with a real directory yourself, {again}"


def _registry_refusal(request: AdapterRequest, marketplace: str, selector: str, known_path: Path, known: dict[str, Path | None] | None) -> tuple[str, str] | None:
    """The name must be free or already this clone's, and this clone must not be registered under another name."""
    again = _PREVIEW_AGAIN.format(name=request.name)
    if known is None:
        return "marketplace_registry_unreadable", f"{known_path} is not a readable UTF-8 JSON object; the update does not guess which marketplaces are registered. Inspect `claude plugin marketplace list`, repair the file, {again}"
    clone = request.target.resolve()
    if marketplace in known and known[marketplace] != clone:
        where = known[marketplace] or "a non-directory source"
        return "marketplace_name_foreign", f"A Claude marketplace named {marketplace!r} is already registered from {where}, not from this clone ({clone}); the update never replaces it. Inspect `claude plugin marketplace list`; if that registration is stale, remove it yourself (`claude plugin marketplace remove {shlex.quote(marketplace)}`), {again}"
    others = sorted(name for name, path in known.items() if path == clone and name != marketplace)
    if others:
        return "marketplace_clone_registered_under_other_name", f"This clone is registered as the Claude marketplace {', '.join(others)}, but the update needs {selector}. Inspect `claude plugin marketplace list`; remove the other registration yourself (`claude plugin marketplace remove {shlex.quote(others[0])}`), {again}"
    return None


def register(request: AdapterRequest, runtime: Runtime, executable: str, plan: Registration) -> tuple[str, str] | None:
    """Apply ``plan`` in create's order; returns the failing step's ``(error_kind, detail)``, or ``None`` once the receipt is published."""
    if plan.write_manifest:
        runtime.atomic_write(plan.manifest, render_claude_marketplace(plan.marketplace), mode=0o644)
    if plan.pin_hooks:
        pin_error = _patch_hook_manifest(request.target / HOOK_MANIFEST, request.target, runtime)
        if pin_error is not None:
            return "hook_manifest_invalid", pin_error
    if plan.add_marketplace:
        added = runtime.run((executable, "plugin", "marketplace", "add", str(request.target)), timeout_seconds=request.timeout_seconds, cwd=request.target)
        if not added.ok:
            return "claude_marketplace_add_failed", f"the Claude CLI refused `claude plugin marketplace add` ({describe_outcome(added)})"
    installed = runtime.run((executable, "plugin", "install", plan.selector), timeout_seconds=request.timeout_seconds, cwd=request.target)
    if not installed.ok:
        return "claude_plugin_install_failed", f"the Claude CLI refused `claude plugin install` ({describe_outcome(installed)})"
    receipt_error = _publish_claude_receipt(request, runtime, executable, plan.marketplace, plan.selector)
    return None if receipt_error is None else ("coordination_receipt_invalid", receipt_error)


class _UnreadableError(Exception):
    """The path exists but is not a readable UTF-8 regular file (a directory, undecodable bytes, no permission)."""


def _read_text(path: Path) -> str | None:
    """The file's text, ``None`` when absent; ``_UnreadableError`` for anything else, never a raw ``OSError``."""
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except (OSError, UnicodeDecodeError) as exc:
        raise _UnreadableError(f"{type(exc).__name__}: {exc}") from exc


def _is_this_clones_manifest(text: str, marketplace: str) -> bool:
    try:
        parsed: object = json.loads(text)
    except json.JSONDecodeError:
        return False
    if not isinstance(parsed, dict):
        return False
    document = cast(JsonObject, parsed)
    plugins = document.get("plugins")
    if document.get("name") != marketplace or not isinstance(plugins, list):
        return False
    return any(isinstance(row, dict) and cast(JsonObject, row).get("name") == "coordination-hooks" and cast(JsonObject, row).get("source") == PLUGIN_SOURCE for row in cast(list[JsonValue], plugins))


def _known_marketplaces(path: Path) -> dict[str, Path | None] | None:
    """Registered name -> resolved directory source (``None`` for a non-directory source); ``None`` when unreadable."""
    try:
        text = _read_text(path)
    except _UnreadableError:
        return None
    if text is None:
        return {}
    try:
        parsed: object = json.loads(text)
    except json.JSONDecodeError:
        return None
    if not isinstance(parsed, dict):
        return None
    return {str(name): _directory_source(entry) for name, entry in cast(JsonObject, parsed).items()}


def _directory_source(entry: JsonValue) -> Path | None:
    source = cast(JsonObject, entry).get("source") if isinstance(entry, dict) else None
    if not isinstance(source, dict):
        return None
    row = cast(JsonObject, source)
    path = row.get("path")
    return Path(path).resolve() if row.get("source") == "directory" and isinstance(path, str) else None
