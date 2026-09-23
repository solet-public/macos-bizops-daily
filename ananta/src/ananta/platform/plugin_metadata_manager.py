import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import TypedDict

logger = logging.getLogger(__name__)


class MissingDependencyInfo(TypedDict):
    plugin: str
    missing_requirement: str


class ValidationResult(TypedDict):
    valid: bool
    errors: list[str]
    warnings: list[str]
    circular_dependencies: list[list[str]]
    missing_dependencies: list[MissingDependencyInfo]


class SummaryResult(TypedDict):
    plugins_discovered: int
    plugins_with_schemas: int
    plugins_with_actions: int
    circular_dependencies: int
    initialized: bool


@dataclass
class PluginPackage:
    plugin_id: str
    version: str | None
    metadata_path: Path
    database_schema: dict[str, object] | None = None
    capabilities: list[str] = field(default_factory=list)
    requirements: list[str] = field(default_factory=list)
    actions: dict[str, dict[str, object]] = field(default_factory=dict)


@dataclass
class DependencyGraph:
    nodes: set[str]
    edges: dict[str, set[str]]
    circular_dependencies: list[list[str]]


class PluginMetadataManager:
    def __init__(self, plugins_root_path: str):
        self.plugins_root = Path(plugins_root_path)
        self._discovered_plugins: dict[str, PluginPackage] = {}
        self._dependency_graph: DependencyGraph | None = None
        self._initialized = False

    def initialize(self) -> bool:
        if self._initialized:
            return True

        try:
            if not self.plugins_root.exists():
                return False

            self._discover_plugins()
            self._build_dependency_graph()

            self._initialized = True
            return True

        except Exception:
            logger.exception(
                "Plugin metadata initialization failed for plugin_id=<metadata-registry> root=%s",
                self.plugins_root,
            )
            return False

    def _discover_plugins(self) -> None:
        for plugin_dir in self.plugins_root.iterdir():
            if not plugin_dir.is_dir():
                continue

            if plugin_dir.name.startswith("."):
                continue

            metadata_dir = plugin_dir / "metadata"
            if not metadata_dir.exists():
                continue

            try:
                plugin_package = self._load_plugin_metadata(plugin_dir.name, metadata_dir)
                if plugin_package:
                    self._discovered_plugins[plugin_package.plugin_id] = plugin_package

            except Exception:
                logger.exception(
                    "Plugin metadata discovery failed for plugin_id=%s", plugin_dir.name
                )

    def _load_plugin_metadata(self, plugin_name: str, metadata_dir: Path) -> PluginPackage | None:
        schema_file = metadata_dir / "schema.json"

        if not schema_file.exists():
            return None

        try:
            with open(schema_file, encoding="utf-8") as f:
                schema_data = json.load(f)

            plugin_package = PluginPackage(
                plugin_id=schema_data.get("plugin_id", plugin_name),
                version=(
                    schema_data["version"] if isinstance(schema_data.get("version"), str) else None
                ),
                metadata_path=metadata_dir,
                database_schema=schema_data.get("database_schema"),
                capabilities=schema_data.get("dependencies", {}).get("provides", []),
                requirements=schema_data.get("dependencies", {}).get("requires", []),
            )

            self._load_plugin_actions(plugin_package, metadata_dir)

            return plugin_package

        except Exception:
            logger.exception("Plugin metadata parsing failed for plugin_id=%s", plugin_name)
            return None

    def _load_plugin_actions(self, plugin_package: PluginPackage, metadata_dir: Path) -> None:
        actions_dir = metadata_dir / "actions"

        if not actions_dir.exists():
            return

        for action_file in actions_dir.glob("*.json"):
            try:
                with open(action_file, encoding="utf-8") as f:
                    action_data = json.load(f)

                action_name = action_file.stem
                plugin_package.actions[action_name] = action_data

            except Exception:
                logger.exception(
                    "Plugin action metadata parsing failed for plugin_id=%s action=%s",
                    plugin_package.plugin_id,
                    action_file.name,
                )

    def _build_dependency_graph(self) -> None:
        nodes = set(self._discovered_plugins.keys())
        edges: dict[str, set[str]] = {}

        for plugin_id, plugin_package in self._discovered_plugins.items():
            edges[plugin_id] = set()

            for requirement in plugin_package.requirements:
                if requirement in nodes:
                    edges[plugin_id].add(requirement)
                else:
                    pass

        circular_deps = self._detect_circular_dependencies(nodes, edges)

        self._dependency_graph = DependencyGraph(
            nodes=nodes,
            edges=edges,
            circular_dependencies=circular_deps,
        )

    def _detect_circular_dependencies(
        self, nodes: set[str], edges: dict[str, set[str]]
    ) -> list[list[str]]:
        visited = set()
        rec_stack = set()
        cycles = []

        def dfs(node: str, path: list[str]) -> None:
            visited.add(node)
            rec_stack.add(node)
            path.append(node)

            for dependency in edges[node]:
                if dependency not in visited:
                    dfs(dependency, path.copy())
                elif dependency in rec_stack:
                    cycle_start = path.index(dependency)
                    cycle = path[cycle_start:] + [dependency]
                    cycles.append(cycle)

            rec_stack.remove(node)

        for node in nodes:
            if node not in visited:
                dfs(node, [])

        return cycles

    def get_discovered_plugins(self) -> dict[str, PluginPackage]:
        if not self._initialized:
            self.initialize()
        return self._discovered_plugins.copy()

    def get_plugin(self, plugin_id: str) -> PluginPackage | None:
        if not self._initialized:
            self.initialize()
        return self._discovered_plugins.get(plugin_id)

    def validate_plugin_dependencies(self) -> ValidationResult:
        if not self._initialized:
            self.initialize()

        validation_result: ValidationResult = {
            "valid": True,
            "errors": [],
            "warnings": [],
            "circular_dependencies": [],
            "missing_dependencies": [],
        }

        if self._dependency_graph and self._dependency_graph.circular_dependencies:
            validation_result["valid"] = False
            validation_result["circular_dependencies"] = (
                self._dependency_graph.circular_dependencies
            )
            validation_result["errors"].append("Circular dependencies detected")

        for plugin_id, plugin_package in self._discovered_plugins.items():
            for requirement in plugin_package.requirements:
                if requirement not in self._discovered_plugins:
                    validation_result["missing_dependencies"].append(
                        {"plugin": plugin_id, "missing_requirement": requirement}
                    )
                    message = f"Plugin {plugin_id} requires missing plugin {requirement}"
                    validation_result["valid"] = False
                    validation_result["errors"].append(message)
                    validation_result["warnings"].append(message)

        return validation_result

    def get_plugins_providing_capability(self, capability: str) -> list[PluginPackage]:
        if not self._initialized:
            self.initialize()

        matching_plugins = []
        for plugin_package in self._discovered_plugins.values():
            if capability in plugin_package.capabilities:
                matching_plugins.append(plugin_package)

        return matching_plugins

    def get_summary(self) -> SummaryResult:
        if not self._initialized:
            self.initialize()

        summary_result: SummaryResult = {
            "plugins_discovered": len(self._discovered_plugins),
            "plugins_with_schemas": len(
                [p for p in self._discovered_plugins.values() if p.database_schema]
            ),
            "plugins_with_actions": len(
                [p for p in self._discovered_plugins.values() if p.actions]
            ),
            "circular_dependencies": (
                len(self._dependency_graph.circular_dependencies) if self._dependency_graph else 0
            ),
            "initialized": self._initialized,
        }
        return summary_result
