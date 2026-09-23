"""Scoped component, runtime, topology, config, and route facts."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import PurePosixPath

from ._technology_fingerprint_probes import (
    _COMPONENT_BY_PACKAGE,
    _DEPENDENCY_RELATIONSHIPS,
    _ROUTE_SUFFIXES,
    MAX_BOUNDED_EXAMPLES,
    _ReadResult,
    evidence,
    join_scope,
    json_pointer_part,
    scope_for_path,
)
from .source_roles import RoleOverride, SourceRole, confirmed_role_for_path
from .targets import TargetTree


def _role_evidence(path: str, overrides: Sequence[RoleOverride]) -> dict[str, object]:
    role = confirmed_role_for_path(path, overrides)
    return {
        "status": "confirmed" if role is not None else "unconfirmed",
        "role": None if role is None else role.value,
        "product_claim_support": role is SourceRole.PRODUCT,
        "finding_support": role is SourceRole.PRODUCT,
        "routing_eligible": role is None or role is SourceRole.PRODUCT,
    }


def _resolved_version(
    package_name: str,
    lock_result: _ReadResult | None,
) -> tuple[dict[str, object], list[dict[str, str]]]:
    if lock_result is None or lock_result.data is None:
        return {"status": "unknown", "value": None, "reason": "co_scoped_lock_unavailable"}, []
    packages = lock_result.data.get("packages")
    if not isinstance(packages, Mapping):
        raise RuntimeError("validated package-lock lost its /packages object")
    lock_key = f"node_modules/{package_name}"
    record = packages.get(lock_key)
    if not isinstance(record, Mapping):
        return {"status": "unknown", "value": None, "reason": "lock_entry_not_present"}, []
    version = record.get("version")
    if not isinstance(version, str):
        return {"status": "unknown", "value": None, "reason": "lock_version_not_present"}, []
    pointer = f"/packages/{json_pointer_part(lock_key)}/version"
    return (
        {"status": "resolved", "value": version, "reason": None},
        [evidence(lock_result.path, pointer, "resolved_version")],
    )


def _manifest_declarations(
    data: Mapping[str, object],
) -> list[tuple[str, str, str, str]]:
    declarations: list[tuple[str, str, str, str]] = []
    for table, relationship in _DEPENDENCY_RELATIONSHIPS:
        raw = data.get(table)
        if raw is None:
            continue
        if not isinstance(raw, Mapping):
            raise RuntimeError("validated package.json dependency table changed shape")
        for package_name, declared_version in sorted(raw.items()):
            component_key = _COMPONENT_BY_PACKAGE.get(str(package_name))
            if component_key is not None:
                declarations.append((table, relationship, str(package_name), str(declared_version)))
    return declarations


def _direct_component_fact(
    *,
    path: str,
    scope: str,
    table: str,
    relationship: str,
    package_name: str,
    declared_version: str,
    lock_result: _ReadResult | None,
    overrides: Sequence[RoleOverride],
) -> dict[str, object]:
    component_key = _COMPONENT_BY_PACKAGE[package_name]
    resolved, lock_evidence = _resolved_version(package_name, lock_result)
    pointer = f"/{table}/{json_pointer_part(package_name)}"
    return {
        "component_key": component_key,
        "package_name": package_name,
        "scope": scope,
        "relationship": relationship,
        "status": "direct_declared",
        "declared_version": {"status": "declared", "value": declared_version},
        "resolved_version": resolved,
        "source_usage_status": "unconfirmed",
        "source_role": _role_evidence(path, overrides),
        "evidence": [
            evidence(path, pointer, "direct_declaration"),
            *lock_evidence,
        ],
    }


def _direct_facts_for_manifest(
    path: str,
    result: _ReadResult,
    lock_result: _ReadResult | None,
    overrides: Sequence[RoleOverride],
) -> list[dict[str, object]]:
    if result.data is None:
        raise RuntimeError("matched package.json has no parsed object")
    scope = scope_for_path(path)
    return [
        _direct_component_fact(
            path=path,
            scope=scope,
            table=table,
            relationship=relationship,
            package_name=package_name,
            declared_version=declared_version,
            lock_result=lock_result,
            overrides=overrides,
        )
        for table, relationship, package_name, declared_version in _manifest_declarations(
            result.data
        )
    ]


def _lock_only_component_fact(
    *,
    path: str,
    scope: str,
    packages: Mapping[str, object],
    package_name: str,
    component_key: str,
    direct_keys: set[str],
    overrides: Sequence[RoleOverride],
) -> dict[str, object] | None:
    if component_key in direct_keys:
        return None
    lock_key = f"node_modules/{package_name}"
    record = packages.get(lock_key)
    if not isinstance(record, Mapping):
        return None
    version = record.get("version")
    if not isinstance(version, str):
        return None
    return {
        "component_key": component_key,
        "package_name": package_name,
        "scope": scope,
        "relationship": "transitive_or_lock_only",
        "status": "lockfile_only",
        "declared_version": {"status": "not_declared", "value": None},
        "resolved_version": {
            "status": "resolved",
            "value": version,
            "reason": None,
        },
        "source_usage_status": "unconfirmed",
        "source_role": _role_evidence(path, overrides),
        "evidence": [
            evidence(
                path,
                f"/packages/{json_pointer_part(lock_key)}/version",
                "lockfile_only_resolution",
            )
        ],
    }


def _lock_only_facts_for_lock(
    path: str,
    result: _ReadResult,
    direct_keys: set[str],
    overrides: Sequence[RoleOverride],
) -> list[dict[str, object]]:
    if result.data is None:
        raise RuntimeError("matched package-lock has no parsed object")
    packages = result.data.get("packages")
    if not isinstance(packages, Mapping):
        raise RuntimeError("validated package-lock lost its /packages object")
    scope = scope_for_path(path)
    candidates = (
        _lock_only_component_fact(
            path=path,
            scope=scope,
            packages=packages,
            package_name=package_name,
            component_key=component_key,
            direct_keys=direct_keys,
            overrides=overrides,
        )
        for package_name, component_key in sorted(
            _COMPONENT_BY_PACKAGE.items(), key=lambda item: item[1]
        )
    )
    return [candidate for candidate in candidates if candidate is not None]


def component_facts(
    successful: Mapping[str, Mapping[str, _ReadResult]],
    overrides: Sequence[RoleOverride],
) -> list[dict[str, object]]:
    manifests = successful["package_json"]
    locks = successful["package_lock"]
    locks_by_scope = {scope_for_path(path): result for path, result in locks.items()}
    facts: list[dict[str, object]] = []
    direct_keys_by_scope: dict[str, set[str]] = {}

    for path, result in sorted(manifests.items()):
        scope = scope_for_path(path)
        direct = _direct_facts_for_manifest(path, result, locks_by_scope.get(scope), overrides)
        facts.extend(direct)
        direct_keys_by_scope[scope] = {str(fact["component_key"]) for fact in direct}

    for path, result in sorted(locks.items()):
        scope = scope_for_path(path)
        facts.extend(
            _lock_only_facts_for_lock(
                path,
                result,
                direct_keys_by_scope.get(scope, set()),
                overrides,
            )
        )
    facts.sort(
        key=lambda fact: (
            str(fact["scope"]),
            str(fact["component_key"]),
            str(fact["relationship"]),
            str(fact["package_name"]),
        )
    )
    return facts


def _bounded_paths(paths: Sequence[str]) -> dict[str, object]:
    ordered = sorted(paths)
    shown = ordered[:MAX_BOUNDED_EXAMPLES]
    return {
        "count": len(ordered),
        "examples": shown,
        "omitted": len(ordered) - len(shown),
    }


def _group_paths_by_role(
    paths: Sequence[str], overrides: Sequence[RoleOverride]
) -> dict[str, dict[str, object]]:
    groups: dict[str, list[str]] = {
        "product": [],
        "non_product": [],
        "unconfirmed": [],
    }
    for path in sorted(paths):
        role = confirmed_role_for_path(path, overrides)
        if role is SourceRole.PRODUCT:
            bucket = "product"
        elif role is None:
            bucket = "unconfirmed"
        else:
            bucket = "non_product"
        groups[bucket].append(path)
    return {name: _bounded_paths(groups[name]) for name in groups}


def deno_runtime_scopes(
    successful: Mapping[str, Mapping[str, _ReadResult]],
    overrides: Sequence[RoleOverride],
) -> list[dict[str, object]]:
    manifests = successful["deno_manifest"]
    locks = successful["deno_lock"]
    manifest_by_scope = _paths_by_scope(manifests)
    lock_by_scope = _paths_by_scope(locks)
    scopes = sorted(set(manifest_by_scope) | set(lock_by_scope))
    facts: list[dict[str, object]] = []
    for scope in scopes:
        manifest_paths = manifest_by_scope.get(scope, [])
        lock_paths = lock_by_scope.get(scope, [])
        primary = manifest_paths[0] if manifest_paths else lock_paths[0]
        facts.append(
            {
                "scope": scope,
                "status": "runtime_declared" if manifest_paths else "lockfile_only",
                "manifest_evidence": [
                    evidence(path, "/", "deno_runtime_manifest") for path in manifest_paths
                ],
                "lock_evidence": [evidence(path, "/", "deno_runtime_lock") for path in lock_paths],
                "source_role": _role_evidence(primary, overrides),
                "source_usage_status": "unconfirmed",
            }
        )
    return facts


def _paths_by_scope(paths: Mapping[str, object]) -> dict[str, list[str]]:
    grouped: dict[str, list[str]] = {}
    for path in sorted(paths):
        grouped.setdefault(scope_for_path(path), []).append(path)
    return grouped


def _supabase_scope(path: str) -> str:
    marker = "/supabase/"
    if path.startswith("supabase/"):
        return "."
    if marker not in path:
        raise RuntimeError(f"supabase probe produced a non-supabase path: {path!r}")
    prefix, _rest = path.split(marker, 1)
    return prefix


def supabase_topology(
    successful: Mapping[str, Mapping[str, _ReadResult]],
    overrides: Sequence[RoleOverride],
) -> list[dict[str, object]]:
    configs = tuple(successful["supabase_config"])
    functions = tuple(successful["supabase_functions"])
    migrations = tuple(successful["supabase_migrations"])
    config_by_scope = _supabase_paths_by_scope(configs)
    function_by_scope = _supabase_paths_by_scope(functions)
    migration_by_scope = _supabase_paths_by_scope(migrations)
    scopes = sorted(set(config_by_scope) | set(function_by_scope) | set(migration_by_scope))
    return [
        _supabase_topology_fact(
            scope,
            config_by_scope.get(scope, []),
            function_by_scope.get(scope, []),
            migration_by_scope.get(scope, []),
            overrides,
        )
        for scope in scopes
    ]


def _supabase_paths_by_scope(paths: Sequence[str]) -> dict[str, list[str]]:
    grouped: dict[str, list[str]] = {}
    for path in sorted(paths):
        grouped.setdefault(_supabase_scope(path), []).append(path)
    return grouped


def _supabase_topology_fact(
    scope: str,
    configs: Sequence[str],
    functions: Sequence[str],
    migrations: Sequence[str],
    overrides: Sequence[RoleOverride],
) -> dict[str, object]:
    all_paths = [*configs, *functions, *migrations]
    return {
        "scope": scope,
        "config": _group_paths_by_role(configs, overrides),
        "edge_functions": _group_paths_by_role(functions, overrides),
        "migrations": _group_paths_by_role(migrations, overrides),
        "evidence": [evidence(path, "$file", "supabase_topology_presence") for path in all_paths],
        "routing_eligible": any(
            _role_evidence(path, overrides)["routing_eligible"] for path in all_paths
        ),
        "source_usage_status": "unconfirmed",
    }


def config_facts(
    successful: Mapping[str, Mapping[str, _ReadResult]],
    overrides: Sequence[RoleOverride],
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    modeled = _embedded_package_configs(successful["package_json"], overrides)
    for probe in (
        "expo_app_json",
        "expo_eas_json",
        "tsconfig_json",
        "eslint_config",
        "jest_config",
    ):
        for path in sorted(successful[probe]):
            modeled.append(
                {
                    "config_key": probe,
                    "scope": scope_for_path(path),
                    "path": path,
                    "status": "present",
                    "source_role": _role_evidence(path, overrides),
                    "evidence": evidence(path, "/", "config_presence"),
                }
            )
    unmodeled: list[dict[str, object]] = []
    for path in sorted(successful["recognized_unmodeled_config"]):
        unmodeled.append(
            {
                "config_key": PurePosixPath(path).name,
                "scope": scope_for_path(path),
                "path": path,
                "status": "presence_only_unmodeled",
                "execution_status": "not_executed",
                "source_role": _role_evidence(path, overrides),
                "evidence": evidence(path, "$file", "recognized_config_presence"),
            }
        )
    return modeled, unmodeled


def _embedded_package_configs(
    manifests: Mapping[str, _ReadResult],
    overrides: Sequence[RoleOverride],
) -> list[dict[str, object]]:
    config_keys = (("eslintConfig", "eslint_package_json"), ("jest", "jest_package_json"))
    facts: list[dict[str, object]] = []
    for path, result in sorted(manifests.items()):
        if result.data is None:
            raise RuntimeError("matched package.json has no parsed object")
        for source_key, config_key in config_keys:
            if source_key not in result.data:
                continue
            facts.append(
                {
                    "config_key": config_key,
                    "scope": scope_for_path(path),
                    "path": path,
                    "status": "present",
                    "source_role": _role_evidence(path, overrides),
                    "evidence": evidence(
                        path,
                        f"/{json_pointer_part(source_key)}",
                        "embedded_config_presence",
                    ),
                }
            )
    return facts


def route_inventories(
    tree: TargetTree,
    components: Sequence[Mapping[str, object]],
    overrides: Sequence[RoleOverride],
) -> list[dict[str, object]]:
    scopes = sorted(
        {
            str(component["scope"])
            for component in components
            if component.get("component_key") == "expo_router"
            and component.get("status") == "direct_declared"
        }
    )
    inventories: list[dict[str, object]] = []
    tracked = sorted(tree.all_files())
    for scope in scopes:
        roots = (join_scope(scope, "app"), join_scope(scope, "src/app"))
        routes = [
            path
            for path in tracked
            if PurePosixPath(path).suffix.lower() in _ROUTE_SUFFIXES
            and any(path.startswith(f"{root}/") for root in roots)
        ]
        inventories.append(
            {
                "scope": scope,
                "declared_by_component": "expo_router",
                "roots": list(roots),
                "routes": _group_paths_by_role(routes, overrides),
                "content_read": False,
                "evidence": [
                    evidence(path, "$file", "route_path_presence")
                    for path in routes[:MAX_BOUNDED_EXAMPLES]
                ],
                "evidence_omitted": max(0, len(routes) - MAX_BOUNDED_EXAMPLES),
            }
        )
    return inventories
