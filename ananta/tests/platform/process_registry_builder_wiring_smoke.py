#!/usr/bin/env python3
"""Smoke test for `ProcessRegistryBuilder` collaborator wiring.

Step 9.A of the plugin-god-class remediation
(`workbench/2026-05-25_plugin_god_class_remediation.md` §9.1)
decomposed the 2,074-LOC `ProcessRegistryBuilder` into six focused
collaborator classes. This smoke verifies the orchestrator wires them
together correctly:

  - Every collaborator is instantiated and held on the orchestrator.
  - Shared dependencies (the `InvocationSchemaGenerator`, the
    `PluginRegistrationValidator`, the `ServiceInterfaceMetadataGenerator`)
    are passed BY REFERENCE — every consumer holds the same instance, not
    its own copy. Independent copies would silently regress.
  - The registry-skeleton initialization still produces the expected shape.
  - The canonical `build_process_registry` entry point is still callable.

Project policy: no pytest. Exits 0 on success, 1 on first failure.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))

from ananta.core.process_registry.builder import (  # noqa: E402
    ProcessRegistryBuilder,
    build_process_registry,
)
from ananta.core.process_registry.invocation_schema_generator import (  # noqa: E402
    InvocationSchemaGenerator,
)
from ananta.core.process_registry.kb_overlay_loader import (  # noqa: E402
    KnowledgeBaseOverlayLoader,
)
from ananta.core.process_registry.plugin_process_scanner import (  # noqa: E402
    PluginProcessScanner,
)
from ananta.core.process_registry.plugin_registration_validator import (  # noqa: E402
    PluginRegistrationValidator,
)
from ananta.core.process_registry.service_interface_metadata_generator import (  # noqa: E402
    ServiceInterfaceMetadataGenerator,
)
from ananta.core.process_registry.service_interface_scanner import (  # noqa: E402
    ServiceInterfaceScanner,
)

_passed = 0
_failed: list[str] = []


def _check(condition: object, label: str) -> None:
    global _passed
    if condition:
        _passed += 1
        print(f"  PASS  {label}")
    else:
        _failed.append(label)
        print(f"  FAIL  {label}")


class _MockPluginManager:
    """Minimal duck-typed PluginManager — just enough for the wiring smoke.

    The orchestrator only inspects `plugins` (a dict) and threads the
    manager into the scanner + KB loader. With an empty `plugins` dict
    no real plugin work happens, so we can exercise the wiring without
    needing a real PluginManager.
    """

    plugins: dict[str, object] = {}
    orchestrator_ref: object | None = None


def _case_collaborators_wired() -> None:
    print("\nCase 1: orchestrator instantiates all six collaborators")
    builder = ProcessRegistryBuilder(_MockPluginManager())  # type: ignore[arg-type]
    _check(isinstance(builder._schema_generator, InvocationSchemaGenerator), "InvocationSchemaGenerator wired")
    _check(isinstance(builder._metadata_generator, ServiceInterfaceMetadataGenerator), "ServiceInterfaceMetadataGenerator wired")
    _check(isinstance(builder._validator, PluginRegistrationValidator), "PluginRegistrationValidator wired")
    _check(isinstance(builder._plugin_scanner, PluginProcessScanner), "PluginProcessScanner wired")
    _check(isinstance(builder._service_interface_scanner, ServiceInterfaceScanner), "ServiceInterfaceScanner wired")
    _check(isinstance(builder._kb_loader, KnowledgeBaseOverlayLoader), "KnowledgeBaseOverlayLoader wired")


def _case_shared_references() -> None:
    print("\nCase 2: collaborators share the same dependency instances")
    builder = ProcessRegistryBuilder(_MockPluginManager())  # type: ignore[arg-type]
    _check(
        builder._kb_loader._schema_generator is builder._schema_generator,
        "kb_loader.schema_generator is the orchestrator's schema_generator",
    )
    _check(
        builder._plugin_scanner._schema_generator is builder._schema_generator,
        "plugin_scanner.schema_generator is the orchestrator's schema_generator",
    )
    _check(
        builder._service_interface_scanner._schema_generator is builder._schema_generator,
        "service_interface_scanner.schema_generator is the orchestrator's schema_generator",
    )
    _check(
        builder._plugin_scanner._validator is builder._validator,
        "plugin_scanner.validator is the orchestrator's validator",
    )
    _check(
        builder._plugin_scanner._metadata_generator is builder._metadata_generator,
        "plugin_scanner.metadata_generator is the orchestrator's metadata_generator",
    )


def _case_registry_skeleton() -> None:
    print("\nCase 3: registry skeleton has the expected shape")
    builder = ProcessRegistryBuilder(_MockPluginManager())  # type: ignore[arg-type]
    skeleton = builder._initialize_registry_structure()
    _check(skeleton.get("processes") == {}, "skeleton.processes is empty dict")
    _check("last_updated" in skeleton, "skeleton has last_updated timestamp")
    _check(skeleton.get("version") == "2.0.0", "skeleton.version is 2.0.0")
    _check(
        skeleton.get("architecture") == "service_interface_only",
        "skeleton.architecture is service_interface_only",
    )


def _case_canonical_entry() -> None:
    print("\nCase 4: canonical entry point preserved")
    _check(callable(build_process_registry), "build_process_registry is callable")
    _check(
        build_process_registry.__module__ == "ananta.core.process_registry.builder",
        "build_process_registry still lives in ananta.core.process_registry.builder",
    )


def main() -> int:
    print("ProcessRegistryBuilder collaborator wiring smoke")
    print("================================================")
    _case_collaborators_wired()
    _case_shared_references()
    _case_registry_skeleton()
    _case_canonical_entry()

    print("\n------------------------------------------------")
    print(f"PASSED: {_passed}")
    print(f"FAILED: {len(_failed)}")
    if _failed:
        print("\nFailures:")
        for label in _failed:
            print(f"  - {label}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
