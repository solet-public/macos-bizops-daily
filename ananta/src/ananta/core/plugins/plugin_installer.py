"""Atomic single-plugin install/remove — the sanctioned runtime roster mutator.

Fourth collaborator in the Step-9.C plugin-manager decomposition
(design record, Step 9.C, dev-checkout workbench — not part of the shipped tree), added by the C1
atomicity design (`workbench/2026-07-10_install_plugin_from_path_failure_
atomicity_design.md`).

Responsibility: stage a single plugin fully OFF to the side — discover class →
instantiate → contract-validate → wire — then commit it into the live roster
with ONE atomic dict insert; and the symmetric atomic remove. Any failure
before the commit leaves the live roster, the allowlist, and every
pre-existing plugin instance byte-identical (the design's §1 contract, by
construction rather than by compensation logic). This is the ONLY code path
that mutates plugin_manager.plugins at runtime. Boot-time
PluginManager.discover_plugins is permitted only before action dispatch starts;
its serving-state guard refuses a live-serving roster.

The installer owns roster + allowlist mutation, atomic ordering, and process
key-collection. It never touches pip (the artifact lifecycle belongs to the
caller), the orchestrator, or the process registry: the "stop services" and
"de-register process keys" mechanisms are supplied as callbacks (mirroring
`install`'s `wire` callback), so this collaborator stays orchestrator-free and
testable with plain fake callbacks.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

from ananta.core.plugins.plugin_base import PluginBase

if TYPE_CHECKING:
    from ananta.core.plugins.plugin_discovery import PluginDiscovery
    from ananta.core.plugins.plugin_initializer import PluginInitializer
    from ananta.core.plugins.plugin_manager import ConfigManagerProtocol


class PluginInstallError(Exception):
    """A staged install/remove failed at a named phase; live state is untouched.

    Modeled on the platform's fail-fast-with-context exceptions (e.g.
    ``PluginCapabilityError``): carries ``plugin_name`` and the ``phase`` the
    failure occurred in, so the caller builds an honest error envelope without
    inspecting the underlying cause. ``str(exc)`` is the human message; the
    cause is chained via ``from``.
    """

    def __init__(self, plugin_name: str, phase: str, message: str) -> None:
        super().__init__(message)
        self.plugin_name = plugin_name
        self.phase = phase


def collect_process_keys(plugin: PluginBase) -> list[str]:
    """Return the process keys a plugin instance exposes.

    ``plugin::<plugin>::<function>`` for plugin-owned actions, the bare
    ``<function>`` short form otherwise. Shared by the removal primitive's
    de-register step and the install verb's success envelope so both derive
    the keys identically.
    """
    keys: list[str] = []
    for action in plugin.get_available_actions():
        plugin_name = getattr(action, "plugin", "")
        function_name = getattr(action, "function", "")
        if not isinstance(function_name, str) or not function_name:
            continue
        if isinstance(plugin_name, str) and plugin_name:
            keys.append(f"plugin::{plugin_name}::{function_name}")
        else:
            keys.append(function_name)
    return keys


class PluginInstaller:
    """Stage-then-atomic-commit install + symmetric atomic remove for one plugin.

    Constructed once by `PluginManager` and exposed as its public ``installer``
    attribute. Receives the shared ``plugins`` registry by reference plus tiny
    accessor callbacks for the config manager and allowlist, so it mutates the
    same live state the manager owns without reaching into its internals.
    """

    def __init__(
        self,
        *,
        plugins: dict[str, PluginBase],
        discovery: PluginDiscovery,
        initializer: PluginInitializer,
        get_config_manager: Callable[[], ConfigManagerProtocol | None],
        get_allowed_plugins: Callable[[], set[str] | None],
        set_allowed_plugins: Callable[[set[str] | None], None],
    ) -> None:
        self._plugins = plugins
        self._discovery = discovery
        self._initializer = initializer
        self._get_config_manager = get_config_manager
        self._get_allowed_plugins = get_allowed_plugins
        self._set_allowed_plugins = set_allowed_plugins

    def install(
        self, plugin_name: str, wire: Callable[[PluginBase], None],
    ) -> PluginBase:
        """Stage → wire → atomically commit ONE plugin into the live roster.

        Discovery of a single class is side-effect-free, and the staged
        instance is a local variable invisible to every concurrent roster
        reader until the final single-dict commit. Raises
        ``PluginInstallError(phase=...)`` on any failure with the live roster,
        the allowlist, and every pre-existing instance untouched. Never runs
        pip — the artifact lifecycle belongs to the caller.
        """
        if plugin_name in self._plugins:
            raise PluginInstallError(
                plugin_name,
                "precondition",
                f"plugin {plugin_name!r} is already loaded; runtime re-install "
                f"cannot replace a live instance — use apply_manifest "
                f"(blue-green) for code pickup",
            )

        # Stage-discover ONE class. `discover` never mutates the manager; an
        # import-raising plugin is logged-and-skipped inside discovery, so it
        # surfaces here as a miss rather than a raise. Its outer re-raise (e.g.
        # entry-point enumeration failing) is wrapped so nothing escapes.
        try:
            classes = self._discovery.discover(
                {plugin_name}, self._get_config_manager(),
            )
        except Exception as exc:
            raise PluginInstallError(
                plugin_name,
                "staging_discovery",
                f"plugin discovery raised for {plugin_name!r}: {exc}",
            ) from exc
        plugin_class = classes.get(plugin_name)
        if plugin_class is None:
            raise PluginInstallError(
                plugin_name,
                "staging_discovery",
                f"entry-point {plugin_name!r} was not found via discovery "
                f"(import error or failed contract validation during load)",
            )

        # Instantiate + contract-validate on the staged (local) instance.
        try:
            instance = self._initializer.create_plugin_instance(
                plugin_class, plugin_name,
            )
            self._discovery.validate_plugin_contract(instance)
        except Exception as exc:
            raise PluginInstallError(
                plugin_name,
                "contract_validation",
                f"plugin {plugin_name!r} failed contract validation: {exc}",
            ) from exc

        # Wire (orchestrator/event-bus/prepare/initialize/start) the staged
        # instance — the g_suite incident's failure phase, now with zero
        # live-state mutation on a raise.
        try:
            wire(instance)
        except Exception as exc:
            raise PluginInstallError(
                plugin_name,
                "wiring",
                f"plugin {plugin_name!r} failed lifecycle wiring: {exc}",
            ) from exc

        # Commit — single atomic dict store, then allowlist add. `None`
        # allowlist = dev-box no-gating mode; leave it None.
        self._plugins[plugin_name] = instance
        allowed = self._get_allowed_plugins()
        if allowed is not None:
            self._set_allowed_plugins(allowed | {plugin_name})
        return instance

    def remove(
        self,
        plugin_name: str,
        *,
        stop: Callable[[PluginBase], None],
        deregister: Callable[[list[str]], None],
    ) -> PluginBase:
        """Atomically remove ONE plugin from the live roster.

        Happy-path order: stop services → de-register process keys → delete
        from the roster (a single atomic dict delete) → remove from the
        allowlist. De-registering BEFORE the delete means the plugin's process
        keys leave the dispatch registry before its instance leaves the roster,
        so no dispatch ever resolves a key to a missing instance. ``stop`` and
        ``deregister`` are caller-supplied (the lifecycle service reaches
        ``stop_services`` and
        ``process_registry_manager.unregister_dynamic_processes`` — which prunes
        the runtime process-registry dict; see the caller's
        ``_deregister_process_keys`` for the discovery-vector caveat, a C1b
        concern that does not affect the C1a unwind path); the installer owns
        ordering, key-collection, and roster/allowlist mutation.

        Robustness (so a caller's pip rollback can never strand a roster entry
        on an uninstalled distribution): ``stop`` and ``deregister`` are
        best-effort — if either raises, the roster + allowlist are STILL
        cleaned, then a ``PluginInstallError(phase="remove")`` naming the
        partial-teardown detail is raised AFTER the roster is clean, so the
        caller can report the partial teardown honestly. The roster is
        therefore always consistent whether this returns or raises.

        Raises ``PluginInstallError(phase="precondition")`` if the plugin is
        not in the roster.
        """
        plugin = self._plugins.get(plugin_name)
        if plugin is None:
            raise PluginInstallError(
                plugin_name,
                "precondition",
                f"plugin {plugin_name!r} is not in the live roster; nothing to remove",
            )

        teardown_errors: list[str] = []
        try:
            stop(plugin)
        except Exception as exc:  # noqa: BLE001 — surfaced below, never swallowed
            teardown_errors.append(f"stop_services failed: {exc}")
        try:
            deregister(collect_process_keys(plugin))
        except Exception as exc:  # noqa: BLE001 — surfaced below, never swallowed
            teardown_errors.append(f"process de-registration failed: {exc}")

        # ALWAYS clean the roster + allowlist, even if stop/deregister failed.
        del self._plugins[plugin_name]
        allowed = self._get_allowed_plugins()
        if allowed is not None and plugin_name in allowed:
            self._set_allowed_plugins(allowed - {plugin_name})

        if teardown_errors:
            raise PluginInstallError(
                plugin_name,
                "remove",
                f"plugin {plugin_name!r} removed from the roster + allowlist, but "
                f"teardown was partial: {'; '.join(teardown_errors)}",
            )
        return plugin
