"""Pure validator for service-binding satisfaction during apply_manifest.

Mirrors the boot-time check in
:func:`ananta.core.orchestration.startup_sequence._load_service_bindings`
(``startup_sequence.py:191-203``): every bound plugin name must appear in the
loaded plugin set, or ``StartupError`` is raised. ``apply_manifest`` runs the
same check as a pre-flight gate before writing the new manifest, so an
operator-readable rejection happens before the solet restarts into a
broken state.

The helper is pure — no I/O, no platform-state access. The caller passes the
proposed manifest and the current ``service_bindings.json`` content. The
helper returns a :class:`ValidationResult`; the caller decides whether to
raise :class:`BindingUnsatisfiedError` or fold the result into an
``apply_manifest`` rejection envelope.

This module is the v1 surface specified by Coordinator dispatch
2026-05-30. Architect's design (``workbench/2026-05-30_plugin_lifecycle_architect_pass.md``
§14.1) generalises the binding-satisfaction rule from "self_deployment_service
must have a provider" to "every binding in the new manifest must have a
provider in the new plugins list." v1 enforces both: a configurable
``required_services`` set ensures the deployment plugin can never be
silently unbound, and the general rule covers every other service.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

_DEFAULT_REQUIRED_SERVICES: tuple[str, ...] = ("self_deployment_service",)


@dataclass(frozen=True, slots=True)
class MissingBinding:
    """One service binding the new manifest fails to satisfy.

    ``service``: the service name (e.g. ``"self_deployment_service"``).
    ``current_provider``: provider in the current ``service_bindings.json``,
        or ``None`` if the service is not currently bound.
    ``replacement``: provider proposed in the new manifest's
        ``service_bindings``, or ``None`` when the new manifest omits the
        binding entirely (the required-service path).
    """

    service: str
    current_provider: str | None
    replacement: str | None


@dataclass(frozen=True, slots=True)
class ValidationResult:
    """Outcome of :func:`validate_bindings_satisfied`."""

    satisfied: bool
    missing_bindings: tuple[MissingBinding, ...]


class BindingUnsatisfiedError(Exception):
    """Raised by callers when a manifest fails binding validation.

    The validator itself returns a :class:`ValidationResult`; this exception
    is provided for callers (e.g. ``apply_manifest``) that prefer to raise
    rather than branch on ``result.satisfied``.
    """

    def __init__(self, missing_bindings: tuple[MissingBinding, ...]) -> None:
        self.missing_bindings = missing_bindings
        services = ", ".join(mb.service for mb in missing_bindings)
        super().__init__(
            f"Service bindings unsatisfied by new manifest: [{services}]"
        )


def validate_bindings_satisfied(
    new_manifest: Mapping[str, Any],
    current_bindings: Mapping[str, str],
    required_services: Iterable[str] = _DEFAULT_REQUIRED_SERVICES,
) -> ValidationResult:
    """Check that the new manifest's service bindings will be satisfied at next boot.

    Two rules, both modelled on
    ``startup_sequence._load_service_bindings``'s boot-time validation:

    1. **Required services present.** Every name in ``required_services``
       must appear in ``new_manifest['service_bindings']``. If absent, an
       entry with ``replacement=None`` is added to
       :attr:`ValidationResult.missing_bindings`. This is the gate that
       prevents operators from silently unbinding ``self_deployment_service``
       (without which ``apply_manifest`` itself becomes un-callable on the
       restarted solet — see Architect §4.3).

    2. **Every bound provider is in the new plugins list.** For each
       ``(service, provider)`` entry in
       ``new_manifest['service_bindings']``, ``provider`` must appear in
       ``new_manifest['plugins']``. Otherwise an entry with
       ``replacement=<provider>`` is added. This is the general rule
       generalised from substrate doc's deployment-specific framing.

    The helper does NOT check that the provider actually implements the
    bound interface — that contract assertion runs at
    ``prepare_for_readiness`` time on the live plugin. Late-fail is
    acceptable in v1 per Coordinator's scope for this prototype slice.

    Args:
        new_manifest: Proposed manifest mapping. Must carry ``plugins``
            (list of plugin names) and ``service_bindings`` (mapping of
            service name to provider plugin name). Other keys are ignored.
        current_bindings: Current ``service_bindings.json`` content as a
            flat ``{service_name: plugin_name}`` mapping. Used solely to
            populate :attr:`MissingBinding.current_provider` in the
            diagnostic payload.
        required_services: Service names that MUST appear in the new
            manifest's bindings even if absent from current bindings.
            Defaults to ``("self_deployment_service",)``.

    Returns:
        :class:`ValidationResult`. ``satisfied=True`` means the new manifest
        passes both rules and is safe to write. ``satisfied=False`` carries
        one :class:`MissingBinding` per violation so the caller can render
        actionable rejection reasons.

    Raises:
        ValueError: when ``new_manifest`` lacks ``plugins`` or
            ``service_bindings``, or those keys carry an unexpected shape.
    """
    new_plugins = _extract_plugins(new_manifest)
    new_bindings = _extract_bindings(new_manifest)

    missing: list[MissingBinding] = []
    _collect_required_service_misses(
        required_services, new_bindings, current_bindings, missing,
    )
    _collect_provider_misses(new_bindings, new_plugins, current_bindings, missing)

    return ValidationResult(
        satisfied=not missing,
        missing_bindings=tuple(missing),
    )


def _extract_plugins(new_manifest: Mapping[str, Any]) -> frozenset[str]:
    """Return the new manifest's plugins list as a frozenset of names."""
    plugins = new_manifest.get("plugins")
    if not isinstance(plugins, list):
        raise ValueError(
            "new_manifest['plugins'] must be a list of plugin name strings; "
            f"got {type(plugins).__name__}"
        )
    for entry in plugins:
        if not isinstance(entry, str) or not entry:
            raise ValueError(
                "new_manifest['plugins'] entries must be non-empty strings"
            )
    return frozenset(plugins)


def _extract_bindings(new_manifest: Mapping[str, Any]) -> dict[str, str]:
    """Return the new manifest's service_bindings mapping, validated."""
    bindings = new_manifest.get("service_bindings")
    if not isinstance(bindings, dict):
        raise ValueError(
            "new_manifest['service_bindings'] must be a mapping of "
            f"service_name to plugin_name; got {type(bindings).__name__}"
        )
    result: dict[str, str] = {}
    for service, provider in bindings.items():
        if not isinstance(service, str) or not service:
            raise ValueError(
                "new_manifest['service_bindings'] keys must be non-empty strings"
            )
        if not isinstance(provider, str) or not provider:
            raise ValueError(
                f"new_manifest['service_bindings'][{service!r}] must be a "
                "non-empty plugin-name string"
            )
        result[service] = provider
    return result


def _collect_required_service_misses(
    required_services: Iterable[str],
    new_bindings: Mapping[str, str],
    current_bindings: Mapping[str, str],
    missing: list[MissingBinding],
) -> None:
    """Emit a MissingBinding for every required service absent from new_bindings."""
    for service in required_services:
        if service in new_bindings:
            continue
        missing.append(MissingBinding(
            service=service,
            current_provider=current_bindings.get(service),
            replacement=None,
        ))


def _collect_provider_misses(
    new_bindings: Mapping[str, str],
    new_plugins: frozenset[str],
    current_bindings: Mapping[str, str],
    missing: list[MissingBinding],
) -> None:
    """Emit a MissingBinding for every bound provider absent from new_plugins."""
    for service, provider in new_bindings.items():
        if provider in new_plugins:
            continue
        missing.append(MissingBinding(
            service=service,
            current_provider=current_bindings.get(service),
            replacement=provider,
        ))
