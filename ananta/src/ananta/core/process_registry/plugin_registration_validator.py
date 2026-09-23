"""Plugin-registration-time validation (EdgeProcessProvider + discoverable).

Extracted from `ProcessRegistryBuilder` during the Step 9.A decomposition
(`workbench/2026-05-25_plugin_god_class_remediation.md` §9.1).

Scope distinction (per §9.1.3): this validator runs at *plugin discovery*
time and enforces consistency between a plugin's `@platform_process`
decorations and its `get_edge_process_definitions()` dict, plus the
embedding-description requirement on discoverable processes. It does
NOT extend `ProcessValidationManager` (which validates *built registry
entries* against a post-build schema contract — different concern).

Public entry points:
  - `validate_edge_process_provider(plugin_name, plugin_instance, actions)`
    fail-fast checks on the EdgeProcessProvider contract.
  - `validate_all_embedding_descriptions(registry)`
    sweeps the post-merge registry for discoverable processes lacking
    embedding descriptions.
"""

from __future__ import annotations

import ast
import logging
import sys
from pathlib import Path

from ananta.core.actions.action_metadata import ActionMetadata
from ananta.core.domain.enums import ErrorSeverity, ProcessorPolicyCategory
from ananta.core.plugins.plugin_base import PluginBase
from ananta.error_handling import FrameworkError
from ananta.interfaces.edge_process_provider import EdgeProcessDefinition, EdgeProcessProvider

logger = logging.getLogger(__name__)

# Length validation constants for embedding descriptions
EMBEDDING_DESCRIPTION_MIN_LENGTH = 200
EMBEDDING_DESCRIPTION_MAX_LENGTH = 400


class PluginRegistrationValidator:
    """Validates plugin EDGE-process registration contracts at discovery time.

    Stateless — every method takes its inputs explicitly. Single instance
    is shared across the plugin scan loop.
    """

    def validate_edge_process_provider(
        self,
        plugin_name: str,
        plugin_instance: PluginBase,
        actions: list[ActionMetadata],
    ) -> None:
        """Validate that EdgeProcessProvider implementations are consistent.

        If a plugin implements EdgeProcessProvider, we validate decorated<->declared
        parity: every EDGE process declared via decorators is also declared in
        get_edge_process_definitions() and vice versa. Customizations and
        field_sensitivities are OPTIONAL (relaxed 2026-07-15, frontier-first
        consolidation — see
        workbench/2026-07-15_frontier_first_result_processing_consolidation.md).

        This fail-fast validation catches missing edge process definitions at startup,
        preventing silent failures at runtime.

        Args:
            plugin_name: Name of the plugin being validated
            plugin_instance: The plugin instance
            actions: List of ActionMetadata from the plugin's decorated methods

        Raises:
            FrameworkError: If validation fails
        """
        decorated_edge_processes = self._get_decorated_edge_processes(actions)

        if not isinstance(plugin_instance, EdgeProcessProvider):
            self._validate_non_provider_has_no_edge_processes(plugin_name, decorated_edge_processes)
            return

        edge_definitions = plugin_instance.get_edge_process_definitions()
        self._validate_edge_definitions_have_methods(
            plugin_name, edge_definitions, decorated_edge_processes
        )
        self._validate_edge_methods_have_definitions(
            plugin_name, plugin_instance, edge_definitions, decorated_edge_processes
        )

    def _get_decorated_edge_processes(
        self, actions: list[ActionMetadata]
    ) -> dict[str, ActionMetadata]:
        """Extract EDGE processes from decorated actions.

        Args:
            actions: List of ActionMetadata from the plugin's decorated methods

        Returns:
            Dictionary of action name to ActionMetadata for EDGE processes
        """
        return {
            a.name: a
            for a in actions
            if a.processor_policy_category == ProcessorPolicyCategory.EDGE
        }

    def _validate_non_provider_has_no_edge_processes(
        self, plugin_name: str, decorated_edge_processes: dict[str, ActionMetadata]
    ) -> None:
        """Validate that a non-EdgeProcessProvider plugin has no EDGE processes.

        Args:
            plugin_name: Name of the plugin being validated
            decorated_edge_processes: Dictionary of EDGE processes from decorators

        Raises:
            FrameworkError: If plugin has EDGE processes but doesn't implement EdgeProcessProvider
        """
        if not decorated_edge_processes:
            return

        edge_names = list(decorated_edge_processes.keys())
        raise FrameworkError(
            message=(
                f"Plugin '{plugin_name}' has EDGE processes {edge_names} but does not "
                f"implement EdgeProcessProvider. All plugins with EDGE processes MUST "
                f"implement EdgeProcessProvider to declare their customizations."
            ),
            error_code="process_registry.edge_process_missing_interface",
            details={
                "plugin_name": plugin_name,
                "edge_processes": edge_names,
            },
            severity=ErrorSeverity.CRITICAL,
        )

    def _validate_edge_definitions_have_methods(
        self,
        plugin_name: str,
        edge_definitions: dict[str, EdgeProcessDefinition],
        decorated_edge_processes: dict[str, ActionMetadata],
    ) -> None:
        """Validate that all declared edge definitions exist as decorated methods.

        Args:
            plugin_name: Name of the plugin being validated
            edge_definitions: Edge process definitions from get_edge_process_definitions()
            decorated_edge_processes: Dictionary of EDGE processes from decorators

        Raises:
            FrameworkError: If a definition doesn't have a matching decorated method
        """
        for def_name in edge_definitions:
            if def_name in decorated_edge_processes:
                continue

            raise FrameworkError(
                message=(
                    f"Plugin '{plugin_name}' EdgeProcessProvider declares edge process "
                    f"'{def_name}' but no @platform_process method with that name exists"
                ),
                error_code="process_registry.edge_process_mismatch",
                details={
                    "plugin_name": plugin_name,
                    "missing_method": def_name,
                    "declared_edge_processes": list(edge_definitions.keys()),
                    "decorated_edge_processes": list(decorated_edge_processes.keys()),
                },
                severity=ErrorSeverity.CRITICAL,
            )

    def _validate_edge_methods_have_definitions(
        self,
        plugin_name: str,
        plugin_instance: PluginBase,
        edge_definitions: dict[str, EdgeProcessDefinition],
        decorated_edge_processes: dict[str, ActionMetadata],
    ) -> None:
        """Validate that all decorated EDGE processes have definitions.

        Args:
            plugin_name: Name of the plugin being validated
            plugin_instance: The plugin instance (for disk-source AST fallback
                when the cached class disagrees with on-disk source).
            edge_definitions: Edge process definitions from get_edge_process_definitions()
            decorated_edge_processes: Dictionary of EDGE processes from decorators

        Raises:
            FrameworkError: If a decorated method doesn't have a matching definition
        """
        for action_name in decorated_edge_processes:
            if action_name not in edge_definitions:
                if _disk_source_declares_edge_definition(plugin_instance, action_name):
                    # Cache-poisoning false-positive: cached class lacks the
                    # entry but the disk source declares it. The cached state
                    # cannot be refreshed in-process (see manifest_preflight
                    # module docstring §"Cache-poisoning limit"); L2's
                    # sandboxed probe runs against a clean import and is the
                    # authoritative gate for this case. Log + defer.
                    logger.warning(
                        "Plugin '%s' EDGE process '%s' missing from cached "
                        "get_edge_process_definitions() but declared in disk "
                        "source — likely cache poisoning; deferring to L2 probe",
                        plugin_name,
                        action_name,
                    )
                    continue
                raise FrameworkError(
                    message=(
                        f"Plugin '{plugin_name}' has EDGE process '{action_name}' decorated with "
                        f"@platform_process but not declared in get_edge_process_definitions()"
                    ),
                    error_code="process_registry.edge_process_not_declared",
                    details={
                        "plugin_name": plugin_name,
                        "undeclared_edge_process": action_name,
                        "declared_edge_processes": list(edge_definitions.keys()),
                    },
                    severity=ErrorSeverity.CRITICAL,
                )

    def validate_all_embedding_descriptions(self, registry: dict[str, object]) -> None:
        """Validate embedding_description on all discoverable processes.

        Must run AFTER the knowledge-base merge so that
        embedding_description values from process JSON files are already
        present on the registry entries. Per-process findings are collected
        and logged as ONE aggregate WARNING per category (plus a DEBUG line
        with the full list) rather than one WARNING per process -- on a
        platform with dozens of pre-existing gaps, per-process logging repeats
        the same ~50 lines on every single boot forever, drowning out real
        signal. FrameworkError on a malformed value still raises immediately.
        """
        processes = registry["processes"]
        if not isinstance(processes, dict):
            return

        missing: list[str] = []
        out_of_range: list[str] = []
        for process_key, entry in processes.items():
            if not isinstance(entry, dict):
                continue
            provider_type = str(entry.get("provider_type", ""))
            provider = str(entry.get("provider", "unknown"))
            if provider_type == "plugin":
                source = f"plugin::{provider}"
            else:
                source = f"service_interface::{provider}"
            self._validate_discoverable_process(process_key, entry, source, missing, out_of_range)

        self._log_embedding_description_summary(missing, out_of_range)

    def _log_embedding_description_summary(self, missing: list[str], out_of_range: list[str]) -> None:
        """Log one aggregate WARNING per finding category, DEBUG for detail."""
        if missing:
            logger.warning(
                f"{len(missing)} discoverable process(es) missing embedding_description "
                "(will be required in a future release); see DEBUG log for the full list."
            )
            logger.debug("Missing embedding_description: %s", ", ".join(missing))
        if out_of_range:
            logger.warning(
                f"{len(out_of_range)} discoverable process(es) have an embedding_description "
                f"outside the recommended [{EMBEDDING_DESCRIPTION_MIN_LENGTH}, "
                f"{EMBEDDING_DESCRIPTION_MAX_LENGTH}] range; see DEBUG log for the full list."
            )
            logger.debug("Out-of-range embedding_description: %s", ", ".join(out_of_range))

    def _validate_discoverable_process(
        self,
        process_key: str,
        process_data: dict[str, object],
        source: str,
        missing: list[str],
        out_of_range: list[str],
    ) -> None:
        """Validate discoverable process has required embedding_description.

        Args:
            process_key: The process key being validated
            process_data: Process data dictionary
            source: Source identifier for error messages (plugin name or service interface)
            missing: Accumulator for processes with no embedding_description
            out_of_range: Accumulator for processes whose embedding_description length is out of range

        Raises:
            FrameworkError: If discoverable process has an invalid (non-string) embedding_description
        """
        is_discoverable = process_data.get("is_discoverable", True)
        if not is_discoverable:
            return

        embedding_desc = process_data.get("embedding_description", "")
        if not embedding_desc:
            # For now, accumulate instead of failing - enables incremental migration
            # TODO: After all processes have embedding_description, change to FrameworkError
            missing.append(f"{process_key} ({source})")
            return

        if not isinstance(embedding_desc, str):
            raise FrameworkError(
                message=f"Process '{process_key}' embedding_description must be a string",
                error_code="process_registry.invalid_embedding_description",
                details={"source": source, "process": process_key, "type": type(embedding_desc).__name__},
                severity=ErrorSeverity.CRITICAL,
            )

        embed_len = len(embedding_desc)
        if embed_len > 0 and not (EMBEDDING_DESCRIPTION_MIN_LENGTH <= embed_len <= EMBEDDING_DESCRIPTION_MAX_LENGTH):
            out_of_range.append(f"{process_key} ({source}), length {embed_len}")


def _resolve_plugin_source_path(plugin_instance: PluginBase) -> Path | None:
    """Return the on-disk path of the plugin's module, or None if absent."""
    module = sys.modules.get(plugin_instance.__class__.__module__)
    if module is None:
        return None
    module_file = getattr(module, "__file__", None)
    if not isinstance(module_file, str):
        return None
    source_path = Path(module_file)
    return source_path if source_path.is_file() else None


def _parse_python_source(source_path: Path) -> ast.Module | None:
    """Parse a Python source file, returning None on any read/parse failure."""
    try:
        return ast.parse(source_path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError, UnicodeDecodeError):
        return None


def _find_class_def_node(tree: ast.Module, class_name: str) -> ast.ClassDef | None:
    """Walk ``tree`` for the first ``class <class_name>`` definition."""
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            return node
    return None


def _find_method_def_node(
    class_node: ast.ClassDef, method_name: str
) -> ast.FunctionDef | None:
    """Find a direct-body method node by name on ``class_node``."""
    for body_item in class_node.body:
        if isinstance(body_item, ast.FunctionDef) and body_item.name == method_name:
            return body_item
    return None


def _find_dict_literal_return(method: ast.FunctionDef) -> ast.Dict | None:
    """Return the first ``return <dict-literal>`` value inside ``method``."""
    for sub in ast.walk(method):
        if isinstance(sub, ast.Return) and isinstance(sub.value, ast.Dict):
            return sub.value
    return None


def _extract_string_dict_keys(dict_node: ast.Dict) -> set[str]:
    """Pull string-constant keys from a dict literal; non-string keys ignored."""
    return {
        key_node.value
        for key_node in dict_node.keys
        if isinstance(key_node, ast.Constant) and isinstance(key_node.value, str)
    }


def _disk_source_declares_edge_definition(
    plugin_instance: PluginBase, action_name: str
) -> bool:
    """AST-scan the plugin's disk source for an edge-process declaration.

    The L1.2 validator reads ``get_edge_process_definitions()`` from the
    already-cached plugin class (``importlib.import_module`` returns the
    in-process module). When the on-disk source is AHEAD of the cached
    class — the dominant case during ``apply_manifest``-driven code
    pickup — the validator's mismatch verdict can be a false positive
    that prevents the very restart meant to bring the new code in.

    This helper resolves the plugin's source file from
    ``plugin_instance.__class__.__module__`` and AST-walks it for the
    plugin class's ``get_edge_process_definitions`` method. If the
    method's ``return`` statement is a dict literal whose keys include
    ``action_name`` as a string constant, the disk source declares it.

    Conservative semantics: any AST surprise (no ``__file__``, can't
    parse, class not found, method not found, return value not a dict
    literal) returns ``False`` — the caller falls back to the strict
    rejection. Plugins that construct their edge-definitions dict
    dynamically opt out of this path and get the strict behaviour;
    that's intentional, since the dict-literal pattern is the dominant
    platform convention.

    L2's probe (``probe_runner.py``) runs a clean-import subprocess and
    remains the authoritative gate for the cache-poisoning case; this
    helper just keeps L1.2 from blocking the restart before L2 sees it.
    """
    source_path = _resolve_plugin_source_path(plugin_instance)
    if source_path is None:
        return False
    tree = _parse_python_source(source_path)
    if tree is None:
        return False
    class_node = _find_class_def_node(tree, plugin_instance.__class__.__name__)
    if class_node is None:
        return False
    method_node = _find_method_def_node(class_node, "get_edge_process_definitions")
    if method_node is None:
        return False
    dict_node = _find_dict_literal_return(method_node)
    if dict_node is None:
        return False
    return action_name in _extract_string_dict_keys(dict_node)
