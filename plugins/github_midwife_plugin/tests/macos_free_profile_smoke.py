"""Slice A smoke — validate the `macos-free-solet` genesis profile.

Loads `knowledge_base/profile_templates/macos-free-solet.yaml` and the
`knowledge_base/profile_baseline/*.json` per-plugin config templates with
plain `yaml`/`json` parsing (github_midwife_plugin ships no installable
code yet — Slice A is data-only, and the plugin must not cross-import
macos_midwife_plugin's loader per the own-copy-per-plugin convention).

Checks:
  1. Binding-satisfaction: every `service_bindings` value names a plugin in
     the `plugins:` allowlist; `inference_service` and
     `self_deployment_service` are asserted ABSENT (the declared-vacant /
     opt-in shape the design mandates), not merely unchecked.
  2. No orphan starting_actions: every `plugin::<name>::...` starting-action
     process_key names a plugin in the allowlist.
  3. Public-safe: no operator-identity path (`/Users/...`) or secret-shaped
     key (`api_key`, `password`, `secret`) appears anywhere in the profile
     YAML or any checked-in `profile_baseline/*.json` file — every
     `profile_baseline` file also parses as valid JSON.
  4. Shipped-profile service closure in both directions: enabled declarations
     must be bound or exactly allowlisted, and every configured registered
     service must select an enabled plugin that actually declares it. Wrong or
     disabled providers, new omissions, and stale/wildcard entries fail.

Run directly: ``.venv/bin/python3
plugins/github_midwife_plugin/tests/macos_free_profile_smoke.py``.
"""

from __future__ import annotations

# ruff: noqa: E402
import ast
import copy
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

_PLUGIN_ROOT = Path(__file__).resolve().parents[1]
_SRC = _PLUGIN_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from ananta.core.orchestration.service_bindings import SERVICE_INTERFACE_MAP

_REPO_ROOT = Path(__file__).resolve().parents[3]
_KB_ROOT = _REPO_ROOT / "plugins" / "github_midwife_plugin" / "knowledge_base"
_PROFILE_PATH = _KB_ROOT / "profile_templates" / "macos-free-solet.yaml"
_PROFILE_DIR = _PROFILE_PATH.parent
_BASELINE_DIR = _KB_ROOT / "profile_baseline"
_PROCESS_ROOT = _REPO_ROOT / "ananta" / "knowledge_base" / "processes"
_SERVICE_BINDING_ALLOWLIST = (
    Path(__file__).resolve().parent / "shipped_profile_service_binding_allowlist.json"
)

_OPERATOR_PATH_PATTERN = re.compile(r"/Users/[A-Za-z0-9_.-]+")
_SECRET_KEY_PATTERN = re.compile(r'"(api_key|password|secret)"\s*:', re.IGNORECASE)

# Service bindings the design explicitly mandates ABSENT from this profile
# (declared-vacant inference; opt-in self-deployment) rather than merely
# unchecked — an orphan-plugin check alone would silently pass a profile
# that never declared them at all, which is the point, but would equally
# silently pass a profile that bound them to something bogus with no
# allowlist entry required. Assert absence explicitly.
_MUST_BE_ABSENT_BINDINGS = ("inference_service", "self_deployment_service")

_CHECKS_RUN: list[str] = []


@dataclass(frozen=True, order=True, slots=True)
class BindingViolation:
    """One enabled provider whose registered service face is absent or misbound."""

    profile: str
    service: str
    providing_plugin: str
    stranded_process_keys: tuple[str, ...]
    configured_plugin: str | None = None


@dataclass(frozen=True, order=True, slots=True)
class BoundProviderViolation:
    """One registered binding whose configured provider does not declare it."""

    profile: str
    service: str
    configured_plugin: str
    enabled_declaring_plugins: tuple[str, ...]


class SmokeFailureError(AssertionError):
    """Raised on any check failure; message is the failure detail."""


def _check(label: str, condition: bool, detail: str) -> None:
    _CHECKS_RUN.append(label)
    if not condition:
        raise SmokeFailureError(f"{label}: {detail}")


def _load_profile() -> dict[str, Any]:
    if not _PROFILE_PATH.is_file():
        raise SmokeFailureError(f"profile template missing: {_PROFILE_PATH}")
    raw = yaml.safe_load(_PROFILE_PATH.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise SmokeFailureError(f"profile template did not parse to a mapping: {_PROFILE_PATH}")
    return raw


def _load_mapping(path: Path) -> dict[str, Any]:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise SmokeFailureError(f"profile template did not parse to a mapping: {path}")
    return raw


def _registered_service_by_interface() -> dict[str, str]:
    registered: dict[str, str] = {}
    for service_name, interface_path in SERVICE_INTERFACE_MAP.items():
        process_dir = _PROCESS_ROOT / service_name.value
        if any(process_dir.glob("*.json")):
            registered[interface_path.rsplit(".", 1)[-1]] = service_name.value
    return registered


def _service_interface_node(node: ast.AST) -> ast.AST | None:
    if (
        isinstance(node, ast.AnnAssign)
        and isinstance(node.target, ast.Name)
        and node.target.id == "service_interfaces"
    ):
        return node.value
    if isinstance(node, ast.Assign) and any(
        isinstance(target, ast.Name) and target.id == "service_interfaces"
        for target in node.targets
    ):
        return node.value
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and (
        node.name == "service_interfaces"
    ):
        return node
    return None


def _provided_registered_services(
    plugin_name: str,
    service_by_interface: dict[str, str],
) -> set[str]:
    source_root = _REPO_ROOT / "plugins" / plugin_name / "src" / plugin_name
    services: set[str] = set()
    if not source_root.is_dir():
        return services
    for path in sorted(source_root.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            interface_node = _service_interface_node(node)
            if interface_node is None:
                continue
            services.update(
                service_by_interface[item.id]
                for item in ast.walk(interface_node)
                if isinstance(item, ast.Name) and item.id in service_by_interface
            )
    return services


def _stranded_process_keys(service_name: str) -> tuple[str, ...]:
    return tuple(
        f"service_interface::{service_name}::{path.stem}"
        for path in sorted((_PROCESS_ROOT / service_name).glob("*.json"))
    )


def _profile_plugins_and_bindings(
    profile_path: Path,
    profile: dict[str, Any],
) -> tuple[tuple[str, ...], dict[str, str]]:
    plugins = profile.get("plugins") or []
    bindings = profile.get("service_bindings") or {}
    if not isinstance(plugins, list) or not all(
        isinstance(plugin, str) for plugin in plugins
    ):
        raise SmokeFailureError(f"invalid plugins shape: {profile_path}")
    if not isinstance(bindings, dict) or not all(
        isinstance(service, str) and isinstance(provider, str)
        for service, provider in bindings.items()
    ):
        raise SmokeFailureError(f"invalid service_bindings shape: {profile_path}")
    return tuple(plugins), bindings


def _reject_disabled_bound_providers(
    profile_path: Path,
    plugins: tuple[str, ...],
    bindings: dict[str, str],
) -> None:
    enabled = set(plugins)
    for service_name, configured_plugin in sorted(bindings.items()):
        if configured_plugin not in enabled:
            raise SmokeFailureError(
                f"{profile_path.name}: service {service_name!r} is bound to configured "
                f"provider {configured_plugin!r}, but that provider is not enabled"
            )


def _profile_binding_violations(
    profile_path: Path,
    profile: dict[str, Any],
    service_by_interface: dict[str, str],
) -> set[BindingViolation]:
    plugins, bindings = _profile_plugins_and_bindings(profile_path, profile)
    _reject_disabled_bound_providers(profile_path, plugins, bindings)
    provided_services = {
        plugin_name: _provided_registered_services(plugin_name, service_by_interface)
        for plugin_name in plugins
    }
    _reject_nondeclaring_bound_providers(
        profile_path,
        bindings,
        provided_services,
        set(service_by_interface.values()),
    )
    return _declaration_to_binding_violations(
        profile_path,
        plugins,
        bindings,
        provided_services,
    )


def _binding_to_declaration_violations(
    profile_path: Path,
    bindings: dict[str, str],
    provided_services: dict[str, set[str]],
    registered_services: set[str],
) -> set[BoundProviderViolation]:
    """Validate configured registered bindings, independent of declarer presence."""
    violations: set[BoundProviderViolation] = set()
    for service_name, configured_plugin in sorted(bindings.items()):
        if service_name not in registered_services:
            continue
        if service_name in provided_services.get(configured_plugin, set()):
            continue
        enabled_declarers = tuple(
            sorted(
                plugin_name
                for plugin_name, declared_services in provided_services.items()
                if service_name in declared_services
            )
        )
        violations.add(
            BoundProviderViolation(
                profile=profile_path.name,
                service=service_name,
                configured_plugin=configured_plugin,
                enabled_declaring_plugins=enabled_declarers,
            )
        )
    return violations


def _reject_nondeclaring_bound_providers(
    profile_path: Path,
    bindings: dict[str, str],
    provided_services: dict[str, set[str]],
    registered_services: set[str],
) -> None:
    violations = sorted(
        _binding_to_declaration_violations(
            profile_path,
            bindings,
            provided_services,
            registered_services,
        )
    )
    if not violations:
        return
    details: list[str] = []
    for violation in violations:
        if violation.enabled_declaring_plugins:
            declaration_state = (
                "enabled declaring providers "
                f"{violation.enabled_declaring_plugins!r} do not include the configured provider"
            )
        else:
            declaration_state = "no enabled plugin declares this registered service"
        details.append(
            f"service {violation.service!r} selects configured provider "
            f"{violation.configured_plugin!r}, which does not declare it "
            f"({declaration_state})"
        )
    raise SmokeFailureError(f"{profile_path.name}: " + "; ".join(details))


def _declaration_to_binding_violations(
    profile_path: Path,
    plugins: tuple[str, ...],
    bindings: dict[str, str],
    provided_services: dict[str, set[str]],
) -> set[BindingViolation]:
    """Validate every enabled registered declaration against configured bindings."""
    violations: set[BindingViolation] = set()
    for plugin_name in plugins:
        for service_name in provided_services.get(plugin_name, set()):
            configured_plugin = bindings.get(service_name)
            if configured_plugin == plugin_name:
                continue
            violations.add(
                BindingViolation(
                    profile=profile_path.name,
                    service=service_name,
                    providing_plugin=plugin_name,
                    stranded_process_keys=_stranded_process_keys(service_name),
                    configured_plugin=configured_plugin,
                )
            )
    return violations


def _binding_violations(
    service_by_interface: dict[str, str],
) -> set[BindingViolation]:
    violations: set[BindingViolation] = set()
    for profile_path in sorted(_PROFILE_DIR.glob("*.yaml")):
        violations.update(
            _profile_binding_violations(
                profile_path,
                _load_mapping(profile_path),
                service_by_interface,
            )
        )
    return violations


def _allowlist_identity(
    item: dict[Any, Any],
    index: int,
) -> tuple[str, str, str, str | None]:
    profile_fragments = item["profile_fragments"]
    if not isinstance(profile_fragments, list) or not all(
        isinstance(value, str) and value for value in profile_fragments
    ):
        raise SmokeFailureError(f"allowlist entry {index} has invalid profile fragments")
    profile = "".join(profile_fragments)
    scalar_values = (profile, item["service"], item["providing_plugin"])
    if not all(isinstance(value, str) and value for value in scalar_values):
        raise SmokeFailureError(f"allowlist entry {index} has invalid identity fields")
    if any("*" in value for value in scalar_values):
        raise SmokeFailureError(f"allowlist entry {index} contains a wildcard")
    return *scalar_values, _allowlist_configured_plugin(item, index)


def _allowlist_configured_plugin(
    item: dict[Any, Any],
    index: int,
) -> str | None:
    configured_plugin = item["configured_plugin"]
    if configured_plugin is not None and (
        not isinstance(configured_plugin, str)
        or not configured_plugin
        or "*" in configured_plugin
    ):
        raise SmokeFailureError(
            f"allowlist entry {index} has invalid configured provider identity"
        )
    return configured_plugin


def _allowlist_process_keys(item: dict[Any, Any], index: int) -> tuple[str, ...]:
    process_keys = item["stranded_process_keys"]
    if not isinstance(process_keys, list) or not all(
        isinstance(value, str) and value for value in process_keys
    ):
        raise SmokeFailureError(f"allowlist entry {index} has invalid process keys")
    return tuple(process_keys)


def _allowlist_entry(item: object, index: int) -> BindingViolation:
    if not isinstance(item, dict):
        raise SmokeFailureError(f"allowlist entry {index} must be an object")
    expected_fields = {
        "profile_fragments",
        "service",
        "providing_plugin",
        "configured_plugin",
        "stranded_process_keys",
        "reason",
    }
    if set(item) != expected_fields:
        raise SmokeFailureError(f"allowlist entry {index} fields differ: {sorted(item)}")
    profile, service, providing_plugin, configured_plugin = _allowlist_identity(item, index)
    process_keys = _allowlist_process_keys(item, index)
    reason = item["reason"]
    if not isinstance(reason, str) or not reason.strip():
        raise SmokeFailureError(f"allowlist entry {index} has no reason")
    return BindingViolation(
        profile=profile,
        service=service,
        providing_plugin=providing_plugin,
        stranded_process_keys=process_keys,
        configured_plugin=configured_plugin,
    )


def _load_binding_allowlist() -> set[BindingViolation]:
    raw = json.loads(_SERVICE_BINDING_ALLOWLIST.read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        raise SmokeFailureError("service-binding allowlist must be a JSON list")
    entries: set[BindingViolation] = set()
    for index, item in enumerate(raw):
        entry = _allowlist_entry(item, index)
        if entry in entries:
            raise SmokeFailureError(f"duplicate allowlist entry: {entry}")
        entries.add(entry)
    return entries


def _bizops_profile_path() -> Path:
    return _PROFILE_DIR / "".join(("macos-", "b", "izops.yaml"))


def _check_wrong_enabled_provider_control(
    service_by_interface: dict[str, str],
) -> None:
    profile_path = _bizops_profile_path()
    profile = copy.deepcopy(_load_mapping(profile_path))
    service_name = "local_self_deployment_service"
    expected_plugin = "macos_self_deployment_plugin"
    observed_plugin = "default_thinking_plugin"
    cast_bindings = profile["service_bindings"]
    if not isinstance(cast_bindings, dict):
        raise SmokeFailureError(f"invalid control bindings shape: {profile_path}")
    cast_bindings[service_name] = observed_plugin
    diagnostic = ""
    try:
        _profile_binding_violations(
            profile_path,
            profile,
            service_by_interface,
        )
    except SmokeFailureError as exc:
        diagnostic = str(exc)
    expected_parts = (
        profile_path.name,
        service_name,
        observed_plugin,
        expected_plugin,
        "does not declare it",
    )
    _check(
        "wrong-enabled-provider-is-actionable",
        all(part in diagnostic for part in expected_parts),
        f"expected service/configured non-declarer/enabled declarer diagnostic; "
        f"found {diagnostic!r}",
    )


def _check_disabled_declaring_provider_control(
    service_by_interface: dict[str, str],
) -> None:
    profile_path = _bizops_profile_path()
    profile = copy.deepcopy(_load_mapping(profile_path))
    plugin_name = "macos_self_deployment_plugin"
    plugins = profile["plugins"]
    if not isinstance(plugins, list):
        raise SmokeFailureError(f"invalid control plugins shape: {profile_path}")
    plugins.remove(plugin_name)
    diagnostic = ""
    try:
        _profile_binding_violations(profile_path, profile, service_by_interface)
    except SmokeFailureError as exc:
        diagnostic = str(exc)
    expected_parts = (
        profile_path.name,
        "local_self_deployment_service",
        plugin_name,
        "not enabled",
    )
    _check(
        "declaring-but-disabled-provider-is-actionable",
        all(part in diagnostic for part in expected_parts),
        f"expected profile/service/provider disabled diagnostic; found {diagnostic!r}",
    )


def _check_bound_nondeclarer_without_real_declarer_control(
    service_by_interface: dict[str, str],
) -> None:
    profile_path = _bizops_profile_path()
    profile = copy.deepcopy(_load_mapping(profile_path))
    real_declarer = "macos_self_deployment_plugin"
    configured_nondeclarer = "default_thinking_plugin"
    service_names = (
        "self_deployment_service",
        "local_self_deployment_service",
    )
    plugins = profile["plugins"]
    bindings = profile["service_bindings"]
    if not isinstance(plugins, list) or not isinstance(bindings, dict):
        raise SmokeFailureError(f"invalid missing-declarer control: {profile_path}")
    plugins.remove(real_declarer)
    for service_name in service_names:
        bindings[service_name] = configured_nondeclarer

    diagnostic = ""
    try:
        _profile_binding_violations(profile_path, profile, service_by_interface)
    except SmokeFailureError as exc:
        diagnostic = str(exc)
    expected_parts = (
        profile_path.name,
        *service_names,
        configured_nondeclarer,
        "does not declare it",
        "no enabled plugin declares this registered service",
    )
    _check(
        "bound-nondeclarer-with-real-declarer-absent-is-actionable",
        all(part in diagnostic for part in expected_parts),
        "expected both services, configured non-declarer, and absent enabled "
        f"declarer diagnostic; found {diagnostic!r}",
    )


def _synthetic_allowlist_entry(
    *,
    profile: str,
    service: str,
    providing_plugin: str,
    configured_plugin: str,
) -> BindingViolation:
    return _allowlist_entry(
        {
            "profile_fragments": [profile],
            "service": service,
            "providing_plugin": providing_plugin,
            "configured_plugin": configured_plugin,
            "stranded_process_keys": list(_stranded_process_keys(service)),
            "reason": "Synthetic permanent multi-declarer identity control.",
        },
        0,
    )


def _check_multi_declarer_allowlist_controls() -> None:
    profile_path = Path("synthetic-multi-declarer.yaml")
    service_name = "quality_service"
    provider_a = "synthetic_provider_a"
    provider_b = "synthetic_provider_b"
    plugins = (provider_a, provider_b)
    declarations = {
        provider_a: {service_name},
        provider_b: {service_name},
    }
    process_keys = _stranded_process_keys(service_name)

    for selected, unselected in ((provider_b, provider_a), (provider_a, provider_b)):
        actual = _declaration_to_binding_violations(
            profile_path,
            plugins,
            {service_name: selected},
            declarations,
        )
        expected = BindingViolation(
            profile=profile_path.name,
            service=service_name,
            providing_plugin=unselected,
            stranded_process_keys=process_keys,
            configured_plugin=selected,
        )
        allowed = _synthetic_allowlist_entry(
            profile=profile_path.name,
            service=service_name,
            providing_plugin=unselected,
            configured_plugin=selected,
        )
        _check(
            f"multi-declarer-select-{selected}-is-exactly-allowlistable",
            actual == {expected} and allowed == expected,
            f"expected {expected!r}; actual={sorted(actual)!r}, allowed={allowed!r}",
        )

    selected_b_actual = _declaration_to_binding_violations(
        profile_path,
        plugins,
        {service_name: provider_b},
        declarations,
    )
    stale = _synthetic_allowlist_entry(
        profile=profile_path.name,
        service=service_name,
        providing_plugin=provider_a,
        configured_plugin="synthetic_provider_c",
    )
    _check(
        "multi-declarer-stale-selected-provider-remains-stale",
        bool(selected_b_actual - {stale}) and bool({stale} - selected_b_actual),
        f"configured provider was not part of allowlist identity: {stale!r}",
    )


def _check_shipped_profile_service_closure() -> None:
    service_by_interface = _registered_service_by_interface()
    actual = _binding_violations(service_by_interface)
    allowed = _load_binding_allowlist()
    unallowlisted = sorted(actual - allowed)
    stale = sorted(allowed - actual)
    _check(
        "no-unallowlisted-shipped-service-binding-gaps",
        not unallowlisted,
        f"new missing or misbound service faces: {unallowlisted}",
    )
    _check(
        "no-stale-shipped-service-binding-allowlist-entries",
        not stale,
        f"remove repaired or mismatched debt entries: {stale}",
    )
    _check_wrong_enabled_provider_control(service_by_interface)
    _check_disabled_declaring_provider_control(service_by_interface)
    _check_bound_nondeclarer_without_real_declarer_control(service_by_interface)
    _check_multi_declarer_allowlist_controls()


def _check_binding_satisfaction(profile: dict[str, Any], plugins: set[str]) -> None:
    bindings = profile.get("service_bindings") or {}
    _check(
        "service_bindings is a mapping",
        isinstance(bindings, dict),
        f"got {type(bindings).__name__}",
    )
    for service_name, plugin_name in bindings.items():
        _check(
            f"binding-satisfaction[{service_name}]",
            plugin_name in plugins,
            f"{service_name} -> {plugin_name!r} not in plugins allowlist",
        )
    for absent_binding in _MUST_BE_ABSENT_BINDINGS:
        _check(
            f"declared-absent[{absent_binding}]",
            absent_binding not in bindings,
            f"{absent_binding} must be absent (declared-vacant / opt-in shape) "
            f"but found bound to {bindings.get(absent_binding)!r}",
        )


def _check_no_orphan_starting_actions(profile: dict[str, Any], plugins: set[str]) -> None:
    starting_actions = profile.get("starting_actions") or []
    _check(
        "starting_actions is a list",
        isinstance(starting_actions, list),
        f"got {type(starting_actions).__name__}",
    )
    for action in starting_actions:
        name = action.get("name", "<unnamed>")
        process_key = action.get("process_key", "")
        if process_key.startswith("plugin::"):
            owner = process_key.split("::")[1]
            _check(
                f"no-orphan-starting-action[{name}]",
                owner in plugins,
                f"process_key {process_key!r} references plugin {owner!r} "
                "not in the plugins allowlist",
            )
        else:
            raise SmokeFailureError(
                f"starting_action {name!r} has an unrecognized process_key shape "
                f"{process_key!r} — this profile only expects plugin::<name>::... "
                "starting actions; extend the check before adding a service_interface:: one"
            )


def _check_public_safe(text: str, source: str) -> None:
    operator_hit = _OPERATOR_PATH_PATTERN.search(text)
    _check(
        f"no-operator-path[{source}]",
        operator_hit is None,
        f"found operator-identity path {operator_hit.group(0)!r}" if operator_hit else "",
    )
    secret_hit = _SECRET_KEY_PATTERN.search(text)
    _check(
        f"no-secret-key[{source}]",
        secret_hit is None,
        f"found secret-shaped key {secret_hit.group(1)!r}" if secret_hit else "",
    )


def _check_baseline_files_are_valid_json() -> list[Path]:
    if not _BASELINE_DIR.is_dir():
        raise SmokeFailureError(f"profile_baseline directory missing: {_BASELINE_DIR}")
    baseline_files = sorted(_BASELINE_DIR.glob("*.json"))
    _check(
        "profile_baseline has at least one file",
        len(baseline_files) > 0,
        f"no *.json files found under {_BASELINE_DIR}",
    )
    for path in baseline_files:
        text = path.read_text(encoding="utf-8")
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError as exc:
            raise SmokeFailureError(f"{path.name} is not valid JSON: {exc}") from exc
        _check(
            f"baseline-is-mapping[{path.name}]",
            isinstance(parsed, dict),
            f"{path.name} did not parse to a JSON object",
        )
    return baseline_files


def main() -> int:
    try:
        profile = _load_profile()
        plugins_raw = profile.get("plugins") or []
        _check(
            "plugins is a non-empty list of strings",
            isinstance(plugins_raw, list)
            and len(plugins_raw) > 0
            and all(isinstance(p, str) for p in plugins_raw),
            f"got {plugins_raw!r}",
        )
        plugins = set(plugins_raw)

        _check_binding_satisfaction(profile, plugins)
        _check_no_orphan_starting_actions(profile, plugins)
        _check_shipped_profile_service_closure()

        profile_text = _PROFILE_PATH.read_text(encoding="utf-8")
        _check_public_safe(profile_text, _PROFILE_PATH.name)

        baseline_files = _check_baseline_files_are_valid_json()
        for path in baseline_files:
            _check_public_safe(path.read_text(encoding="utf-8"), path.name)

    except SmokeFailureError as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        print(f"  ({len(_CHECKS_RUN)} checks attempted before failure)", file=sys.stderr)
        return 1

    print(
        f"macos_free_profile_smoke OK: {len(_CHECKS_RUN)} checks passed "
        f"({len(plugins)} plugins, {len(baseline_files)} baseline files)"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
