"""Seed-side ``existing::migration.plugin_transition`` (iss_6d26db73, design sha256 18ed9595).

Applies the release's declared plugin transitions to an existing solet during a
normal ``solet-manager update``.  For each declared transition the handler
observes the managed subset, classifies it (``plugin_transition_declaration``)
and, in the apply phase only:

1. makes the replacement ready -- acquires a missing pinned asset and proves it
   in a bounded child process on the target's own N+1 interpreter;
2. when ready, writes in an order that is bootable at every step: replacement
   config, roster gains the replacement right after the old plugin, the service
   binding moves, the roster drops the old plugin;
3. when not ready, writes nothing (an interrupted write is put back to the
   predecessor values) and defers.

A deferral or a refused conflict returns ``pending`` with ``retry_safe`` and a
repair: the Manager records the row ``deferred`` and publishes
``needs_attention`` after promotion, so the next plain update retries.  The
old plugin, its config and its package are never removed here: the old closure
stays runnable until the replacement is active.  No database, credential,
vault or LaunchAgent is touched, and LM Studio itself is never touched.
"""

from __future__ import annotations

import json
import math
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import cast

import yaml

from .apple_setup_adapter import COREAI_ASSET_MANIFEST, COREAI_ASSET_ROOT, apple_inference_config_text, coreai_config_text, pinned_asset_error
from .existing_install_migrations import blocked, facts_evidence, read_text
from .plugin_transition_declaration import (
    Classification,
    Declaration,
    DeclarationError,
    Observation,
    PluginConfig,
    SourcePlugin,
    TargetPlugin,
    Transition,
    classify,
    owner_sentence,
    parse_declaration,
)
from .profile_identity import PROFILE_TEMPLATE_BY_BUNDLE, ProvenanceError, declared_profile
from .setup_adapter_contract import AdapterRequest, JsonObject, planned_action, result
from .setup_adapter_runtime import Runtime

__all__ = ["DECLARATION_PATH", "migration_plugin_transition", "observe"]

DECLARATION_PATH = Path("plugins/github_midwife_plugin/knowledge_base/plugin_transitions.json")
#: The release's existing-install flow: its ``host_profiles`` are the one place a host threshold is declared.
FLOW_PATH = Path("plugins/github_midwife_plugin/knowledge_base/existing_install_flow.json")
_HOST_VERSION = re.compile(r"^(\d+)(?:\.\d+){0,2}$")
_HOST_MACHINE = re.compile(r"^[a-z0-9_]{1,32}$")
_PROFILE_TEMPLATES = Path("plugins/github_midwife_plugin/knowledge_base/profile_templates")
_MANIFEST = Path("profile/config/manifest.yaml")
_BINDINGS = Path("profile/config/service_bindings.json")
_PLUGIN_CONFIGS = Path("profile/config/plugins")
_VENV_PYTHON = Path(".venv/bin/python3")
# One apply shares the Manager's adapter timeout (request.timeout_seconds): every readiness child gets
# at most what is left after _WRITE_RESERVE_SECONDS, and a spent budget defers the transition
# (retry-safe, old binding kept) instead of letting the Manager's own timeout fail the update
# (iss_5341396d).  A child that times out likewise defers, never fails.
# Children run isolated: no inherited PYTHONPATH, no cwd on sys.path, so nothing in the
# target tree or the parent's environment can shadow the target venv's packages (iss_831383f5).
_ISOLATED = ("-I", "-c")
_ACQUIRE_TIMEOUT_SECONDS = 150
_PROOF_TIMEOUT_SECONDS = 120
_WRITE_RESERVE_SECONDS = 30
_MIN_CHILD_SECONDS = 10
_clock: Callable[[], float] = time.monotonic
_OPEN_STATES = frozenset({"eligible", "partial", "cleanup", "conflict"})
_RENDERERS: dict[str, Callable[[Path], str | None]] = {
    "coreai_embeddings": coreai_config_text,
    "apple_inference": apple_inference_config_text,
}
_ACQUIRE_SCRIPT = (
    "import sys\n"
    "from pathlib import Path\n"
    "from coreai_embeddings_plugin.assets import DistributionManifest, acquire_asset\n"
    "acquire_asset(Path(sys.argv[1]), DistributionManifest.from_file(Path(sys.argv[2])))\n"
)
_PROOF_SCRIPT = (
    "import json, math, sys\n"
    "from pathlib import Path\n"
    "from coreai_embeddings_plugin.runtime import EmbeddingRuntime\n"
    "runtime = EmbeddingRuntime(Path(sys.argv[1]), sys.argv[2])\n"
    "try:\n"
    "    runtime.prepare()\n"
    "    vector = runtime.generate(['solet plugin transition readiness proof'])[0]\n"
    "    unit = str(runtime.diagnostics().get('observed_compute_unit'))\n"
    "finally:\n"
    "    runtime.close()\n"
    "print(json.dumps({'dimension': len(vector), 'norm': math.sqrt(sum(x * x for x in vector)), 'compute_unit': unit}))\n"
)
_IMPORT_SCRIPT = "import importlib, sys\nimportlib.import_module(sys.argv[1] + '.plugin')\n"


class ObservationError(RuntimeError):
    def __init__(self, code: str, repair: str) -> None:
        super().__init__(repair)
        self.code = code
        self.repair = repair


@dataclass(frozen=True, slots=True)
class Readiness:
    ready: bool
    code: str
    detail: str


@dataclass(frozen=True, slots=True)
class HostGate:
    """The measured host against the release's host profiles: ``unmet`` maps each profile it misses to that profile's threshold."""

    version: str | None
    machine: str | None
    unmet: dict[str, tuple[int, str]]


@dataclass(frozen=True, slots=True)
class Deferral:
    migration_id: str
    error_kind: str
    repair: str


# --- entry point -----------------------------------------------------------------


def migration_plugin_transition(request: AdapterRequest, runtime: Runtime) -> JsonObject:
    try:
        declaration = load_declaration(request.target)
        gate = host_gate(declaration, request.target, runtime)
        observation = observe(request.target)
        states = [(item, _classify(item, observation, request.target, gate)) for item in declaration.transitions]
        items = [_summary_evidence(declaration, observation, gate), *(_transition_evidence(item, state) for item, state in states)]
        if request.phase == "probe":
            return _probe_result(request, states, items)
        return _apply(request, runtime, declaration, items, gate)
    except DeclarationError as exc:
        return blocked(request, "plugin_transition_declaration_invalid", f"{exc}. Reinstall the reviewed release, then re-preview.")
    except ObservationError as exc:
        return blocked(request, exc.code, exc.repair)


def load_declaration(target: Path) -> Declaration:
    raw = (target / DECLARATION_PATH).read_bytes()
    declaration = parse_declaration(raw)
    for item in declaration.transitions:
        unknown = [name for name in item.profiles if not (target / _PROFILE_TEMPLATES / f"{name}.yaml").is_file()]
        if unknown:
            raise DeclarationError(f"{item.migration_id}: profiles {unknown} have no template in this release")
    return declaration


# --- host --------------------------------------------------------------------------


def host_gate(declaration: Declaration, target: Path, runtime: Runtime) -> HostGate:
    """Measure the host (sw_vers + uname) once, only when a replacement requires a host profile (rul_385dac24).

    The thresholds come from the release's own flow ``host_profiles``; a host the
    probes cannot read refuses the operation rather than guessing it is eligible.
    """
    required = sorted({name for item in declaration.transitions if (name := _required_profile(item)) is not None})
    if not required:
        return HostGate(None, None, {})
    profiles = _host_profiles(target)
    unknown = [name for name in required if name not in profiles]
    if unknown:
        raise DeclarationError(f"replacements require host profiles {unknown} that the release flow does not declare")
    version = _probe_text(runtime, ("/usr/bin/sw_vers", "-productVersion"), _HOST_VERSION)
    machine = _probe_text(runtime, ("/usr/bin/uname", "-m"), _HOST_MACHINE)
    major = int(version.split(".", 1)[0])
    unmet = {name: profiles[name] for name in required if major < profiles[name][0] or machine != profiles[name][1]}
    return HostGate(version, machine, unmet)


def _required_profile(transition: Transition) -> str | None:
    return None if transition.target is None else transition.target.requires_host


def _host_profiles(target: Path) -> dict[str, tuple[int, str]]:
    try:
        raw: object = json.loads((target / FLOW_PATH).read_text(encoding="utf-8")).get("host_profiles", {})
    except (OSError, json.JSONDecodeError, AttributeError) as exc:
        raise ObservationError("host_profiles_unreadable", f"the release flow's host_profiles are unreadable ({exc}); reinstall the reviewed release, then re-preview.") from exc
    if not isinstance(raw, dict):
        raise ObservationError("host_profiles_unreadable", "the release flow's host_profiles is not an object; reinstall the reviewed release, then re-preview.")
    profiles: dict[str, tuple[int, str]] = {}
    for name, row in cast(dict[str, object], raw).items():
        if not isinstance(row, dict) or not isinstance(row.get("macos_major_min"), int) or not isinstance(row.get("machine"), str):
            raise ObservationError("host_profiles_unreadable", f"host profile {name!r} is malformed; reinstall the reviewed release, then re-preview.")
        values = cast(dict[str, object], row)
        profiles[name] = (cast(int, values["macos_major_min"]), cast(str, values["machine"]))
    return profiles


def _probe_text(runtime: Runtime, argv: tuple[str, ...], shape: re.Pattern[str]) -> str:
    outcome = runtime.run(argv, timeout_seconds=5)
    text = outcome.stdout.strip()
    if not outcome.ok or outcome.timed_out or outcome.stdout_truncated or shape.fullmatch(text) is None:
        raise ObservationError("host_platform_unknown", f"{argv[0]} did not report the host (exit {outcome.returncode}); the transition is not decided on a guess. Re-preview once it answers.")
    return text


# --- observation -----------------------------------------------------------------


def observe(target: Path) -> Observation:
    """Read the managed subset once; a symlinked or unreadable carrier refuses rather than guesses."""
    manifest = _load_mapping(target / _MANIFEST, "profile_manifest_unreadable")
    raw_plugins = manifest.get("plugins")
    if not isinstance(raw_plugins, list) or not all(isinstance(item, str) for item in cast(list[object], raw_plugins)):
        raise ObservationError("profile_manifest_invalid", "profile/config/manifest.yaml has no plugin list; repair it, then re-preview.")
    bindings_raw = _load_json_object(target / _BINDINGS, "service_bindings_unreadable")
    if not all(isinstance(value, str) for value in bindings_raw.values()):
        raise ObservationError("service_bindings_invalid", "profile/config/service_bindings.json binds a non-string value; repair it, then re-preview.")
    label = manifest.get("profile_name")
    manifest_profile = label if isinstance(label, str) else None
    profile, profile_problem = _identify_profile(target, manifest_profile)
    configs = {path.stem: _plugin_config(path) for path in sorted((target / _PLUGIN_CONFIGS).glob("*.json"))}
    return Observation(profile, tuple(cast(list[str], raw_plugins)), cast(dict[str, str], bindings_raw), configs, manifest_profile, profile_problem)


def _identify_profile(target: Path, manifest_profile: str | None) -> tuple[str | None, str | None]:
    """The release profile this solet is, or why it cannot be told (rul_dedd310a: one resolver, no fact-guessing).

    The manifest's ``profile_name`` is only a label (templates renamed it, ``apply_manifest`` resets
    it to ``local``).  The sealed ``PROVENANCE.json`` bundle is the identity; a provenance-less tree
    falls back to the label, but only when it is one of this release's profile names (a yaml in
    profile_templates that is no profile, such as a model catalog, does not count).  An
    unreadable or unknown provenance is never ignored, and neither is a label that names no profile.
    """
    cannot = f"cannot identify this solet's profile (profile_name {manifest_profile!r})"
    try:
        declared = declared_profile(target)
    except ProvenanceError as exc:
        return None, f"{cannot}: {str(exc).partition(': ')[0]}"
    if declared is not None:
        return declared, None
    if manifest_profile in PROFILE_TEMPLATE_BY_BUNDLE.values():
        return manifest_profile, None
    return None, f"{cannot}: it is no release profile and PROVENANCE.json names no bundle"


def _require_regular(path: Path, code: str) -> str:
    if path.is_symlink() or not path.is_file():
        raise ObservationError(code, f"{path} is missing or not a regular file; repair it, then re-preview.")
    text = read_text(path)
    if text is None:
        raise ObservationError(code, f"{path} disappeared while it was read; re-preview.")
    return text


def _load_mapping(path: Path, code: str) -> dict[str, object]:
    try:
        raw: object = yaml.safe_load(_require_regular(path, code))
    except yaml.YAMLError as exc:
        raise ObservationError(code, f"{path} is not valid YAML ({exc}); repair it, then re-preview.") from exc
    if not isinstance(raw, dict):
        raise ObservationError(code, f"{path} is not a mapping; repair it, then re-preview.")
    return cast(dict[str, object], raw)


def _load_json_object(path: Path, code: str) -> dict[str, object]:
    try:
        raw: object = json.loads(_require_regular(path, code))
    except json.JSONDecodeError as exc:
        raise ObservationError(code, f"{path} is not valid JSON ({exc}); repair it, then re-preview.") from exc
    if not isinstance(raw, dict):
        raise ObservationError(code, f"{path} is not a JSON object; repair it, then re-preview.")
    return cast(dict[str, object], raw)


def _plugin_config(path: Path) -> PluginConfig:
    if path.is_symlink() or not path.is_file():
        return PluginConfig(True, None, None)
    text = read_text(path)
    try:
        value: object = json.loads(text or "")
    except json.JSONDecodeError:
        return PluginConfig(True, text, None)
    return PluginConfig(True, text, cast(JsonObject, value) if isinstance(value, dict) else None)


def _rendered(transition: Transition, target: Path) -> str | None:
    return None if transition.target is None else _RENDERERS[transition.target.config_renderer](target)


def _classify(transition: Transition, observation: Observation, target: Path, gate: HostGate) -> Classification:
    """The declared classification, then the host: an open transition this Mac cannot run is a settled keep."""
    state = classify(transition, observation, _rendered(transition, target))
    refusal = _host_refusal(transition, gate)
    if refusal is not None and state.state in {"eligible", "conflict"}:
        return Classification("host_unsupported", refusal, running=state.running)
    return state


def _host_refusal(transition: Transition, gate: HostGate) -> str | None:
    """Why this Mac cannot run the replacement, in the owner's terms; ``None`` when it can (or none is required)."""
    requires = None if transition.target is None else transition.target.requires_host
    if requires is None or requires not in gate.unmet:
        return None
    minimum, machine = gate.unmet[requires]
    return f"this Mac runs macOS {gate.version} ({gate.machine}); {transition.replacement_label} requires macOS {minimum} or later on {machine}"


# --- probe -----------------------------------------------------------------------


def _probe_result(request: AdapterRequest, states: list[tuple[Transition, Classification]], items: list[JsonObject]) -> JsonObject:
    open_states = [(item, state) for item, state in states if state.state in _OPEN_STATES]
    if not open_states:
        return result(request, status="verified", evidence_items=items)
    actions = [action for item, state in open_states for action in _planned_actions(request.target, item, state)]
    return result(request, status="pending", actions=actions, evidence_items=items, repair="Approve the declared plugin transition shown in the preview.")


def _planned_actions(target: Path, transition: Transition, state: Classification) -> list[JsonObject]:
    prefix = f"plugin_transition.{transition.migration_id}"
    evidence_ref = f"plugin_transition.{transition.migration_id}"
    if state.state == "conflict":
        return [planned_action(action_id=f"{prefix}.refuse", title=f"Refuse {transition.migration_id} and keep the current plugin: {state.detail}", mutation_kind="none", target="$TARGET/profile/config", evidence_ref=evidence_ref)]
    if state.state == "cleanup":
        return [planned_action(action_id=f"{prefix}.roster", title=_roster_title(transition, cleanup=True), mutation_kind="file_write", target=str(target / _MANIFEST), evidence_ref=evidence_ref)]
    actions: list[JsonObject] = []
    replacement = transition.target
    # The list must not depend on what apply is about to change: the Manager keys each
    # target's backup by its index here, so a download finished before a crash would
    # otherwise shift every later index and strand the resume without its backups.
    if replacement is not None and replacement.readiness == "coreai_embedding":
        actions.append(planned_action(action_id=f"{prefix}.acquire", title="Make sure the pinned Core AI model asset is present and verified (downloaded only if missing)", mutation_kind="host_provisioning", target=f"$TARGET/{COREAI_ASSET_ROOT}", evidence_ref=evidence_ref))
    if replacement is not None:
        actions.append(planned_action(action_id=f"{prefix}.prove", title=f"Prove {replacement.plugin} works before any config changes", mutation_kind="none", target="$TARGET/.venv", evidence_ref=evidence_ref))
        actions.append(planned_action(action_id=f"{prefix}.config", title=f"Write the {replacement.plugin} config", mutation_kind="file_write", target=str(target / _PLUGIN_CONFIGS / f"{replacement.plugin}.json"), evidence_ref=evidence_ref))
    actions.append(planned_action(action_id=f"{prefix}.roster", title=_roster_title(transition), mutation_kind="file_write", target=str(target / _MANIFEST), evidence_ref=evidence_ref))
    if transition.service is not None:
        actions.append(planned_action(action_id=f"{prefix}.binding", title=f"Bind {transition.service} to {replacement.plugin if replacement else 'nothing'}", mutation_kind="file_write", target=str(target / _BINDINGS), evidence_ref=evidence_ref))
    return actions


def _roster_title(transition: Transition, *, cleanup: bool = False) -> str:
    source = transition.source.plugin if transition.source is not None else None
    replacement = transition.target.plugin if transition.target is not None else None
    if cleanup and source is not None:
        return f"Remove the unused {source} from the plugin roster ({replacement} already serves {transition.service})"
    if source is not None and replacement is not None:
        return f"Replace {source} with {replacement} in the plugin roster"
    return f"Add {replacement} to the plugin roster" if replacement is not None else f"Remove {source} from the plugin roster"


# --- apply -----------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Deadline:
    """The part of one adapter invocation's timeout the readiness children may spend."""

    at: float
    timeout_seconds: int

    @classmethod
    def for_request(cls, request: AdapterRequest) -> Deadline:
        return cls(_clock() + request.timeout_seconds - _WRITE_RESERVE_SECONDS, request.timeout_seconds)

    def child_timeout(self, cap: int) -> int | None:
        """``cap`` or what is left, whichever is less; ``None`` when too little is left to prove anything."""
        remaining = int(self.at - _clock())
        return None if remaining < _MIN_CHILD_SECONDS else min(cap, remaining)

    def exhausted(self, step: str) -> Readiness:
        return Readiness(False, "update_time_budget_spent", f"this update step's {self.timeout_seconds} s budget was spent before {step}")


def _apply(request: AdapterRequest, runtime: Runtime, declaration: Declaration, items: list[JsonObject], gate: HostGate) -> JsonObject:
    deferrals: list[Deferral] = []
    refused: set[tuple[str, str]] = set()
    deadline = Deadline.for_request(request)
    for transition in declaration.transitions:
        state = _classify(transition, observe(request.target), request.target, gate)
        if state.state not in _OPEN_STATES:
            continue
        if state.state == "conflict":
            deferrals.append(Deferral(transition.migration_id, "plugin_transition_conflict", _conflict_repair(transition, state, request.name, repeat=(state.detail, state.repair) in refused)))
            refused.add((state.detail, state.repair))
            continue
        if state.state == "cleanup":
            _roster_remove(request.target, runtime, cast(SourcePlugin, transition.source).plugin)
        else:
            deferred = _switch(transition, request, runtime, gate, deadline, items)
            if deferred is not None:
                deferrals.append(deferred)
                continue
        final = _classify(transition, observe(request.target), request.target, gate)
        if final.state != "done":
            return _write_unverified(request, items, transition, final)
    return _apply_result(request, items, deferrals)


def _apply_result(request: AdapterRequest, items: list[JsonObject], deferrals: list[Deferral]) -> JsonObject:
    if not deferrals:
        return result(request, status="applied", retry_safe=True, evidence_items=items)
    kind = "plugin_transition_pending" if any(item.error_kind == "plugin_transition_pending" for item in deferrals) else "plugin_transition_conflict"
    return result(request, status="pending", error_kind=kind, retry_safe=True, exit_code=None, evidence_items=items, repair=" ".join(item.repair for item in deferrals))


def _switch(transition: Transition, request: AdapterRequest, runtime: Runtime, gate: HostGate, deadline: Deadline, items: list[JsonObject]) -> Deferral | None:
    """Prove the replacement ready, then write the switch; a replacement not ready writes nothing (an interrupted write is put back) and defers."""
    readiness = _make_ready(transition, request, runtime, gate, deadline)
    items.append(facts_evidence(f"plugin_transition.{transition.migration_id}.readiness", {"ready": readiness.ready, "code": readiness.code, "detail": readiness.detail}, verified=readiness.ready))
    if not readiness.ready:
        _revert_partial(transition, request, runtime)
        return Deferral(transition.migration_id, "plugin_transition_pending", _pending_repair(transition, readiness, request.name))
    _write_forward(transition, request, runtime)
    return None


def _write_unverified(request: AdapterRequest, items: list[JsonObject], transition: Transition, final: Classification) -> JsonObject:
    return result(request, status="failed", error_kind="plugin_transition_write_unverified", retry_safe=False, exit_code=None, evidence_items=items, repair=f"{transition.migration_id} wrote its files but reads back as {final.state} ({final.detail}); keep the Manager backups and run `solet-manager doctor {request.name}`.")


def _conflict_repair(transition: Transition, state: Classification, name: str, *, repeat: bool) -> str:
    """The repair for a refused transition; a cause an earlier transition already named is not said twice (the result caps ``repair`` at 512 characters)."""
    if repeat:
        return f"{transition.migration_id} refused for the same reason."
    return f"{transition.migration_id} refused: {state.detail}. The current plugin stays active; {state.repair}, then re-run `solet-manager update {name}`."


def _pending_repair(transition: Transition, readiness: Readiness, name: str) -> str:
    source = transition.source.plugin if transition.source is not None else "the current configuration"
    replacement = cast(TargetPlugin, transition.target).plugin
    return f"{replacement} is not ready ({readiness.code}: {readiness.detail}); {source} stays active. Re-run `solet-manager update {name}` to retry."


# --- readiness -------------------------------------------------------------------


def _make_ready(transition: Transition, request: AdapterRequest, runtime: Runtime, gate: HostGate, deadline: Deadline) -> Readiness:
    replacement = transition.target
    if replacement is None:
        return Readiness(True, "no_replacement", "a retirement needs no replacement")
    refusal = _host_refusal(transition, gate)
    if refusal is not None:
        return Readiness(False, "host_unsupported", refusal)
    python = request.target / _VENV_PYTHON
    if not python.is_file():
        return Readiness(False, "target_python_missing", f"{python} is missing")
    if replacement.readiness == "import":
        return _prove_import(replacement, request, runtime, python, deadline)
    return _prove_coreai(transition, request, runtime, python, deadline)


def _prove_import(replacement: TargetPlugin, request: AdapterRequest, runtime: Runtime, python: Path, deadline: Deadline) -> Readiness:
    timeout = deadline.child_timeout(_PROOF_TIMEOUT_SECONDS)
    if timeout is None:
        return deadline.exhausted(f"proving {replacement.plugin} imports")
    outcome = runtime.run((str(python), *_ISOLATED, _IMPORT_SCRIPT, replacement.plugin), timeout_seconds=timeout, cwd=request.target)
    if outcome.timed_out or not outcome.ok:
        return Readiness(False, "replacement_import_failed", f"{replacement.plugin} does not import in the target environment")
    return Readiness(True, "imported", f"{replacement.plugin} imports")


def _prove_coreai(transition: Transition, request: AdapterRequest, runtime: Runtime, python: Path, deadline: Deadline) -> Readiness:
    target = request.target
    if pinned_asset_error(target) is not None:
        timeout = deadline.child_timeout(_ACQUIRE_TIMEOUT_SECONDS)
        if timeout is None:
            return deadline.exhausted("downloading the pinned Core AI model asset")
        runtime.run((str(python), *_ISOLATED, _ACQUIRE_SCRIPT, str(target / COREAI_ASSET_ROOT), str(target / COREAI_ASSET_MANIFEST)), timeout_seconds=timeout, cwd=target)
        error = pinned_asset_error(target)
        if error is not None:
            return Readiness(False, "replacement_asset_unavailable", error)
    config = json.loads(cast(str, _rendered(transition, target)))
    timeout = deadline.child_timeout(_PROOF_TIMEOUT_SECONDS)
    if timeout is None:
        return deadline.exhausted("the Core AI readiness embedding")
    outcome = runtime.run((str(python), *_ISOLATED, _PROOF_SCRIPT, str(config["asset_root"]), str(config["compute_preference"])), timeout_seconds=timeout, cwd=target)
    if outcome.timed_out or not outcome.ok:
        return Readiness(False, "replacement_embedding_failed", "the Core AI readiness embedding did not complete")
    return _embedding_verdict(outcome.stdout)


def _embedding_verdict(stdout: str) -> Readiness:
    try:
        payload = json.loads(stdout.strip().splitlines()[-1])
        dimension = int(payload["dimension"])
        norm = float(payload["norm"])
    except (IndexError, KeyError, TypeError, ValueError, json.JSONDecodeError):
        return Readiness(False, "replacement_embedding_failed", "the Core AI readiness embedding returned no vector")
    if dimension != 768 or not math.isclose(norm, 1.0, abs_tol=1e-3):
        return Readiness(False, "replacement_embedding_invalid", f"dimension={dimension} norm={norm:.4f}")
    return Readiness(True, "embedded", f"dimension=768 compute_unit={payload.get('compute_unit')}")


# --- writes ----------------------------------------------------------------------


def _write_forward(transition: Transition, request: AdapterRequest, runtime: Runtime) -> None:
    """Replacement config, roster add, binding, roster remove: every intermediate state boots."""
    target = request.target
    source = transition.source
    replacement = transition.target
    if replacement is not None:
        config_path = target / _PLUGIN_CONFIGS / f"{replacement.plugin}.json"
        if not config_path.exists():
            runtime.atomic_write(config_path, cast(str, _rendered(transition, target)), mode=0o600)
        _roster_insert(target, runtime, replacement.plugin, after=None if source is None else source.plugin)
    if transition.service is not None and replacement is not None:
        _set_binding(target, runtime, transition.service, replacement.plugin)
    if source is not None:
        _roster_remove(target, runtime, source.plugin)


def _revert_partial(transition: Transition, request: AdapterRequest, runtime: Runtime) -> None:
    """Put an interrupted replace back to the predecessor values; the old binding boots as before."""
    source = transition.source
    replacement = transition.target
    if transition.kind != "replace" or source is None or replacement is None:
        return
    observation = observe(request.target)
    if replacement.plugin not in observation.roster:
        return
    if transition.service is not None and observation.bindings.get(transition.service) == replacement.plugin:
        _set_binding(request.target, runtime, transition.service, source.plugin)
    _roster_remove(request.target, runtime, replacement.plugin)


def _roster_lines(text: str, plugin: str) -> list[int]:
    pattern = re.compile(rf"^(\s*)-\s+{re.escape(plugin)}\s*$")
    return [index for index, line in enumerate(text.splitlines()) if pattern.match(line)]


def _roster_insert(target: Path, runtime: Runtime, plugin: str, *, after: str | None) -> None:
    path = target / _MANIFEST
    text = _require_regular(path, "profile_manifest_unreadable")
    if _roster_lines(text, plugin):
        return
    lines = text.splitlines(keepends=True)
    anchors = _roster_lines(text, after) if after is not None else _all_roster_lines(text)
    if not anchors or (after is not None and len(anchors) != 1):
        raise ObservationError("profile_manifest_unwritable", f"cannot locate the roster entry to place {plugin} after; repair profile/config/manifest.yaml")
    anchor = anchors[-1]
    indent = re.match(r"^(\s*)-", lines[anchor])
    entry = f"{indent.group(1) if indent else ''}- {plugin}\n"
    _write_manifest(path, runtime, [*lines[: anchor + 1], entry, *lines[anchor + 1 :]])


def _roster_remove(target: Path, runtime: Runtime, plugin: str) -> None:
    path = target / _MANIFEST
    text = _require_regular(path, "profile_manifest_unreadable")
    matches = _roster_lines(text, plugin)
    if not matches:
        return
    if len(matches) != 1:
        raise ObservationError("profile_manifest_unwritable", f"{plugin} appears more than once in profile/config/manifest.yaml")
    lines = text.splitlines(keepends=True)
    _write_manifest(path, runtime, [line for index, line in enumerate(lines) if index != matches[0]])


def _all_roster_lines(text: str) -> list[int]:
    return [index for index, line in enumerate(text.splitlines()) if re.match(r"^\s*-\s+[a-z][a-z0-9_]*\s*$", line)]


def _write_manifest(path: Path, runtime: Runtime, lines: list[str]) -> None:
    content = "".join(lines)
    if not isinstance(yaml.safe_load(content), dict):
        raise ObservationError("profile_manifest_unwritable", "the rewritten roster does not parse; nothing was written")
    runtime.atomic_write(path, content, mode=_mode(path))


def _set_binding(target: Path, runtime: Runtime, service: str, plugin: str) -> None:
    path = target / _BINDINGS
    bindings = _load_json_object(path, "service_bindings_unreadable")
    if bindings.get(service) == plugin:
        return
    bindings[service] = plugin
    runtime.atomic_write(path, json.dumps(bindings, indent=2) + "\n", mode=_mode(path))


def _mode(path: Path) -> int:
    return path.stat().st_mode & 0o777


# --- evidence --------------------------------------------------------------------


def _summary_evidence(declaration: Declaration, observation: Observation, gate: HostGate) -> JsonObject:
    facts: dict[str, str | int | bool] = {"declaration_sha256": declaration.sha256, "profile": observation.profile or "unknown", "manifest_profile_name": observation.manifest_profile or "unknown", "transitions": len(declaration.transitions)}
    if gate.version is not None:
        facts["host_macos"] = gate.version
    return facts_evidence("plugin_transitions", facts, verified=True)


def _transition_evidence(transition: Transition, state: Classification) -> JsonObject:
    """One evidence row per transition; its summary is the owner-facing sentence for exactly this state."""
    facts: dict[str, str | int | bool] = {"migration_id": transition.migration_id, "kind": transition.kind, "state": state.state, "detail": state.detail[:200]}
    if isinstance(transition.source, SourcePlugin):
        facts["from"] = transition.source.plugin
    if isinstance(transition.target, TargetPlugin):
        facts["to"] = transition.target.plugin
    item = facts_evidence(f"plugin_transition.{transition.migration_id}", facts, verified=state.state not in _OPEN_STATES)
    item["summary"] = owner_sentence(transition, state)[:512]
    return item
