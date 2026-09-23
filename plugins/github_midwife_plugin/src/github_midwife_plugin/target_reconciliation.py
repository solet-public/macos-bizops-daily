"""Closed target-local protocol for ``reconcile::runtime.cutover``.

This is a TRANSPORT SHIM and nothing more.  It reuses the setup adapter's
subprocess executable — a target-local process running from the target's own
frozen environment is exactly the locality this operation needs — but it does
NOT reuse the setup flow identity.  Pretending a live cutover is a setup
operation would erase an authority boundary: setup runs against a target being
born, this runs against a target that is serving traffic.

The blue-green mechanics are not here and must never arrive here.  This module
validates a closed request, decides which launch vintage it is looking at, and
hands a typed input to
``macos_self_deployment_plugin.reconciliation_cutover.ReconciliationCutoverController``.
Review fails if a colour token, router call, ``launchctl`` invocation, or
release-symlink mutation appears in this file.

The manager supplies identities and expected hashes only.  Every operational
path is derived here, from this installation's own root, and compared for
equality — so a manager that has drifted cannot talk this adapter into acting
on a target it was not approved for.
"""

from __future__ import annotations

import hashlib
import json
import plistlib
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Final

__all__ = [
    "FLOW_ID",
    "OPERATION_REF",
    "LaunchTopology",
    "TargetReconciliationError",
    "TargetReconciliationRequest",
    "detect_topology",
    "dispatch_reconciliation",
    "plist_label",
    "plist_sha256",
    "installed_target_root",
    "launchagent_plist_path",
    "reconciliation_envelope",
    "is_reconciliation_payload",
]

FLOW_ID: Final[str] = "macos.target_reconciliation"
OPERATION_REF: Final[str] = "reconcile::runtime.cutover"
SCHEMA_VERSION: Final[int] = 1

_PHASES: Final[frozenset[str]] = frozenset({"probe", "apply", "recover"})

#: Closed v1 field set, codex design §3.2.  Membership is exact: an unknown key
#: is a refusal, not an ignored extra.  A manager that sends a field this
#: adapter does not understand has a different idea of the operation than the
#: adapter does, and the safe reading of that disagreement is "stop".
_REQUEST_KEYS: Final[frozenset[str]] = frozenset({
    "schema_version",
    "flow_id",
    "operation_ref",
    "phase",
    "name",
    "target_realpath",
    "reconciliation_id",
    "approved_fingerprint",
    "expected_source_surface_sha256",
    "expected_release_surface_sha256",
    "expected_manifest_etag",
    "expected_current_release_id",
    "expected_active_instance_id",
    "expected_active_start_token",
    "launch_topology",
    "expected_launchagent_label",
    "expected_launchagent_plist_sha256",
    "verification_modules",
    "timeout_seconds",
})


class LaunchTopology:
    """The two vintages this channel knows how to reach, plus the refusal.

    b15's stranded-target class spans both, so a design that handled only the
    materialized shape would be unable to repair the targets that motivated it.
    """

    LEGACY_DIRECT: Final[str] = "legacy_direct"
    MATERIALIZED_SUPERVISOR: Final[str] = "materialized_supervisor"
    UNSUPPORTED: Final[str] = "unsupported_launch_topology"

    SUPPORTED: Final[frozenset[str]] = frozenset({LEGACY_DIRECT, MATERIALIZED_SUPERVISOR})


class TargetReconciliationError(Exception):
    """A closed-protocol refusal, carrying the machine-readable kind.

    ``error_kind`` is the field the manager branches on; the message is for a
    human reading a receipt.  Every refusal raised from this module happens
    before any byte of the target changes.
    """

    def __init__(self, error_kind: str, message: str) -> None:
        super().__init__(message)
        self.error_kind = error_kind


@dataclass(frozen=True, slots=True)
class TargetReconciliationRequest:
    """The validated closed request — codex §3.2, exactly these fields."""

    schema_version: int
    flow_id: str
    operation_ref: str
    phase: str
    name: str
    target_realpath: str
    reconciliation_id: str
    approved_fingerprint: str
    expected_source_surface_sha256: str
    expected_release_surface_sha256: str
    expected_manifest_etag: str
    expected_current_release_id: str
    expected_active_instance_id: str
    expected_active_start_token: str
    launch_topology: str
    expected_launchagent_label: str
    expected_launchagent_plist_sha256: str
    verification_modules: tuple[str, ...]
    timeout_seconds: int

    @classmethod
    def from_json(cls, raw_text: str) -> TargetReconciliationRequest:
        try:
            raw: object = json.loads(raw_text)
        except json.JSONDecodeError as exc:
            raise TargetReconciliationError(
                "adapter_protocol_error", "stdin must contain exactly one JSON object",
            ) from exc
        if not isinstance(raw, dict):
            raise TargetReconciliationError(
                "adapter_protocol_error", "request must be a JSON object",
            )
        return cls.from_dict(raw)

    @classmethod
    def from_dict(cls, raw: dict[str, object]) -> TargetReconciliationRequest:
        present = frozenset(raw)
        if present != _REQUEST_KEYS:
            missing = sorted(_REQUEST_KEYS - present)
            unknown = sorted(present - _REQUEST_KEYS)
            raise TargetReconciliationError(
                "adapter_protocol_error",
                f"request does not match the closed v1 field set "
                f"(missing={missing}, unknown={unknown})",
            )
        _require_equals(raw, "schema_version", SCHEMA_VERSION)
        _require_equals(raw, "flow_id", FLOW_ID)
        _require_equals(raw, "operation_ref", OPERATION_REF)
        phase = _require_text(raw, "phase")
        if phase not in _PHASES:
            raise TargetReconciliationError(
                "adapter_protocol_error", f"phase must be one of {sorted(_PHASES)}: {phase!r}",
            )
        topology = _require_text(raw, "launch_topology")
        if topology not in LaunchTopology.SUPPORTED:
            raise TargetReconciliationError(
                "unsupported_cutover_vintage",
                f"launch_topology must be one of {sorted(LaunchTopology.SUPPORTED)}: {topology!r}",
            )
        return cls(
            schema_version=SCHEMA_VERSION,
            flow_id=FLOW_ID,
            operation_ref=OPERATION_REF,
            phase=phase,
            name=_require_text(raw, "name"),
            target_realpath=_require_text(raw, "target_realpath"),
            reconciliation_id=_require_text(raw, "reconciliation_id"),
            approved_fingerprint=_require_text(raw, "approved_fingerprint"),
            expected_source_surface_sha256=_require_text(raw, "expected_source_surface_sha256"),
            expected_release_surface_sha256=_require_text(raw, "expected_release_surface_sha256"),
            expected_manifest_etag=_require_text(raw, "expected_manifest_etag"),
            expected_current_release_id=_require_text(raw, "expected_current_release_id"),
            expected_active_instance_id=_require_text(raw, "expected_active_instance_id"),
            expected_active_start_token=_require_text(raw, "expected_active_start_token"),
            launch_topology=topology,
            expected_launchagent_label=_require_text(raw, "expected_launchagent_label"),
            expected_launchagent_plist_sha256=_require_text(
                raw, "expected_launchagent_plist_sha256",
            ),
            verification_modules=_require_module_list(raw, "verification_modules"),
            timeout_seconds=_require_positive_int(raw, "timeout_seconds"),
        )

    def require_is_own_target(self, installed_root: Path) -> None:
        """Refuse a request naming a target that is not this installation.

        The single most important check in this module.  The adapter runs
        inside exactly one target; a request addressed to a sibling must not be
        executed here even if every hash in it is valid, because the blast
        radius of getting this wrong is another solet's live process.
        """
        declared = Path(self.target_realpath)
        if not declared.is_absolute():
            raise TargetReconciliationError(
                "target_identity_mismatch",
                f"target_realpath must be absolute: {self.target_realpath!r}",
            )
        actual = installed_root.resolve()
        if declared.resolve() != actual:
            raise TargetReconciliationError(
                "target_identity_mismatch",
                f"request names {declared} but this adapter is installed at {actual}",
            )

    def require_local_identity(self, installed_root: Path, plist_path: Path) -> None:
        """Prove the plist is THIS installation's, from target-local facts.

        The earlier version compared the manager's ``expected_launchagent_label``
        against a label derived from the manager's own ``name`` — two request
        fields confirming each other, which proves only that the manager is
        self-consistent. A manager that is confidently wrong passes that check.

        Here the authorities are local: the plist's OWN ``Label``, and its own
        ``ProgramArguments`` resolving inside this installation's root. The
        request is then checked against those, not against itself.
        """
        actual_label = plist_label(plist_path)
        if not actual_label:
            raise TargetReconciliationError(
                "launchagent_label_unreadable",
                f"installed plist declares no Label: {plist_path}",
            )
        if actual_label != self.expected_launchagent_label:
            raise TargetReconciliationError(
                "launchagent_label_mismatch",
                f"request expects {self.expected_launchagent_label!r} but the installed "
                f"plist declares {actual_label!r}",
            )
        if actual_label != f"local.solet.{self.name}":
            raise TargetReconciliationError(
                "launchagent_label_mismatch",
                f"installed plist Label {actual_label!r} does not name target {self.name!r}",
            )
        self._require_plist_belongs_to(installed_root, plist_path)

    @staticmethod
    def _require_plist_belongs_to(installed_root: Path, plist_path: Path) -> None:
        """The plist must actually launch something inside this target.

        A correct Label on a plist that launches a SIBLING solet would
        otherwise satisfy every name check while pointing the cutover at
        another installation — the blast-radius failure the design scopes
        against by construction.
        """
        arguments = _program_arguments(plist_path)
        root = installed_root.resolve()
        if not arguments:
            raise TargetReconciliationError(
                "launchagent_scope_mismatch",
                f"installed plist has no ProgramArguments to attribute: {plist_path}",
            )
        if not any(_argument_within(argument, root) for argument in arguments):
            raise TargetReconciliationError(
                "launchagent_scope_mismatch",
                f"installed plist at {plist_path} launches nothing inside {root}",
            )

    def require_plist_unchanged(self, plist_path: Path) -> None:
        """Compare-and-swap on the approval-bound plist digest, measured here."""
        observed = plist_sha256(plist_path)
        if observed != self.expected_launchagent_plist_sha256:
            raise TargetReconciliationError(
                "launchagent_plist_drift",
                f"plist digest changed since approval: expected "
                f"{self.expected_launchagent_plist_sha256}, measured {observed}",
            )


def _argument_within(argument: str, root: Path) -> bool:
    candidate = Path(argument)
    if not candidate.is_absolute():
        return False
    try:
        return candidate.resolve().is_relative_to(root)
    except OSError:
        return str(candidate).startswith(f"{root}/")


def is_reconciliation_payload(raw_text: str) -> bool:
    """Whether this stdin payload declares the reconciliation flow.

    Deliberately total: a payload that is not parseable JSON, or carries no
    ``flow_id``, is simply "not mine" — the setup path owns the decision about
    whether it is malformed.  Claiming a payload in order to reject it would
    turn every setup protocol error into a reconciliation error.
    """
    try:
        raw: object = json.loads(raw_text)
    except json.JSONDecodeError:
        return False
    return isinstance(raw, dict) and raw.get("flow_id") == FLOW_ID


def detect_topology(plist_path: Path) -> str:
    """Derive the launch vintage from this installation's own plist.

    The manager derives the same value independently from the registry-owned
    plist and the two must agree; disagreement is the manager's refusal, not
    this adapter's.  What this side owns is an honest reading of what is
    actually installed here.

    Returns :data:`LaunchTopology.UNSUPPORTED` rather than raising, so the
    caller decides whether an unknown vintage is a refusal (apply) or simply a
    reported fact (probe).
    """
    arguments = _program_arguments(plist_path)
    if not arguments:
        return LaunchTopology.UNSUPPORTED
    interpreter = Path(arguments[0])
    if not interpreter.is_absolute():
        return LaunchTopology.UNSUPPORTED
    if _runs_from_current_release(interpreter, arguments):
        return LaunchTopology.MATERIALIZED_SUPERVISOR
    if _runs_ananta_cli_directly(arguments):
        return LaunchTopology.LEGACY_DIRECT
    return LaunchTopology.UNSUPPORTED


def _runs_from_current_release(interpreter: Path, arguments: list[str]) -> bool:
    """A supervisor whose interpreter resolves beneath ``releases/current``.

    Checked on the UNRESOLVED path: ``releases/current`` is a symlink that
    points at whichever release is live right now, and resolving it first would
    turn every materialized target into an unrecognizable absolute release path.
    The symlink's presence in the launch arguments IS the vintage signal.
    """
    parts = interpreter.parts
    if "releases" in parts and "current" in parts:
        return True
    return any("releases/current" in argument for argument in arguments)


def _runs_ananta_cli_directly(arguments: list[str]) -> bool:
    return "-m" in arguments and "ananta.cli" in arguments


def plist_sha256(plist_path: Path) -> str:
    """``sha256:<hex>`` over the plist's own bytes, read locally.

    The manager ASSERTS this digest at approval time; this side MEASURES it.
    An asserted-but-unmeasured field is the shape that lets a same-topology
    plist mutation through: topology answers "what kind of launch is this",
    never "is it still the launch that was approved".
    """
    digest = hashlib.sha256()
    with plist_path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return f"sha256:{digest.hexdigest()}"


def plist_label(plist_path: Path) -> str:
    """The Label the installed plist declares for itself, or empty."""
    parsed = _parsed_plist(plist_path)
    label = parsed.get("Label")
    return label if isinstance(label, str) else ""


def _parsed_plist(plist_path: Path) -> dict[str, object]:
    try:
        with plist_path.open("rb") as stream:
            parsed = plistlib.load(stream)
    except (OSError, plistlib.InvalidFileException):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _program_arguments(plist_path: Path) -> list[str]:
    parsed = _parsed_plist(plist_path)
    arguments = parsed.get("ProgramArguments")
    if not isinstance(arguments, list):
        return []
    return [argument for argument in arguments if isinstance(argument, str)]


def _require_equals(raw: dict[str, object], key: str, expected: object) -> None:
    if raw[key] != expected:
        raise TargetReconciliationError(
            "adapter_protocol_error", f"{key} must be {expected!r}: {raw[key]!r}",
        )


def _require_text(raw: dict[str, object], key: str) -> str:
    value = raw[key]
    if not isinstance(value, str) or not value:
        raise TargetReconciliationError(
            "adapter_protocol_error", f"{key} must be a non-empty string: {value!r}",
        )
    return value


def _require_positive_int(raw: dict[str, object], key: str) -> int:
    value = raw[key]
    # bool is an int subclass; True would otherwise pass as a timeout of 1.
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise TargetReconciliationError(
            "adapter_protocol_error", f"{key} must be a positive integer: {value!r}",
        )
    return value


def _require_module_list(raw: dict[str, object], key: str) -> tuple[str, ...]:
    value = raw[key]
    if not isinstance(value, list):
        raise TargetReconciliationError(
            "adapter_protocol_error", f"{key} must be a list: {value!r}",
        )
    modules: list[str] = []
    for element in value:
        if not isinstance(element, str) or not element.replace(".", "").isidentifier():
            raise TargetReconciliationError(
                "adapter_protocol_error",
                f"{key} must contain qualified module names: {element!r}",
            )
        modules.append(element)
    if len(set(modules)) != len(modules):
        raise TargetReconciliationError(
            "adapter_protocol_error", f"{key} must not contain duplicates",
        )
    return tuple(modules)


#: The seam through which the refreshed adapter reaches the cutover controller.
#: Injected rather than imported at module scope so this shim stays importable
#: (and testable) on a host where the self-deployment plugin is absent — and so
#: a smoke can prove the transport refusals WITHOUT a cutover implementation
#: present at all.
CutoverInvoker = Callable[[TargetReconciliationRequest], dict[str, object]]


def dispatch_reconciliation(
    raw_text: str,
    *,
    installed_root: Path,
    plist_path: Path,
    invoke_cutover: CutoverInvoker,
) -> dict[str, object]:
    """Validate, scope, and hand off one reconciliation request.

    The ordering is the safety property, not an implementation detail.  Parse,
    then prove the request is addressed to THIS installation, then prove the
    label is this target's own, then agree on the launch vintage — and only
    then invoke the cutover.  Every refusal above is reached before the
    invoker is called, so a refused request cannot have changed a byte.

    ``probe`` stops after the read-only findings by construction: the design's
    probe result is a no-mutation plan, and the only way to guarantee that is
    for the probe branch to have no path to the invoker at all.

    A failure raised by the cutover itself is passed through as
    ``cutover_failed`` with its text intact rather than being reclassified —
    the manager's independent verification, not this shim's opinion, decides
    what the target is actually serving now.
    """
    request = TargetReconciliationRequest.from_json(raw_text)
    request.require_is_own_target(installed_root)
    request.require_local_identity(installed_root, plist_path)
    request.require_plist_unchanged(plist_path)
    observed = detect_topology(plist_path)
    if observed == LaunchTopology.UNSUPPORTED:
        raise TargetReconciliationError(
            "unsupported_cutover_vintage",
            f"installed launch topology at {plist_path} is not a supported vintage",
        )
    if observed != request.launch_topology:
        raise TargetReconciliationError(
            "topology_disagreement",
            f"manager derived {request.launch_topology!r} but this installation reads "
            f"{observed!r}; refusing before any byte changes",
        )
    findings: dict[str, object] = {
        "operation_ref": OPERATION_REF,
        "phase": request.phase,
        "name": request.name,
        "reconciliation_id": request.reconciliation_id,
        # Echoed so the receipt records WHICH approval this evidence belongs to.
        # The manager remains the authority that validates the whole
        # fingerprint; this side must still say which one it acted under, or
        # the evidence cannot be attributed to an approval at all.
        "approved_fingerprint": request.approved_fingerprint,
        "observed_launch_topology": observed,
        # MEASURED here, not echoed from the request. Reporting the expected
        # value under an "observed" key would make the evidence agree with the
        # approval by construction, which is worth nothing.
        "observed_launchagent_plist_sha256": plist_sha256(plist_path),
    }
    if request.phase == "probe":
        return {**findings, "status": "probed", "mutated": False}
    try:
        return {**findings, "status": "invoked", "cutover": invoke_cutover(request)}
    except (OSError, RuntimeError) as exc:
        raise TargetReconciliationError(
            "cutover_failed", f"cutover controller failed: {exc}",
        ) from exc


def installed_target_root() -> Path:
    """This adapter's own installation root, derived from its own location.

    The adapter module lives at
    ``<target>/plugins/github_midwife_plugin/src/github_midwife_plugin/…``, so
    the target root is four parents up from the package directory.  Derived
    rather than accepted from the request on purpose: a manager that has
    drifted must not be able to name the tree this process will act on.
    """
    return Path(__file__).resolve().parents[4]


def launchagent_plist_path(home: Path, name: str) -> Path:
    """The one plist this operation may read, keyed by the target's own name."""
    return home / "Library" / "LaunchAgents" / f"local.solet.{name}.plist"


def reconciliation_envelope(raw_text: str, *, home: Path, invoke_cutover: CutoverInvoker) -> dict[str, object]:
    """Run one reconciliation request and shape the closed result envelope.

    Refusals become a ``blocked`` envelope carrying the machine-readable
    ``error_kind``; they are never raised past this boundary, because the
    manager's contract is one JSON result on stdout, not a traceback on stderr.

    The target root and plist path are derived here, from this installation and
    the request's own name, so the only thing the manager influences is WHICH
    named target it is addressing — and that is checked for equality against
    this installation before anything else happens.
    """
    installed_root = installed_target_root()
    try:
        request = TargetReconciliationRequest.from_json(raw_text)
        plist_path = launchagent_plist_path(home, request.name)
        return {
            "schema_version": SCHEMA_VERSION,
            "flow_id": FLOW_ID,
            "status": "ok",
            "result": dispatch_reconciliation(
                raw_text,
                installed_root=installed_root,
                plist_path=plist_path,
                invoke_cutover=invoke_cutover,
            ),
        }
    except TargetReconciliationError as exc:
        return {
            "schema_version": SCHEMA_VERSION,
            "flow_id": FLOW_ID,
            "status": "blocked",
            "error_kind": exc.error_kind,
            "message": str(exc),
        }
