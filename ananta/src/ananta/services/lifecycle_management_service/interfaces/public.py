"""Lifecycle Management Service Public API.

AI-discoverable service lifecycle operations with @service_interface_process decorators.
All methods in this interface are indexed for process discovery.

Discoverability Policy (Task #47, 2026-05-24):
- EVERY method declares ``is_discoverable=True`` explicitly. The base decorator
  default for ``@service_interface_process`` is ``is_discoverable=False`` (service
  methods are presumed internal); lifecycle-management operations are all
  operator-callable (start_service_via_interface, stop_service,
  reload_python_module, list_plugins, list_available_plugins,
  set_plugin_enabled, set_plugin_priority, reload_plugin_config,
  update_platform_config, install_plugin_from_path, apply_manifest), so the
  per-method flag overrides the default. the solet is the operator's primary tool;
  these are first-class agent operations.
- Adding a new method without ``is_discoverable=True`` will SILENTLY exclude it
  from ``process_search`` and the agent will not be able to find it.
"""

from abc import ABC, abstractmethod
from typing import Any

from ananta.core.actions.action_metadata import (
    MergeErrorProcessorCustomizations,
    ParameterMetadata,
    ParameterType,
    ReturnValueSchema,
)
from ananta.core.services.service_interface_decorator import service_interface_process

LIST_PLUGINS_FILTER_VALUES: tuple[str, ...] = (
    "enabled",
    "loaded",
    "lifecycle_managed",
)

PLATFORM_CONFIG_TAKES_EFFECT_NEXT_RESTART = "next_restart"
PLATFORM_CONFIG_TAKES_EFFECT_IMMEDIATE = "immediate"


class LifecycleManagementAPI(ABC):
    """Public lifecycle management operations - AI-discoverable via process registry.

    This interface defines service lifecycle operations that can be discovered
    and invoked by the AI orchestration system:

    1. start_service_via_interface - Start a service plugin with configuration
    2. stop_service - Stop a running service
    3. reload_python_module - Reload a single RELOAD_SAFE module without restart
    4. list_plugins - Enumerate the live plugin roster with status / config metadata
    5. list_available_plugins - Enumerate plugins loadable into the next manifest
       (installed entry points + source-tree candidates)
    6. apply_manifest - Validate + atomically write a new manifest + service
       bindings, then delegate restart to the bound self_deployment_service plugin

    Each method is decorated with complete metadata for process registry.
    """

    @service_interface_process(
        name="start_service_via_interface",
        is_discoverable=True,
        provider="lifecycle_management_service",
        parameters={
            "service": ParameterMetadata(
                description="Service definition with provider information",
                required=True,
                type=ParameterType.OBJECT,
            ),
            "start": ParameterMetadata(
                description="Start action file to load (e.g., 'evaluate_input.json')",
                required=False,
                type=ParameterType.STRING,
            ),
            "config": ParameterMetadata(
                description="Service-specific configuration",
                required=False,
                type=ParameterType.OBJECT,
            ),
        },
        return_value_schema=ReturnValueSchema(
            description="Service start result",
            type=ParameterType.OBJECT,
            properties={
                "success": ParameterMetadata(
                    type=ParameterType.BOOLEAN,
                    description="Whether service started successfully",
                    required=False,
                ),
                "service_name": ParameterMetadata(
                    type=ParameterType.STRING, description="Name of started service", required=False
                ),
                "message": ParameterMetadata(
                    type=ParameterType.STRING, description="Status message", required=False
                ),
            },
            usage_patterns=[
                "Start console service for user interaction",
                "Start background services for processing",
            ],
        ),
        is_enabled=True,
        error_processor_customizations=MergeErrorProcessorCustomizations(
            retryable=True
        )
    )
    @abstractmethod
    def start_service_via_interface(
        self,
        service: dict[str, Any] | str,
        start: str | None = None,
        config: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Start a service plugin with specified configuration.

        Args:
            service: Service definition dict with 'name' key, or service name string
            start: Start action file to load (e.g., 'evaluate_input.json')
            config: Service-specific configuration

        Returns:
            ActionResult with service start status
        """
        ...

    @service_interface_process(
        name="stop_service",
        is_discoverable=True,
        provider="lifecycle_management_service",
        parameters={
            "service": ParameterMetadata(
                description="Name of the service to stop", required=True, type=ParameterType.STRING
            ),
        },
        return_value_schema=ReturnValueSchema(
            description="Service stop result",
            type=ParameterType.OBJECT,
            properties={
                "success": ParameterMetadata(
                    type=ParameterType.BOOLEAN,
                    description="Whether service stopped successfully",
                    required=False,
                ),
                "service_name": ParameterMetadata(
                    type=ParameterType.STRING, description="Name of stopped service", required=False
                ),
                "message": ParameterMetadata(
                    type=ParameterType.STRING, description="Status message", required=False
                ),
            },
            usage_patterns=[
                "Stop console service cleanly",
                "Shutdown background services",
            ],
        ),
        is_enabled=True,
        error_processor_customizations=MergeErrorProcessorCustomizations(
            retryable=True
        )
    )
    @abstractmethod
    def stop_service(self, service: str) -> dict[str, Any]:
        """Stop a running service.

        Args:
            service: Name of the service plugin to stop

        Returns:
            ActionResult with service stop status
        """
        ...

    @service_interface_process(
        name="reload_python_module",
        is_discoverable=True,
        provider="lifecycle_management_service",
        parameters={
            "module_name": ParameterMetadata(
                description=(
                    "Fully-qualified Python module name to reload, as it appears "
                    "in sys.modules (e.g. 'audio_processing_plugin.audio_analysis')."
                ),
                required=True,
                type=ParameterType.STRING,
            ),
        },
        return_value_schema=ReturnValueSchema(
            description="Module reload result",
            type=ParameterType.OBJECT,
            properties={
                "success": ParameterMetadata(
                    type=ParameterType.BOOLEAN,
                    description="Whether the module was reloaded successfully",
                    required=False,
                ),
                "module_name": ParameterMetadata(
                    type=ParameterType.STRING,
                    description="Module that was targeted",
                    required=False,
                ),
                "message": ParameterMetadata(
                    type=ParameterType.STRING,
                    description="Status or refusal detail",
                    required=False,
                ),
            },
            usage_patterns=[
                "Reload a pure-DSP module after a code edit without restarting",
                "Apply a synthesizers fix without losing blob storage state",
            ],
        ),
        is_enabled=True,
        error_processor_customizations=MergeErrorProcessorCustomizations(
            retryable=False,
        )
    )
    @abstractmethod
    def reload_python_module(self, module_name: str) -> dict[str, Any]:
        """Reload a Python module marked ``RELOAD_SAFE = True`` without restarting the solet.

        The service refuses to reload modules that do not declare a module-level
        ``RELOAD_SAFE = True`` constant. Stateful modules (plugin classes,
        blob storage adapters, action queue, etc.) MUST NOT be marked safe;
        marking them is a configuration error that the safety gate protects
        against.

        Args:
            module_name: Fully-qualified module name as listed in ``sys.modules``.

        Returns:
            Result dict with success flag, module name, and status / refusal message.
        """
        ...

    @service_interface_process(
        name="list_plugins",
        is_discoverable=True,
        provider="lifecycle_management_service",
        parameters={
            "filter": ParameterMetadata(
                description=(
                    "Optional filter restricting the returned roster. Accepted values: "
                    "'enabled' (only plugins with enabled != false in their config), "
                    "'loaded' (only plugins currently instantiated on the orchestrator), "
                    "'lifecycle_managed' (only plugins implementing the LifecycleManaged "
                    "protocol). Omit to return every loaded plugin."
                ),
                required=False,
                type=ParameterType.STRING,
                validation={"enum": list(LIST_PLUGINS_FILTER_VALUES)},
            ),
        },
        return_value_schema=ReturnValueSchema(
            description="Roster of plugins known to the live orchestrator",
            type=ParameterType.OBJECT,
            properties={
                "plugins": ParameterMetadata(
                    type=ParameterType.LIST,
                    description=(
                        "Each entry carries name, version, readiness status, enabled flag, "
                        "load priority, number of registered processes, whether the plugin "
                        "is LifecycleManaged, whether its services are currently running, "
                        "and (when present) the last readiness error string."
                    ),
                    required=False,
                ),
            },
            usage_patterns=[
                "Take inventory of installed plugins before reregistering one",
                "Check which plugins are LifecycleManaged before stopping services",
                "Diagnose missing capability by confirming the plugin is loaded and enabled",
            ],
        ),
        is_enabled=True,
        error_processor_customizations=MergeErrorProcessorCustomizations(
            retryable=False,
        ),
    )
    @abstractmethod
    def list_plugins(self, filter: str | None = None) -> dict[str, Any]:
        """Return the live plugin roster with status and config metadata.

        Args:
            filter: Optional roster filter — one of ``"enabled"``, ``"loaded"``,
                ``"lifecycle_managed"`` (validated against the allowed set). Omit
                to return every loaded plugin.

        Returns:
            Result dict containing ``plugins``: a list of dicts, each with
            ``name``, ``version``, ``status``, ``enabled``, ``priority``,
            ``process_count``, ``lifecycle_managed``, ``is_running``, and
            optionally ``last_error``.
        """
        ...

    @service_interface_process(
        name="list_available_plugins",
        is_discoverable=True,
        provider="lifecycle_management_service",
        parameters={},
        return_value_schema=ReturnValueSchema(
            description=(
                "Plugins that could be loaded into the next manifest, sourced "
                "from installed entry points plus source-tree directories with "
                "valid plugin.yaml or pyproject.toml."
            ),
            type=ParameterType.OBJECT,
            properties={
                "plugins": ParameterMetadata(
                    type=ParameterType.LIST,
                    description=(
                        "Sorted list of per-plugin records. Each entry carries "
                        "name (str), source ('installed_entry_point' for plugins "
                        "registered via importlib.metadata.entry_points, or "
                        "'available_uninstalled' for plugins present in the "
                        "repo's plugins/ directory but not yet pip-installed), "
                        "has_metadata (bool), version (str|null), description "
                        "(str|null), and implements (list of interface short "
                        "names parsed from plugin.yaml)."
                    ),
                    required=False,
                ),
            },
            usage_patterns=[
                "Populate apply_manifest's diff preview before writing a new manifest",
                "Confirm a candidate plugin is installable before referencing it in a manifest",
                "Survey the solet image's plugin catalog",
            ],
        ),
        is_enabled=True,
        error_processor_customizations=MergeErrorProcessorCustomizations(
            retryable=False,
        ),
    )
    @abstractmethod
    def list_available_plugins(self) -> dict[str, Any]:
        """Enumerate plugins that could be loaded into the next manifest.

        Walks ``importlib.metadata.entry_points(group="ananta.plugins")``
        for installed plugins and the repo's ``plugins/`` directory for
        source-tree candidates not yet installed. The response shape is
        the input ``list_available_plugins`` expects from
        ``apply_manifest`` when populating its diff preview.

        Returns:
            Result dict containing ``plugins``: a list of dicts, each with
            ``name``, ``source`` (``"installed_entry_point"`` or
            ``"available_uninstalled"``), ``has_metadata``, ``version``,
            ``description``, and ``implements``.
        """
        ...

    @service_interface_process(
        name="set_plugin_enabled",
        is_discoverable=True,
        provider="lifecycle_management_service",
        parameters={
            "plugin_name": ParameterMetadata(
                description=(
                    "Name of the plugin to enable or disable (matches the "
                    "entry-point name and the key under "
                    "orchestrator.plugin_manager.plugins)."
                ),
                required=True,
                type=ParameterType.STRING,
            ),
            "enabled": ParameterMetadata(
                description=(
                    "Target state. True schedules the plugin into the live roster, "
                    "load if needed, and starts services for LifecycleManaged plugins. "
                    "False stops services, removes the plugin from the live roster, "
                    "and refreshes the process registry so its action keys disappear."
                ),
                required=True,
                type=ParameterType.BOOLEAN,
            ),
        },
        return_value_schema=ReturnValueSchema(
            description="Plugin enable/disable result",
            type=ParameterType.OBJECT,
            properties={
                "applied": ParameterMetadata(
                    type=ParameterType.BOOLEAN,
                    description="Whether the runtime state change took effect in this session",
                    required=False,
                ),
                "restart_required": ParameterMetadata(
                    type=ParameterType.BOOLEAN,
                    description="True if the persisted change needs a solet restart to fully apply",
                    required=False,
                ),
                "plugin_name": ParameterMetadata(
                    type=ParameterType.STRING,
                    description="Plugin the call targeted",
                    required=False,
                ),
                "message": ParameterMetadata(
                    type=ParameterType.STRING,
                    description="Status or refusal detail",
                    required=False,
                ),
            },
            usage_patterns=[
                "Disable a misbehaving plugin without restarting the solet",
                "Re-enable a plugin after fixing its config",
            ],
        ),
        is_enabled=True,
        error_processor_customizations=MergeErrorProcessorCustomizations(
            retryable=False,
        ),
    )
    @abstractmethod
    def set_plugin_enabled(
        self, plugin_name: str, enabled: bool
    ) -> dict[str, Any]:
        """Toggle a plugin's enabled state, persisting and applying live.

        Writes ``enabled`` into the per-plugin config file (creating it if
        absent) and then applies the change on the live orchestrator: enabling
        loads the plugin if it is not already present and starts its services
        when it is LifecycleManaged; disabling stops its services, removes it
        from the live roster, and refreshes the process registry so its action
        keys disappear from discovery.

        Args:
            plugin_name: Plugin to enable or disable.
            enabled: True to enable; False to disable.

        Returns:
            Result dict with ``applied``, ``restart_required``,
            ``plugin_name``, and ``message``.
        """
        ...

    @service_interface_process(
        name="set_plugin_priority",
        is_discoverable=True,
        provider="lifecycle_management_service",
        parameters={
            "plugin_name": ParameterMetadata(
                description="Plugin whose load priority should change.",
                required=True,
                type=ParameterType.STRING,
            ),
            "priority": ParameterMetadata(
                description=(
                    "New load-priority integer. Lower values load earlier; "
                    "foundational service plugins use values below 50. The "
                    "value is persisted to the per-plugin config file and "
                    "consulted by the plugin manager on the next start."
                ),
                required=True,
                type=ParameterType.INTEGER,
            ),
        },
        return_value_schema=ReturnValueSchema(
            description="Plugin priority update result",
            type=ParameterType.OBJECT,
            properties={
                "applied": ParameterMetadata(
                    type=ParameterType.BOOLEAN,
                    description="Whether the change was persisted to disk",
                    required=False,
                ),
                "takes_effect": ParameterMetadata(
                    type=ParameterType.STRING,
                    description=(
                        "When the new priority becomes active. v1 always returns "
                        "'next_restart'; in-session reorder is out of scope."
                    ),
                    required=False,
                ),
                "plugin_name": ParameterMetadata(
                    type=ParameterType.STRING,
                    description="Plugin the call targeted",
                    required=False,
                ),
                "message": ParameterMetadata(
                    type=ParameterType.STRING,
                    description="Status or refusal detail",
                    required=False,
                ),
            },
            usage_patterns=[
                "Promote a plugin to load earlier on the next restart",
                "Demote a plugin so a higher-priority alternative wins service binding",
            ],
        ),
        is_enabled=True,
        error_processor_customizations=MergeErrorProcessorCustomizations(
            retryable=False,
        ),
    )
    @abstractmethod
    def set_plugin_priority(
        self, plugin_name: str, priority: int
    ) -> dict[str, Any]:
        """Persist a plugin's load priority; takes effect on the next start.

        Writes ``priority`` into the per-plugin config file (creating it if
        absent). The plugin manager re-reads the file on the next discovery
        pass and sorts entry points by the persisted value. v1 does not
        perform an in-session reorder, so the response carries
        ``takes_effect = "next_restart"``.

        Args:
            plugin_name: Plugin whose priority should change.
            priority: New load-priority integer.

        Returns:
            Result dict with ``applied``, ``takes_effect``, ``plugin_name``,
            and ``message``.
        """
        ...

    @service_interface_process(
        name="reload_plugin_config",
        is_discoverable=True,
        provider="lifecycle_management_service",
        parameters={
            "plugin_name": ParameterMetadata(
                description="Plugin whose config should be re-read from disk.",
                required=True,
                type=ParameterType.STRING,
            ),
        },
        return_value_schema=ReturnValueSchema(
            description="Plugin config reload result",
            type=ParameterType.OBJECT,
            properties={
                "success": ParameterMetadata(
                    type=ParameterType.BOOLEAN,
                    description="Whether the new config was pushed into the plugin",
                    required=False,
                ),
                "plugin_name": ParameterMetadata(
                    type=ParameterType.STRING,
                    description="Plugin the call targeted",
                    required=False,
                ),
                "dirty_keys": ParameterMetadata(
                    type=ParameterType.LIST,
                    description=(
                        "Config keys whose values differ between the prior in-memory "
                        "view and the freshly-read on-disk file."
                    ),
                    required=False,
                ),
                "message": ParameterMetadata(
                    type=ParameterType.STRING,
                    description="Status or refusal detail",
                    required=False,
                ),
            },
            usage_patterns=[
                "Pick up a per-plugin config edit without restarting the solet",
                "Apply a tweaked sample rate or model selection mid-session",
            ],
        ),
        is_enabled=True,
        error_processor_customizations=MergeErrorProcessorCustomizations(
            retryable=False,
        ),
    )
    @abstractmethod
    def reload_plugin_config(self, plugin_name: str) -> dict[str, Any]:
        """Re-read a plugin's config and push the new view into the plugin.

        Reads the on-disk per-plugin config, computes the diff against the
        plugin manager's cached view, refreshes the cache, and (when the
        plugin implements ``initialize``) calls ``initialize(new_config)``
        so the plugin can rebind its runtime parameters.

        Args:
            plugin_name: Plugin whose config should be re-read.

        Returns:
            Result dict with ``success``, ``plugin_name``, ``dirty_keys``
            (config keys whose values changed since the last load), and
            ``message``.
        """
        ...

    @service_interface_process(
        name="update_platform_config",
        is_discoverable=True,
        provider="lifecycle_management_service",
        parameters={
            "scope": ParameterMetadata(
                description=(
                    "Top-level section of platform.json being mutated, e.g. 'logging'. "
                    "Validated against the platform-config allowlist."
                ),
                required=True,
                type=ParameterType.STRING,
            ),
            "key": ParameterMetadata(
                description=(
                    "Key within the scope, e.g. 'log_level'. Validated against the "
                    "platform-config allowlist for the chosen scope."
                ),
                required=True,
                type=ParameterType.STRING,
            ),
            "value": ParameterMetadata(
                description=(
                    "New value for the scope.key entry. Stored verbatim in platform.json; "
                    "consumer responsibility to validate type/range."
                ),
                required=True,
                type=ParameterType.OBJECT,
            ),
        },
        return_value_schema=ReturnValueSchema(
            description="Platform config update result",
            type=ParameterType.OBJECT,
            properties={
                "applied": ParameterMetadata(
                    type=ParameterType.BOOLEAN,
                    description="Whether the new value was persisted",
                    required=False,
                ),
                "prev_value": ParameterMetadata(
                    type=ParameterType.OBJECT,
                    description=(
                        "Previous value at scope.key (null when no prior entry existed)"
                    ),
                    required=False,
                ),
                "restart_required": ParameterMetadata(
                    type=ParameterType.BOOLEAN,
                    description=(
                        "True if the new value cannot be applied in-process and only takes "
                        "effect on the next solet restart"
                    ),
                    required=False,
                ),
                "scope": ParameterMetadata(
                    type=ParameterType.STRING,
                    description="Scope written",
                    required=False,
                ),
                "key": ParameterMetadata(
                    type=ParameterType.STRING,
                    description="Key written",
                    required=False,
                ),
                "message": ParameterMetadata(
                    type=ParameterType.STRING,
                    description="Status or refusal detail",
                    required=False,
                ),
            },
            usage_patterns=[
                "Raise the platform log level to DEBUG without restarting",
                "Persist an allowlisted env-var-like setting from a process call",
            ],
        ),
        is_enabled=True,
        error_processor_customizations=MergeErrorProcessorCustomizations(
            retryable=False,
        ),
    )
    @abstractmethod
    def update_platform_config(
        self, scope: str, key: str, value: Any
    ) -> dict[str, Any]:
        """Persist a single platform-level config entry and apply it live where possible.

        Validates ``(scope, key)`` against the platform-config allowlist,
        writes ``platform.json`` under the active profile, and applies the
        change in-process for the small set of keys the platform knows how to
        rebind without a restart (currently only ``logging.log_level``). Other
        allowlisted entries are persisted but require a restart to take
        effect; ``restart_required`` discriminates.

        Args:
            scope: Top-level section of platform.json (e.g. ``"logging"``).
            key: Key within the scope (e.g. ``"log_level"``).
            value: New value to persist verbatim.

        Returns:
            Result dict with ``applied``, ``prev_value``, ``restart_required``,
            ``scope``, ``key``, and ``message``.
        """
        ...

    @service_interface_process(
        name="install_plugin_from_path",
        is_discoverable=True,
        provider="lifecycle_management_service",
        parameters={
            "path": ParameterMetadata(
                description=(
                    "Filesystem path to the plugin source root (the directory that "
                    "contains plugin.yaml or pyproject.toml). The directory will be "
                    "installed into the active venv with `pip install -e <path>`."
                ),
                required=True,
                type=ParameterType.STRING,
            ),
        },
        return_value_schema=ReturnValueSchema(
            description="Plugin installation result",
            type=ParameterType.OBJECT,
            properties={
                "installed": ParameterMetadata(
                    type=ParameterType.BOOLEAN,
                    description=(
                        "True when a brand-new plugin entry-point appeared after install"
                    ),
                    required=False,
                ),
                "plugin_name": ParameterMetadata(
                    type=ParameterType.STRING,
                    description="Name of the newly-installed plugin (empty if none new)",
                    required=False,
                ),
                "new_process_keys": ParameterMetadata(
                    type=ParameterType.LIST,
                    description="Process keys that the new plugin contributes to the registry",
                    required=False,
                ),
                "message": ParameterMetadata(
                    type=ParameterType.STRING,
                    description="Status or refusal detail",
                    required=False,
                ),
            },
            usage_patterns=[
                "Install a freshly-checked-out plugin without restarting the solet",
                "Bring a sibling plugin online from a workbench scratch directory",
            ],
        ),
        is_enabled=True,
        error_processor_customizations=MergeErrorProcessorCustomizations(
            retryable=False,
        ),
    )
    @abstractmethod
    def install_plugin_from_path(self, path: str) -> dict[str, Any]:
        """Install a plugin from a local source directory at runtime.

        Validates that ``path`` exists and contains ``plugin.yaml`` or
        ``pyproject.toml``, runs ``pip install -e <path>`` against the active
        Python interpreter, re-runs entry-point discovery on the plugin
        manager, and reports whichever plugin entry-point is newly present.

        Args:
            path: Filesystem path to the plugin source root.

        Returns:
            Result dict with ``installed``, ``plugin_name``,
            ``new_process_keys``, and ``message``.
        """
        ...

    @service_interface_process(
        name="apply_manifest",
        is_discoverable=True,
        provider="lifecycle_management_service",
        parameters={
            "new_manifest": ParameterMetadata(
                description=(
                    "Proposed manifest dict. v1 scope per Architect §15.1 "
                    "(workbench/2026-05-30_plugin_lifecycle_architect_pass.md): "
                    "only ``{plugins: list[str], profile_name?: str}`` is "
                    "accepted. ``service_bindings`` and ``plugin_config_overrides`` "
                    "in the payload are REJECTED with a stable "
                    "``bindings_change_rejected_in_v1`` token before any disk "
                    "write — full-bundle externalization is deferred to v2 "
                    "(per Codex review #2 Finding 2, where cloud restart "
                    "uploads only manifest.yaml to S3 and bindings changes "
                    "would split-brain the new color). Operators who need "
                    "binding mutations should edit the profile template + "
                    "re-run launch.py for now; per-plugin config edits "
                    "use ``reload_plugin_config``. The verb auto-synthesizes "
                    "service_bindings from the currently-bound services so "
                    "downstream validation + write still see a coherent "
                    "manifest shape."
                ),
                required=True,
                type=ParameterType.OBJECT,
            ),
            "reason": ParameterMetadata(
                description=(
                    "Operator-supplied audit string. Recorded in the "
                    "deployment plugin's restart message and surfaced in "
                    "the response so future status calls can trace 'why "
                    "was this restart triggered'."
                ),
                required=True,
                type=ParameterType.STRING,
            ),
            "expected_etag": ParameterMetadata(
                description=(
                    "Compare-and-swap precondition. The caller passes the "
                    "etag they observed when they last read the on-disk "
                    "state. The verb refuses with status='rejected' + "
                    "reason='precondition_failed' if the on-disk etag has "
                    "shifted in the interim. Read the current etag from a "
                    "dry-run call's response (status='dry_run' carries the "
                    "current etag for first-time callers)."
                ),
                required=False,
                type=ParameterType.STRING,
            ),
            "dry_run": ParameterMetadata(
                description=(
                    "When true, validate + diff + return without writing "
                    "or delegating restart. Use to preview the change "
                    "shape and to read the current etag before a "
                    "subsequent committing call."
                ),
                required=False,
                type=ParameterType.BOOLEAN,
                default=False,
            ),
        },
        return_value_schema=ReturnValueSchema(
            description="apply_manifest outcome",
            type=ParameterType.OBJECT,
            properties={
                "status": ParameterMetadata(
                    type=ParameterType.STRING,
                    description=(
                        "'applied' when the manifest was validated, written, "
                        "and the deployment plugin reported a manifest-applied "
                        "restart status (queued / completed). "
                        "'dry_run' when the caller passed dry_run=true. "
                        "'rejected' when pre-flight validation or the CAS "
                        "check failed (no disk write happened); "
                        "rejection_reasons explains why. "
                        "'restart_failed_after_manifest_commit' (Codex review "
                        "#2 Finding 1) when the manifest WAS written locally "
                        "(and to S3 for cloud) but the deployment plugin "
                        "could not schedule the restart — a partial remote "
                        "commit that needs operator recovery; restart_status "
                        "carries the underlying failure token and "
                        "rejection_reasons formats the deploy error."
                    ),
                    required=False,
                ),
                "diff": ParameterMetadata(
                    type=ParameterType.OBJECT,
                    description=(
                        "Set-level diff between current and proposed: "
                        "{added_plugins, removed_plugins, rebound_services}."
                    ),
                    required=False,
                ),
                "current_etag": ParameterMetadata(
                    type=ParameterType.STRING,
                    description=(
                        "The current on-disk etag at the moment of read. "
                        "Populated on every response (rejection, dry-run, "
                        "and committed). The caller passes this back as "
                        "expected_etag on a subsequent committing call."
                    ),
                    required=False,
                ),
                "new_etag": ParameterMetadata(
                    type=ParameterType.STRING,
                    description=(
                        "Etag of the newly-written manifest + bindings "
                        "pair. Populated when status='applied'."
                    ),
                    required=False,
                ),
                "manifest_written_to": ParameterMetadata(
                    type=ParameterType.STRING,
                    description=(
                        "Path of the manifest.yaml that was written. "
                        "Populated when status='applied'."
                    ),
                    required=False,
                ),
                "service_bindings_written_to": ParameterMetadata(
                    type=ParameterType.STRING,
                    description=(
                        "Path of the service_bindings.json that was "
                        "written. Populated when status='applied'."
                    ),
                    required=False,
                ),
                "restart_action_id": ParameterMetadata(
                    type=ParameterType.STRING,
                    description=(
                        "Identifier the bound deployment plugin returned "
                        "from its restart_with_manifest call. Semantics "
                        "depend on the implementation — cloud sibling "
                        "returns a pollable id consumable by "
                        "self_deployment_service::deploy_status; local "
                        "sibling returns an audit-only token. Empty "
                        "string on status='restart_failed_after_manifest_commit'."
                    ),
                    required=False,
                ),
                "restart_status": ParameterMetadata(
                    type=ParameterType.STRING,
                    description=(
                        "Verbatim status the bound deployment plugin "
                        "returned from restart_with_manifest. Populated "
                        "when status='applied' (carries the happy-path "
                        "token, e.g. 'queued' or 'completed') or "
                        "when status='restart_failed_after_manifest_commit' "
                        "(carries one of 'failed', 'unbound', "
                        "'plugin_protocol_error', 'plugin_raised') so the "
                        "operator can tell exactly what the deployment "
                        "plugin reported."
                    ),
                    required=False,
                ),
                "rejection_reasons": ParameterMetadata(
                    type=ParameterType.LIST,
                    description=(
                        "List of human-readable reasons populated when "
                        "status='rejected'. Empty otherwise."
                    ),
                    required=False,
                ),
                "message": ParameterMetadata(
                    type=ParameterType.STRING,
                    description="Status detail.",
                    required=False,
                ),
            },
            usage_patterns=[
                "Preview a plugin add via dry_run=true; commit on a follow-up call",
                "Atomically swap a service binding alongside a plugin set change",
                "Roll forward to a new local manifest and trigger a self-restart",
            ],
        ),
        is_enabled=True,
        error_processor_customizations=MergeErrorProcessorCustomizations(
            retryable=False,
        ),
    )
    @abstractmethod
    def apply_manifest(
        self,
        new_manifest: dict[str, Any],
        reason: str,
        expected_etag: str | None = None,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Validate, write, and restart with a new manifest.

        The verb is the platform-tier orchestrator of the manifest
        lifecycle: it narrows the v1 payload (Architect §15.1 —
        plugin-list-only externalization; ``service_bindings`` /
        ``plugin_config_overrides`` in the payload are rejected before
        any write), runs binding-satisfaction pre-flight against
        ``binding_validator`` using the *currently-bound* services
        (synthesized into the effective manifest), computes a set-level
        diff, atomically writes ``service_bindings.json`` (unchanged
        content in v1) followed by ``manifest.yaml`` under a sha256 CAS,
        then delegates the actual restart to whichever plugin is
        currently bound to ``self_deployment_service``. Per Codex review #2
        Finding 1, the restart status is propagated into the response —
        a write that succeeds but cannot schedule a cutover surfaces as
        ``status="restart_failed_after_manifest_commit"``, not
        ``"applied"``.

        Args:
            new_manifest: Proposed manifest. v1 shape:
                ``{plugins: list[str], profile_name?: str}``. Passing
                ``service_bindings`` or ``plugin_config_overrides`` is
                rejected with ``bindings_change_rejected_in_v1`` before
                any disk activity (deferred to v2 — full-bundle
                externalization).
            reason: Operator-supplied audit string.
            expected_etag: CAS token from the caller's prior read.
                When ``None``, the verb runs in unchecked mode (caller
                accepts the risk of a concurrent override).
            dry_run: When ``True``, validate + diff + return current
                etag without writing or restarting.

        Returns:
            Result dict per the return-value schema. ``status`` is one
            of ``"applied"``, ``"dry_run"``, ``"rejected"``, or
            ``"restart_failed_after_manifest_commit"``; ``restart_status``
            carries the deployment plugin's verbatim status on the
            applied/failed paths.
        """
        ...
