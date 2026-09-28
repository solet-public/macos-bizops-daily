"""Closed parser and state classifier for release-declared plugin transitions (iss_6d26db73).

The declaration (``knowledge_base/plugin_transitions.json``, schema
``solet.plugin_transitions.v1``) names, per profile, one managed service whose
active plugin a release replaces, adds or retires.  This module is pure: it
parses the declaration bytes and classifies one observed profile config into
exactly one transition state.  It reads nothing but what it is given and never
writes; the ``existing::migration.plugin_transition`` handler owns I/O.

Only the managed subset is compared: the roster entries for the declared
plugins, the declared service binding, the old plugin's config, and the new
plugin's config.  Every other roster entry, binding, file and field is outside
the transition and is never a reason to refuse or to write.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Literal, cast

from .setup_adapter_contract import JsonObject, JsonValue

__all__ = [
    "CONFIG_RENDERERS",
    "READINESS_KINDS",
    "SCHEMA",
    "Classification",
    "Declaration",
    "DeclarationError",
    "Observation",
    "PluginConfig",
    "SourcePlugin",
    "TargetPlugin",
    "Transition",
    "classify",
    "owner_sentence",
    "parse_declaration",
]

SCHEMA = "solet.plugin_transitions.v1"
CONFIG_RENDERERS = frozenset({"coreai_embeddings", "apple_inference"})
READINESS_KINDS = frozenset({"coreai_embedding", "import"})
_KINDS = frozenset({"replace", "add", "retire"})
_MATCHES = frozenset({"exact", "fields"})
_IDENTIFIER = re.compile(r"^[a-z][a-z0-9_]{1,127}$")
_MIGRATION_ID = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z0-9_]+)+$")
_PROFILE = re.compile(r"^[a-z][a-z0-9-]{1,63}$")

type Kind = Literal["replace", "add", "retire"]
type State = Literal["not_applicable", "unsupported", "done", "eligible", "partial", "conflict", "host_unsupported"]


class DeclarationError(ValueError):
    """The declaration is not the closed v1 shape; nothing may be planned from it."""


@dataclass(frozen=True, slots=True)
class SourcePlugin:
    plugin: str
    config_match: Literal["exact", "fields"]
    config: JsonObject


@dataclass(frozen=True, slots=True)
class TargetPlugin:
    plugin: str
    config_renderer: str
    readiness: str
    #: A host profile of the release's existing-install flow (rul_385dac24); ``None`` runs on any host.
    requires_host: str | None = None


@dataclass(frozen=True, slots=True)
class Transition:
    migration_id: str
    kind: Kind
    service_label: str
    replacement_label: str
    done_note: str | None
    profiles: tuple[str, ...]
    service: str | None
    source: SourcePlugin | None
    target: TargetPlugin | None


@dataclass(frozen=True, slots=True)
class Declaration:
    sha256: str
    transitions: tuple[Transition, ...]


@dataclass(frozen=True, slots=True)
class PluginConfig:
    """One ``profile/config/plugins/<plugin>.json`` as observed: absent, unreadable, or parsed."""

    present: bool
    text: str | None
    value: JsonObject | None


@dataclass(frozen=True, slots=True)
class Observation:
    """The managed subset of one target's profile config, read once."""

    profile: str | None
    roster: tuple[str, ...]
    bindings: dict[str, str]
    configs: dict[str, PluginConfig]


@dataclass(frozen=True, slots=True)
class Classification:
    state: State
    detail: str


# --- parsing ---------------------------------------------------------------------


def parse_declaration(raw_bytes: bytes) -> Declaration:
    """Parse and closed-validate the declaration; refuse anything ambiguous before planning."""
    try:
        raw: object = json.loads(raw_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DeclarationError(f"plugin transition declaration is unreadable: {exc}") from exc
    root = _object(raw, {"schema", "transitions"}, "declaration")
    if root["schema"] != SCHEMA:
        raise DeclarationError(f"plugin transition declaration schema must be {SCHEMA!r}")
    items = root["transitions"]
    if not isinstance(items, list):
        raise DeclarationError("transitions must be an array")
    transitions = tuple(_transition(item) for item in cast(list[JsonValue], items))
    _refuse_overlaps(transitions)
    return Declaration("sha256:" + hashlib.sha256(raw_bytes).hexdigest(), transitions)


def _transition(raw: JsonValue) -> Transition:
    keys = {"migration_id", "kind", "service_label", "replacement_label", "done_note", "profiles", "service", "from", "to"}
    item = _object(raw, keys, "transition", optional={"done_note", "service", "from", "to"})
    migration_id = _string(item["migration_id"], "migration_id", _MIGRATION_ID)
    labels = [_owner_text(item[key], f"{migration_id}.{key}", 60) for key in ("service_label", "replacement_label")]
    done_note = None if item.get("done_note") is None else _owner_text(item["done_note"], f"{migration_id}.done_note", 200)
    kind = item["kind"]
    if kind not in _KINDS:
        raise DeclarationError(f"{migration_id}: kind must be one of {sorted(_KINDS)}")
    profiles = _profiles(item["profiles"], migration_id)
    service = None if item.get("service") is None else _string(item["service"], f"{migration_id}.service", _IDENTIFIER)
    source = None if "from" not in item else _source(item["from"], migration_id)
    target = None if "to" not in item else _target(item["to"], migration_id)
    typed_kind = cast(Kind, kind)
    _require_sides(typed_kind, source, target, service, migration_id)
    return Transition(migration_id, typed_kind, labels[0], labels[1], done_note, profiles, service, source, target)


def _owner_text(raw: JsonValue, label: str, limit: int) -> str:
    if not isinstance(raw, str) or not 0 < len(raw) <= limit or raw != raw.strip():
        raise DeclarationError(f"{label} must be one to {limit} characters of owner-facing text")
    return raw


def _require_sides(kind: Kind, source: SourcePlugin | None, target: TargetPlugin | None, service: str | None, migration_id: str) -> None:
    shapes = {
        "replace": source is not None and target is not None and service is not None,
        "add": source is None and target is not None,
        "retire": source is not None and target is None and service is None,
    }
    if not shapes[kind]:
        raise DeclarationError(f"{migration_id}: a {kind} transition has the wrong from/to/service sides")
    if source is not None and target is not None and source.plugin == target.plugin:
        raise DeclarationError(f"{migration_id}: from and to name the same plugin")


def _profiles(raw: JsonValue, migration_id: str) -> tuple[str, ...]:
    if not isinstance(raw, list) or not raw:
        raise DeclarationError(f"{migration_id}: profiles must be a non-empty array")
    profiles = tuple(_string(item, f"{migration_id}.profiles", _PROFILE) for item in cast(list[JsonValue], raw))
    if len(set(profiles)) != len(profiles):
        raise DeclarationError(f"{migration_id}: profiles repeat")
    return profiles


def _source(raw: JsonValue, migration_id: str) -> SourcePlugin:
    item = _object(raw, {"plugin", "config_match", "config"}, f"{migration_id}.from")
    match = item["config_match"]
    if match not in _MATCHES:
        raise DeclarationError(f"{migration_id}: config_match must be one of {sorted(_MATCHES)}")
    config = item["config"]
    if not isinstance(config, dict) or not config:
        raise DeclarationError(f"{migration_id}: from.config must be a non-empty object")
    return SourcePlugin(_string(item["plugin"], f"{migration_id}.from.plugin", _IDENTIFIER), cast(Literal["exact", "fields"], match), cast(JsonObject, config))


def _target(raw: JsonValue, migration_id: str) -> TargetPlugin:
    item = _object(raw, {"plugin", "config_renderer", "readiness", "requires_host"}, f"{migration_id}.to", optional={"requires_host"})
    renderer = item["config_renderer"]
    readiness = item["readiness"]
    if renderer not in CONFIG_RENDERERS:
        raise DeclarationError(f"{migration_id}: config_renderer must be one of {sorted(CONFIG_RENDERERS)}")
    if readiness not in READINESS_KINDS:
        raise DeclarationError(f"{migration_id}: readiness must be one of {sorted(READINESS_KINDS)}")
    requires_host = None if item.get("requires_host") is None else _string(item["requires_host"], f"{migration_id}.to.requires_host", _IDENTIFIER)
    return TargetPlugin(_string(item["plugin"], f"{migration_id}.to.plugin", _IDENTIFIER), renderer, readiness, requires_host)


def _refuse_overlaps(transitions: tuple[Transition, ...]) -> None:
    """Refuse duplicate ids, shared plugins or services, and cycles: each would make the order matter."""
    sources = [item.source.plugin for item in transitions if item.source is not None]
    targets = [item.target.plugin for item in transitions if item.target is not None]
    _refuse_repeats([item.migration_id for item in transitions], "migration_id values repeat")
    _refuse_repeats(sources, "two transitions share a from plugin")
    _refuse_repeats(targets, "two transitions share a to plugin")
    _refuse_repeats([item.service for item in transitions if item.service is not None], "two transitions share a service")
    if set(sources) & set(targets):
        raise DeclarationError("a plugin is both a from and a to: the declaration has a cycle")


def _refuse_repeats(values: list[str], message: str) -> None:
    if len(set(values)) != len(values):
        raise DeclarationError(message)


def _object(raw: object, keys: set[str], label: str, *, optional: set[str] | None = None) -> dict[str, JsonValue]:
    if not isinstance(raw, dict):
        raise DeclarationError(f"{label} must be an object")
    item = cast(dict[str, JsonValue], raw)
    present = set(item)
    required = keys - (optional or set())
    if not required <= present or not present <= keys:
        raise DeclarationError(f"{label} keys must be exactly {sorted(keys)} (optional {sorted(optional or set())})")
    return item


def _string(raw: JsonValue, label: str, pattern: re.Pattern[str]) -> str:
    if not isinstance(raw, str) or pattern.fullmatch(raw) is None:
        raise DeclarationError(f"{label} is not a valid identifier")
    return raw


# --- classification --------------------------------------------------------------


def classify(transition: Transition, observation: Observation, rendered: str | None) -> Classification:
    """Place one declared transition's managed subset in exactly one state.

    ``rendered`` is the replacement config this release would write (None when
    the release cannot render it, or the kind has no replacement).
    """
    if transition.kind == "replace":
        return _classify_replace(transition, observation, rendered)
    if transition.kind == "add":
        return _classify_add(transition, observation, rendered)
    return _classify_retire(transition, observation)


def _classify_replace(transition: Transition, observation: Observation, rendered: str | None) -> Classification:
    source = cast(SourcePlugin, transition.source)
    target = cast(TargetPlugin, transition.target)
    service = cast(str, transition.service)
    roster = observation.roster
    bound_source = _bound(observation, source.plugin)
    bound_target = _bound(observation, target.plugin)
    if source.plugin not in roster and target.plugin in roster and bound_target == [service] and not bound_source:
        return Classification("done", f"{service} is served by {target.plugin}")
    if source.plugin not in roster:
        return Classification("not_applicable", f"{source.plugin} is not in the roster")
    if observation.profile not in transition.profiles:
        return Classification("unsupported", f"profile {observation.profile!r} has no declared {transition.migration_id} transition")
    refusal = _replace_refusal(transition, observation, rendered)
    if refusal is not None:
        return Classification("conflict", refusal)
    if target.plugin not in roster:
        return Classification("eligible", f"{service}: {source.plugin} -> {target.plugin}")
    return Classification("partial", f"{service}: an interrupted {source.plugin} -> {target.plugin} write")


def _replace_refusal(transition: Transition, observation: Observation, rendered: str | None) -> str | None:
    source = cast(SourcePlugin, transition.source)
    target = cast(TargetPlugin, transition.target)
    service = cast(str, transition.service)
    roster = observation.roster
    if roster.count(source.plugin) != 1 or roster.count(target.plugin) > 1:
        return "the roster repeats a transition plugin"
    config_refusal = _source_config_refusal(source, observation) or _target_config_refusal(target, observation, rendered)
    if config_refusal is not None:
        return config_refusal
    bindings = (_bound(observation, source.plugin), _bound(observation, target.plugin))
    if target.plugin not in roster:
        return None if bindings == ([service], []) else f"{source.plugin} is not bound to exactly {service}"
    adjacent = roster.index(target.plugin) == roster.index(source.plugin) + 1
    if adjacent and bindings in (([service], []), ([], [service])):
        return None
    return f"{target.plugin} is already in the roster in a state this transition did not write"


def _source_config_refusal(source: SourcePlugin, observation: Observation) -> str | None:
    config = observation.configs.get(source.plugin)
    if config is None or not config.present or config.value is None:
        return f"{source.plugin}.json is missing or unreadable"
    if source.config_match == "exact":
        return None if config.value == source.config else f"{source.plugin}.json was edited (exact predecessor config expected)"
    edited = sorted(key for key, value in source.config.items() if config.value.get(key) != value)
    return None if not edited else f"{source.plugin}.json was edited: {', '.join(edited)}"


def _target_config_refusal(target: TargetPlugin, observation: Observation, rendered: str | None) -> str | None:
    if rendered is None:
        return f"this release cannot render the {target.plugin} config"
    config = observation.configs.get(target.plugin)
    if config is None or not config.present:
        return None
    if config.value is None or config.value != json.loads(rendered):
        return f"{target.plugin}.json exists and differs from the release config"
    return None


def _classify_add(transition: Transition, observation: Observation, rendered: str | None) -> Classification:
    target = cast(TargetPlugin, transition.target)
    service = transition.service
    if target.plugin in observation.roster and (service is None or observation.bindings.get(service) == target.plugin):
        return Classification("done", f"{target.plugin} is in the roster")
    if observation.profile not in transition.profiles:
        return Classification("not_applicable", f"profile {observation.profile!r} is not declared")
    if target.plugin in observation.roster:
        return Classification("conflict", f"{target.plugin} is in the roster but {service} is bound elsewhere")
    if service is not None and service in observation.bindings:
        return Classification("conflict", f"{service} is already bound to {observation.bindings[service]}")
    refusal = _target_config_refusal(target, observation, rendered)
    if refusal is not None:
        return Classification("conflict", refusal)
    return Classification("eligible", f"add {target.plugin}")


def _classify_retire(transition: Transition, observation: Observation) -> Classification:
    source = cast(SourcePlugin, transition.source)
    if source.plugin not in observation.roster:
        return Classification("done", f"{source.plugin} is not in the roster")
    if observation.profile not in transition.profiles:
        return Classification("unsupported", f"profile {observation.profile!r} has no declared {transition.migration_id} transition")
    bound = _bound(observation, source.plugin)
    if bound:
        return Classification("conflict", f"{source.plugin} still serves {', '.join(bound)}")
    refusal = _source_config_refusal(source, observation)
    if refusal is not None:
        return Classification("conflict", refusal)
    return Classification("eligible", f"retire {source.plugin}")


def _bound(observation: Observation, plugin: str) -> list[str]:
    return sorted(service for service, value in observation.bindings.items() if value == plugin)


# --- owner-facing sentences --------------------------------------------------------------

_LEFT_AS_IS = "LM Studio and its models are left exactly as they were"


def owner_sentence(transition: Transition, state: Classification) -> str:
    """The one owner-facing sentence for a transition in ``state``: what is true now, never the finished outcome early.

    Every sentence that still involves the old plugin says the solet still uses it
    (B4: a Samantha owner must not read that summaries left LM Studio), and none
    ever implies LM Studio or its models are removed (rul_ef0363a2).  An open switch
    happens with the next release's update: an update at the release a verified
    solet already runs is ``already_current`` and changes nothing (iss_e3b3e159);
    only an update that left the switch pending is finished by re-running it.
    """
    label = transition.service_label
    replacement = transition.replacement_label
    old = "LM Studio"
    sentences: dict[str, str] = {
        "done": f"{label.capitalize()} run on {replacement}.{' ' + transition.done_note if transition.done_note else ''} {_LEFT_AS_IS}; the solet simply stops using them for {label}.",
        "eligible": f"{label.capitalize()} still use {old}. This Mac supports Apple-native; the solet switches {label} to {replacement} with the next release's update (or when an update that left the switch pending is re-run). {old} stays in use until then.",
        "partial": f"A switch of {label} to {replacement} was interrupted; the next update finishes it, and {label} keep working meanwhile.",
        "conflict": f"{label.capitalize()} stay on {old} because its settings differ from the release defaults ({state.detail}). {_LEFT_AS_IS}.",
        "host_unsupported": f"{state.detail[0].upper()}{state.detail[1:]}. The solet keeps using {old} for {label}; {_LEFT_AS_IS}.",
        "unsupported": f"{label.capitalize()} keep using {old} on this solet's profile; keep {old} installed and running.",
        "not_applicable": f"Nothing to switch: this solet does not use {old} for {label}.",
    }
    return sentences[state.state]
