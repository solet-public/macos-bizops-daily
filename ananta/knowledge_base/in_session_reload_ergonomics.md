# In-Session Reload Ergonomics

Article Layer: 1

Article Role: operations_reference
Tags: knowledge:tag:platform_operations, knowledge:tag:in_session_reload

Embedding Description: How to pick up a knowledge-base edit, a process JSON edit, or a Python source edit live without restarting the solet or losing blob storage.

## Why This Article Exists

Mid-session edits used to mean restarting the solet (which wipes blob storage by default) or running `plugins/default_knowledge_plugin/tools/reload_knowledge_bases.py` (which requires the solet stopped). Neither is necessary anymore. The platform ships four reload/refresh verbs, each targeting a different kind of edit. Pick the one that matches the file you just changed.

Use this article when:

- You edited a `.md` file under any `knowledge_base/` directory.
- You edited a process-definition JSON under any `processes/` directory.
- You edited a Python source file in a plugin or service.
- You are about to suggest a full solet restart and want to confirm that is actually the minimum-disruption path.

## What To Call For Which Edit

| Edit you just made | Verb to call | Provider |
|---|---|---|
| Edited a `.md` file in `knowledge_bases/<name>/` or `plugins/<plugin>/knowledge_base/` | `update` | `knowledge_service` |
| Edited one `processes/<name>.json` file | `refresh_plugin_process` | `knowledge_service` |
| Edited several `processes/*.json` files in the same plugin | `refresh_plugin_processes` | `knowledge_service` |
| Edited a Python source file (`.py`) that is marked `RELOAD_SAFE = True` | `reload_python_module` | `lifecycle_management_service` |
| Edited a Python source file that is NOT marked `RELOAD_SAFE` | `apply_manifest` (see "Picking up new code without a bare restart" below; see also "When Restart Is Still Required") | `lifecycle_management_service` |

The four verbs are independent. Editing both a `.md` and a process JSON requires calling both `update` and `refresh_plugin_process(es)` — one does not imply the other.

## The Four Verbs

### Verb 1: knowledge_service::update

Re-embed a single knowledge base after one or more `.md` files in it changed. The platform walks the knowledge base's `content.patterns.include` glob, re-reads any file whose mtime changed since the last index, and re-embeds the affected chunks in pgvector.

Call shape:

```json
{
  "process_key": "service_interface::knowledge_service::update",
  "arguments": {"name": "<knowledge_base_name>"}
}
```

The `name` is the canonical knowledge-base identifier (the directory name under `knowledge_bases/` or the name declared in the plugin's `manifest.yaml`). Use `knowledge_service::search` afterwards with a query that should hit the new content to confirm the reindex took effect.

### Verb 2: knowledge_service::refresh_plugin_process

Re-read ONE process-definition JSON file and merge it into the live process registry. Use this after editing a single `processes/<name>.json` — for example, tweaking the `description` or `embedding_description` to improve discovery.

Call shape:

```json
{
  "process_key": "service_interface::knowledge_service::refresh_plugin_process",
  "arguments": {
    "plugin_name": "<plugin_name>",
    "process_key": "plugin::<plugin>::<function>"
  }
}
```

For service-interface JSONs that live under `ananta/knowledge_base/processes/<service>/`, pass `plugin_name="ananta"`.

Returns:

```json
{
  "status": "success",
  "plugin_name": "<plugin_name>",
  "refreshed_process_key": "plugin::<plugin>::<function>",
  "updated": true,
  "errors": []
}
```

The key that was refreshed comes back as `refreshed_process_key`, **not** `process_key`. At the top level of a result envelope, `process_key` is reserved for the key of the verb that *produced* the result, and the platform's result contract enforces that. This verb used the name for the key it had just refreshed, so the two meanings collided and every single call raised `RESULT_CONTRACT_VIOLATION` — after the refresh had already been applied and a completed result row had already been stored. Callers were told success while the result was rejected and the parent flow token was blocked. Renamed 2026-08-01; the plural verb was never affected because `process_keys` is spelled differently.

The refresh triggers a full discovery rebuild after merging. Fields editable through this path: `display_name`, `description`, `embedding_description`, `action_definition_template_arguments`, `result_processor_customizations`, `error_processor_customizations`. Fields that require a restart instead: `process_key`, `invocation_schema` (because they require matching code changes).

### Verb 3: knowledge_service::refresh_plugin_processes

Re-read ALL process-definition JSONs for one plugin and merge them. Use this after editing several `processes/*.json` files in the same plugin — saves calling `refresh_plugin_process` per file.

Call shape:

```json
{
  "process_key": "service_interface::knowledge_service::refresh_plugin_processes",
  "arguments": {"plugin_name": "<plugin_name>"}
}
```

For platform service-interface processes, `plugin_name="ananta"` covers every JSON under `ananta/knowledge_base/processes/<service>/`.

### Verb 4: lifecycle_management_service::reload_python_module

Reload one Python module currently loaded into `sys.modules`. Gated by a module-level `RELOAD_SAFE = True` constant — the verb refuses to reload any module that does not declare the marker. Stateful modules (plugin classes holding service references, blob storage, action queue, schema manager, background threads) MUST NOT be marked safe; the gate exists to prevent reload from corrupting platform state.

Call shape:

```json
{
  "process_key": "service_interface::lifecycle_management_service::reload_python_module",
  "arguments": {"module_name": "<fully.qualified.module>"}
}
```

Mechanics: `importlib.reload` semantics apply. The existing module object is updated in place. References already held by other modules to specific functions or classes from the reloaded module are NOT automatically swapped — only fresh imports see the new code. `reload_python_module` is the narrow single-module form; class-body changes, decorator additions, or stateful-module edits require a restart (see "When Restart Is Still Required" below). The earlier `reregister_plugin` verb that promised a grown-up plugin-package reload was deprecated in favor of the blue-green availability design (Option C): restart-via-`apply_manifest` is the canonical zero-downtime deploy path now.

To make a module reload-safe, add at the top:

```python
RELOAD_SAFE = True
```

Mark only modules that hold no module-level state: pure-function DSP, validators, orchestrators that take their dependencies as arguments. Never mark a plugin's main `plugin.py`, blob I/O adapters, or anything that holds a database connection.

## Worked Example: Iterating On A DSP Helper And Its Documentation

Scenario: you are tuning a new audio-processing function and the surrounding documentation is wrong. One round trip touches three of the four verbs.

1. Edit the Python source — `plugins/audio_processing_plugin/src/audio_processing_plugin/audio_analysis.py` (RELOAD_SAFE) — to adjust the algorithm.

2. Call `reload_python_module` to make the new code live:

   ```json
   {
     "process_key": "service_interface::lifecycle_management_service::reload_python_module",
     "arguments": {"module_name": "audio_processing_plugin.audio_analysis"}
   }
   ```

3. Edit the process JSON — `plugins/audio_processing_plugin/knowledge_base/processes/welch_psd.json` — to improve the `description` after observing how the new algorithm behaves.

4. Call `refresh_plugin_process` to push the JSON change into the registry:

   ```json
   {
     "process_key": "service_interface::knowledge_service::refresh_plugin_process",
     "arguments": {
       "plugin_name": "audio_processing_plugin",
       "process_key": "plugin::audio_processing_plugin::welch_psd"
     }
   }
   ```

5. Edit a knowledge-base article — `plugins/audio_processing_plugin/knowledge_base/audio_analysis_overview.md` — to document the change.

6. Call `update` to re-embed the article:

   ```json
   {
     "process_key": "service_interface::knowledge_service::update",
     "arguments": {"name": "audio_processing_knowledge_base"}
   }
   ```

7. Run a `knowledge_service::search` query that should hit the new content and confirm it appears in the top-3.

Total: three reload/refresh calls, no solet restart, no blob storage wiped, no LM Studio dependency, no interruption to whatever else the session is doing.

## When Restart Is Still Required

The four verbs do NOT cover:

- **Changes to `invocation_schema` or `process_key`** in a process JSON. These are merged into the registry at boot from the matching `@platform_process` or `@service_interface_process` decorator; the JSON's prompt-facing fields are the only refreshable surface. Schema or key changes require a restart so the registry rebuilds against the new decorator state.
- **Edits to stateful Python modules** that hold service references, blob storage adapters, action queue, or background threads. These cannot be marked `RELOAD_SAFE`; reloading them mid-flight would orphan their holders. Restart.
- **Edits to a plugin's class definition itself** (the `class FooPlugin(...):` body). Reloading the module that contains the class does NOT re-instantiate existing plugin objects; old instances continue with their previous method tables. Restart via `apply_manifest` + the bound `self_deployment_service` plugin.
- **Adding or removing `@platform_process` / `@service_interface_process` methods**. The process registry is built at startup from the decorator scan; mid-session decorator additions are not picked up. Restart.
- **Schema or database migrations**, plugin entry-point changes in `pyproject.toml`, new service bindings in `service_bindings.json`. Restart.

If you find yourself wanting any of the above without a restart, prefer `apply_manifest` + blue-green restart (`./launch.py` is non-destructive by default; the L2 probe subprocess validates the new manifest before the watchdog cuts over). The earlier `reregister_plugin` path was deprecated because plugins with module-level state (action queues, blob adapters, background threads) can never satisfy its `RELOAD_SAFE`-everywhere precondition.

## Anti-Patterns

- **Calling `update` to refresh process JSONs.** `update` is the article-reindex verb, NOT the process-registry refresh verb. Process JSONs are merged into the registry, not embedded in pgvector — they have a different refresh path.
- **Calling `refresh_plugin_process(es)` after a `.md` edit.** That edit only changed an article's embedding, not the process registry; you need `update`.
- **Marking a stateful module `RELOAD_SAFE`.** The gate exists for a reason; bypassing it leaves the platform in a partial-reload state that is not recoverable in-process. If a module has any module-level state, factor the pure helpers into a sibling module and mark only that sibling.
- **Reaching for `plugins/default_knowledge_plugin/tools/reload_knowledge_bases.py` while the solet is running.** That tool is the offline fallback for when the solet is stopped; it does not coordinate with the live process and can leave the registry inconsistent with the on-disk JSONs. Always prefer the four service verbs while the solet is up.
- **Restarting the solet when one of the four verbs would have sufficed.** Restart wipes blob storage by default and disrupts every in-flight action; prefer the narrowest matching verb.

## Plugin lifecycle introspection — list_plugins

`list_plugins` is the read-only companion to the reload/refresh family. It enumerates every plugin loaded into the live orchestrator and returns a per-plugin row with name, version, readiness status, enabled flag, load priority, registered process count, lifecycle-managed flag, is-running flag, and (when present) the last readiness error string. Use it before any plugin-mutating verb (`set_plugin_enabled`, `reload_plugin_config`, `install_plugin_from_path`, `apply_manifest`) to confirm the target is actually loaded and in the expected state; use it after `apply_manifest`-driven restart to confirm the new manifest's plugins are healthy.

Call shape:

```json
{
  "process_key": "service_interface::lifecycle_management_service::list_plugins",
  "arguments": {"filter": "lifecycle_managed"}
}
```

The optional `filter` is one of `"enabled"`, `"loaded"`, or `"lifecycle_managed"`; omit it for the full roster. Filtering is applied after every row is materialised, so the response shape is identical whether the filter is set or not.

The returned dict has one key, `plugins`, whose value is the list of rows. A row looks like:

```json
{
  "name": "some_plugin",
  "version": "0.1.0",
  "status": "ready",
  "enabled": true,
  "priority": 100,
  "process_count": 7,
  "lifecycle_managed": true,
  "is_running": true
}
```

Worked example: before any plugin-mutating verb, confirm the target is loaded and not held up by an unready dependency.

1. Call `list_plugins` with no filter and look for the target by name.
2. If `status` is `error`, read `last_error` and address the underlying problem first — re-running a mutating verb without fixing the underlying cause will hit the same readiness failure.
3. If `lifecycle_managed` is true and `is_running` is false, the plugin is in a partially-started state; `apply_manifest`-driven restart is the cleanest recovery.
4. After any mutating verb, re-issue `list_plugins` once and confirm `process_count` matches what the plugin's source declares.

`list_plugins` does not touch the registry, the configs, or any plugin code; it is safe to call from anywhere in a flow.

## Plugin enable/disable — set_plugin_enabled

`set_plugin_enabled` toggles a plugin's `enabled` flag in both the persisted per-plugin config and the live orchestrator. Writing the flag is durable across restarts; mutating the live roster is the part that distinguishes this verb from a manual edit of `profile/config/plugins/<plugin_name>.json`.

Call shape:

```json
{
  "process_key": "service_interface::lifecycle_management_service::set_plugin_enabled",
  "arguments": {"plugin_name": "<entry_point_name>", "enabled": false}
}
```

On disable, the verb stops services on the plugin if it is `LifecycleManaged` and removes the plugin from `orchestrator.plugin_manager.plugins` — dispatch resolves a process key to a live instance by roster lookup, so this removal is what makes the plugin's action keys undispatchable. It also refreshes the process registry via `knowledge_service::refresh_plugin_processes`, but that call only re-syncs the plugin's on-disk process-JSON metadata against the knowledge base; it does not itself deregister anything from the runtime dispatch table (that belongs to the atomic installer's remove primitive, which the disable path does not call). The plugin's Python module stays in `sys.modules`; re-enabling reuses already-loaded code where possible.

On enable, if the plugin is already in the live roster the verb only starts its services (no-op when already running). If the plugin is not in the live roster, the verb stages a fresh instance, wires it (injecting `orchestrator_ref` and `event_bus` before `prepare_for_readiness`, the same boot order `install_plugin_from_path` uses), and commits it into the live roster with one atomic dict store via the plugin manager's single-plugin installer — every other already-loaded plugin's instance is left untouched, and a failure at any staging phase (contract validation, wiring) leaves the whole roster, allowlist, and every pre-existing instance byte-identical. If the entry-point is genuinely not installed, the response returns `restart_required=true` with a message naming the missing entry-point so the caller can install it (typically via `install_plugin_from_path`). The same envelope is returned when the entry-point is installed on disk but excluded from this solet's profile manifest allowlist — the staged install checks the manifest before staging, so it never bypasses an operator-set exclusion to load an unlisted plugin; `install_plugin_from_path` will not help in that case, the manifest itself needs editing. These two are the only cases where `restart_required` is true.

The response carries `applied` (true when the runtime state changed this session), `restart_required` (true when the entry-point is missing or manifest-excluded), `plugin_name`, and `message`.

## Plugin priority — set_plugin_priority

`set_plugin_priority` writes the `priority` integer into the per-plugin config. The plugin manager re-reads the file when it discovers entry-points (sorted by `_get_plugin_priority` with the persisted value taking precedence over hardcoded defaults), so the new priority controls load order on the next solet start.

Call shape:

```json
{
  "process_key": "service_interface::lifecycle_management_service::set_plugin_priority",
  "arguments": {"plugin_name": "<entry_point_name>", "priority": 40}
}
```

v1 deliberately does not perform an in-session reorder — the orchestrator's plugin manager and service bindings are stable once startup completes. The response carries `applied=true` (the file write succeeded), `takes_effect="next_restart"`, `plugin_name`, and a message confirming the new value. Verify the new priority is honoured by restarting the solet and re-running `list_plugins`; in the current session `list_plugins` still reflects the cached load order from boot.

Lower values load earlier. Foundational service plugins (state, blob storage, address book) conventionally live below 50; ordinary plugins use 100. Pick a value in the gap when you need a custom plugin to load between two foundational ones.

## Per-plugin config hot reload — reload_plugin_config

`reload_plugin_config` re-reads `profile/config/plugins/<plugin_name>.json` from disk, refreshes the plugin manager's cached view, diffs the prior vs. new config, and calls `plugin.initialize(new_config)` when the plugin implements `initialize`. Use this verb after editing per-plugin config to apply tweaks — sample rates, model selections, thresholds — without reloading any Python modules and without re-instantiating the plugin class.

Call shape:

```json
{
  "process_key": "service_interface::lifecycle_management_service::reload_plugin_config",
  "arguments": {"plugin_name": "<entry_point_name>"}
}
```

The response carries `success`, `plugin_name`, `dirty_keys` (sorted list of config keys whose values differ between the prior cached view and the freshly-read file, including additions and removals), and `message`. An empty `dirty_keys` means the file is in sync with the cached view and the call was a no-op.

This verb is the config-only sibling of `reload_python_module`: it does NOT touch the process registry, does NOT reload module bytecode, and does NOT re-instantiate the plugin class. If the plugin's `initialize` raises on the new payload the cached config has still been refreshed but the plugin instance may be in a half-applied state; an `apply_manifest`-driven restart (with the new config persisted on disk) is the cleanest recovery.

Refuses if the plugin is not currently loaded — for a disabled plugin, re-enable with `set_plugin_enabled` first.

## Platform-level config — update_platform_config

`update_platform_config` persists a single platform-level config entry to `profile/config/platform.json` (under the active profile) and applies it in-process when the platform knows how to. Distinct from `reload_plugin_config`: that verb covers per-plugin JSON; this verb covers platform-wide settings that live outside any plugin — log level, default model selection, env-var-like knobs.

Call shape:

```json
{
  "process_key": "service_interface::lifecycle_management_service::update_platform_config",
  "arguments": {"scope": "logging", "key": "log_level", "value": "DEBUG"}
}
```

The `(scope, key)` pair is validated against an allowlist constant at the top of `plugin_config_io.py`: only pairs explicitly registered there can be written, so a new platform-config knob requires both an allowlist edit and a code path that consumes it. v1 ships one pair: `("logging", "log_level")` — writing it applies the new level to the root logger via `logging.getLogger().setLevel` and the response carries `restart_required=false`.

Future allowlist entries that have no in-process applier are persisted with `restart_required=true` so the caller knows the change is durable but needs a reboot to take effect. The `value` is stored verbatim; type and range validation are the caller's responsibility.

The response carries `applied` (true on a successful write), `prev_value` (the previous value at `scope.key` or `null`), `restart_required`, `scope`, `key`, and `message`.

## Runtime plugin install — install_plugin_from_path

`install_plugin_from_path` brings a plugin online from a local source directory without restarting the solet. It runs `pip install -e <path>` against the active Python interpreter via subprocess, then stages, wires (`orchestrator_ref` and `event_bus` injected before `prepare_for_readiness`, then `initialize` and `start_services`), and commits the new plugin into the live roster with a single atomic dict store via the plugin manager's single-plugin installer — the new plugin is the only roster entry touched, and a failure at any staging phase (contract validation, wiring) leaves every pre-existing plugin instance byte-identical. It also registers the new plugin's process keys via `knowledge_service::refresh_plugin_processes`.

Call shape:

```json
{
  "process_key": "service_interface::lifecycle_management_service::install_plugin_from_path",
  "arguments": {"path": "/absolute/path/to/plugin/source"}
}
```

Validates the path exists, is a directory, and contains either `plugin.yaml` or `pyproject.toml` (refuses otherwise). The pip subprocess inherits the active venv automatically (`sys.executable -m pip`). If pip exits non-zero, the response surfaces the captured stderr so the caller can diagnose dependency conflicts or build errors.

The response carries `installed` (true when a brand-new plugin entry-point appeared after install), `plugin_name` (name of the newly installed plugin, or empty string when none new), `new_process_keys` (sorted list of process keys the new plugin contributes), and `message`.

An upgrade-in-place of an already-loaded plugin returns `installed=false` with an explanatory message — pip succeeded but no new entry-point appeared. In that case the in-process Python code is still the pre-upgrade bytecode (since the running plugin instance was constructed at boot); restart via `apply_manifest` + the bound `self_deployment_service` plugin to pick up the new code.

## Picking up new code without a bare restart — apply_manifest

`apply_manifest` is the recurring answer above whenever "restart to pick up new code" comes up, but it needs its own call shape like every other verb here. To restart with the SAME plugin set — no roster change, just picking up updated code for plugins already installed — pass the current plugin list back unchanged:

```json
{
  "process_key": "service_interface::lifecycle_management_service::apply_manifest",
  "arguments": {
    "new_manifest": {"plugins": ["<the exact list list_plugins just returned>"]},
    "reason": "<short human-readable reason for the restart>",
    "dry_run": true
  }
}
```

Run `list_plugins` first to get the current roster verbatim. Call once with `dry_run=true` to preview the diff and confirm `added_plugins`/`removed_plugins` are both empty (an unchanged roster) before committing with `dry_run` omitted or `false`. `reason` is required (a missing value returns an error); `new_manifest.plugins` is required, `profile_name` and `service_bindings` are optional and default to the current values when omitted.

This delegates the actual restart to whichever plugin is bound to `self_deployment_service` — check `list_plugins` for `macos_self_deployment_plugin` (local blue-green router, zero-downtime swap) versus its absence (single-color, plain process restart) before relying on a specific latency or downtime characteristic. On a router-carrying profile the swap is genuinely zero-downtime for the no-MCP CLI path this platform defaults to; an MCP-connected caller sees the bridge go dark for roughly 30–60 seconds during the cutover regardless.
