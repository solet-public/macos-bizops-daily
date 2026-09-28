"""Host-conditioned fresh setup choices (iss_3a2a74ea, rul_c11cf191, rul_5cc2910c).

A static decision option may declare ``host_available_when`` naming one of the
flow's ``host_profiles`` -- the same thresholds the release's existing-install
flow declares -- with ``meets`` or ``below``.  Only choices still being made
are bound to the host: an explicit ``--decision`` selection, a flow default, or
a decision prompt.  A recorded answer is never re-judged, so a solet whose Mac
is later upgraded keeps the choice it was created with.

The host is measured with r52's :func:`host_platform.read_host_platform`, and
only while such a choice is pending; a Mac that cannot be measured refuses the
choice rather than guessing.  Binding marks each option the host does not
satisfy ``unavailable`` with an owner-facing ``unavailable_reason`` and drops it
from ``recommended_option_refs``, so the existing default rule (exactly one
recommendation) and the existing option refusal apply unchanged.
"""

from __future__ import annotations

import copy
import dataclasses
from collections.abc import Callable, Iterator, Mapping
from typing import TYPE_CHECKING, cast

from .errors import ContractError, HostPlatformUnknownError
from .host_platform import HostPlatform, HostPlatformError, read_host_platform
from .models import JsonValue

if TYPE_CHECKING:
    from .contracts import ContractBundle

__all__ = [
    "HostProfile",
    "host_bound",
    "host_profiles",
    "require_host_available",
    "validate_host_conditions",
]

type HostReader = Callable[[], HostPlatform]
type JsonObject = dict[str, JsonValue]

_CONDITION_KEYS = frozenset({"host_profile", "operator"})
_PROFILE_KEYS = frozenset({"macos_major_min", "machine"})
_OPERATORS = frozenset({"meets", "below"})


@dataclasses.dataclass(frozen=True, slots=True)
class HostProfile:
    """One named host threshold from the flow's ``host_profiles``."""

    name: str
    macos_major_min: int
    machine: str

    def met_by(self, host: HostPlatform) -> bool:
        return host.macos_major >= self.macos_major_min and host.machine == self.machine

    def describe(self) -> str:
        return f"macOS {self.macos_major_min} or later on {self.machine}"


def host_profiles(flow: Mapping[str, JsonValue]) -> dict[str, HostProfile]:
    """The flow's declared host profiles; an absent registry declares none."""
    raw = flow.get("host_profiles", {})
    if not isinstance(raw, dict):
        raise ContractError("setup flow host_profiles must be an object")
    profiles: dict[str, HostProfile] = {}
    for name, row in raw.items():
        if not isinstance(row, dict) or frozenset(row) != _PROFILE_KEYS:
            raise ContractError(f"host profile {name!r} must declare exactly macos_major_min and machine")
        minimum, machine = row["macos_major_min"], row["machine"]
        if type(minimum) is not int or not 11 <= minimum <= 99 or not isinstance(machine, str) or not machine:
            raise ContractError(f"host profile {name!r} has an invalid macos_major_min or machine")
        profiles[name] = HostProfile(name, minimum, machine)
    return profiles


def validate_host_conditions(flow: Mapping[str, JsonValue]) -> None:
    """Every ``host_available_when`` is closed and names a declared host profile."""
    profiles = host_profiles(flow)
    for decision_id, option_id, condition in _host_conditions(flow):
        label = f"decision {decision_id!r} option {option_id!r} host_available_when"
        if frozenset(condition) != _CONDITION_KEYS or condition["operator"] not in _OPERATORS:
            raise ContractError(f"{label} must declare exactly host_profile and operator meets|below")
        if condition["host_profile"] not in profiles:
            raise ContractError(f"{label} names undeclared host profile {condition['host_profile']!r}")


def host_bound(
    bundle: ContractBundle,
    decisions: Mapping[str, JsonValue],
    selections: Mapping[str, JsonValue],
    *,
    read: HostReader | None = None,
) -> ContractBundle:
    """``bundle`` as this Mac may choose from, measured only while a host-conditioned choice is pending."""
    conditioned = {decision_id for decision_id, _option, _condition in _host_conditions(bundle.flow)}
    if not any(decision_id in selections or decision_id not in decisions for decision_id in conditioned):
        return bundle
    host = _measure(read_host_platform if read is None else read)
    flow = copy.deepcopy(bundle.flow)
    profiles = host_profiles(flow)
    for decision_id, option_id, condition in _host_conditions(flow):
        profile = profiles[str(condition["host_profile"])]
        if profile.met_by(host) != (condition["operator"] == "meets"):
            _withhold(cast(JsonObject, cast(JsonObject, flow["decisions"])[decision_id]), option_id,
                      profile, host, str(condition["operator"]))
    return dataclasses.replace(bundle, flow=flow)


def _withhold(definition: JsonObject, option_id: str, profile: HostProfile, host: HostPlatform, operator: str) -> None:
    """Mark one option unavailable on this Mac, with its reason, and stop recommending it."""
    option = _static_options(definition)[option_id]
    option["availability"] = "unavailable"
    option["unavailable_reason"] = _unavailable_reason(str(option["label"]), profile, host, operator)
    recommended = definition.get("recommended_option_refs")
    if isinstance(recommended, list):
        definition["recommended_option_refs"] = [item for item in recommended if item != option_id]


def require_host_available(bound: ContractBundle, selections: Mapping[str, JsonValue]) -> None:
    """Refuse an explicit selection of an option this Mac cannot take, naming why."""
    for decision_id, option_id, _condition in _host_conditions(bound.flow):
        if selections.get(decision_id) != option_id:
            continue
        option = _static_options(bound.decisions[decision_id])[option_id]
        if option.get("availability") != "unavailable":
            continue
        offered = sorted(
            candidate_id
            for candidate_id, candidate in _static_options(bound.decisions[decision_id]).items()
            if candidate.get("availability") == "supported"
        )
        raise ContractError(
            f"decision {decision_id!r} option {option_id!r} is not available on this Mac: {option['unavailable_reason']}",
            repair=f"Choose one of {offered} for {decision_id}, or run setup on a Mac that meets the requirement.",
        )


def _measure(read: HostReader) -> HostPlatform:
    try:
        return read()
    except HostPlatformError as exc:
        raise HostPlatformUnknownError(
            f"this Mac could not be measured ({exc}); the embeddings and summaries choices depend on its macOS version",
            repair="Check that /usr/bin/sw_vers and /usr/bin/uname run, then preview again.",
        ) from exc


def _unavailable_reason(label: str, profile: HostProfile, host: HostPlatform, operator: str) -> str:
    running = f"this Mac runs macOS {host.product_version} ({host.machine})"
    if operator == "meets":
        return f"{running}; {label} requires {profile.describe()}"
    return f"{running}; {label} is for Macs below {profile.describe()}, which take the Apple-native option instead"


def _host_conditions(flow: Mapping[str, JsonValue]) -> Iterator[tuple[str, str, JsonObject]]:
    decisions = flow.get("decisions")
    if not isinstance(decisions, dict):
        return
    for decision_id, definition in decisions.items():
        if not isinstance(definition, dict):
            continue
        for option_id, option in _static_options(definition).items():
            condition = option.get("host_available_when")
            if condition is None:
                continue
            if not isinstance(condition, dict):
                raise ContractError(f"decision {decision_id!r} option {option_id!r} host_available_when must be an object")
            yield decision_id, option_id, condition


def _static_options(definition: JsonObject) -> dict[str, JsonObject]:
    source = definition.get("option_source")
    if not isinstance(source, dict) or source.get("mode") != "static":
        return {}
    options = source.get("options")
    if not isinstance(options, dict):
        return {}
    return {key: value for key, value in options.items() if isinstance(value, dict)}
