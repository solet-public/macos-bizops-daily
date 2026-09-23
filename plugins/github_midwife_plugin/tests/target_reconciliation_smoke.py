#!/usr/bin/env python3
"""T3 closed-protocol smoke for ``reconcile::runtime.cutover``.

Every refusal here happens before any byte of a target changes, so the smoke
needs no target: it drives the validator and the topology reader directly.

Legs, and the mutation each one reds on:

===============================  ===================================================
Leg                              Red mutation it catches
===============================  ===================================================
closed field set                 accept unknown keys / tolerate a missing one
flow + operation pinned          let the setup flow id through
vintage refusal                  accept an unknown launch_topology at parse time
sibling target refused           compare declared paths instead of resolved ones
label derived, not trusted       accept the manager's label verbatim
topology per vintage             collapse the two vintages into one branch
unknown topology reported        raise instead of returning the refusal token
payload claim is total           claim malformed JSON as a reconciliation payload
===============================  ===================================================

The sibling-target leg is the one that matters most: the blast radius of
getting it wrong is another solet's live process, which is exactly what the
design's name-keyed scoping exists to prevent.
"""

from __future__ import annotations

import json
import plistlib
import shutil
import sys
import uuid
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[3]
_SELF_DEPLOYMENT_PLUGIN_SRC = _ROOT / "plugins" / "macos_self_deployment_plugin" / "src"
sys.path.insert(0, str(_ROOT / "ananta" / "src"))
if _SELF_DEPLOYMENT_PLUGIN_SRC.is_dir():
    sys.path.insert(0, str(_SELF_DEPLOYMENT_PLUGIN_SRC))
sys.path.insert(
    0, str(Path(__file__).resolve().parents[1] / "src"),
)

from github_midwife_plugin.target_reconciliation import (  # noqa: E402
    FLOW_ID,
    OPERATION_REF,
    LaunchTopology,
    TargetReconciliationError,
    TargetReconciliationRequest,
    detect_topology,
    dispatch_reconciliation,
    is_reconciliation_payload,
    plist_sha256,
)

_SOURCE = "sha256:" + "a" * 64
_RELEASE = "sha256:" + "b" * 64
_PLIST = "sha256:" + "c" * 64


def _bundle_plugin_skip(plugin_root: Path, plugin: str) -> str | None:
    """Return an explicit skip only for a sealed bundle omitting ``plugin``."""
    if plugin_root.is_dir():
        return None
    provenance_path = _ROOT / "PROVENANCE.json"
    if not provenance_path.is_file():
        raise AssertionError(f"red: {plugin} is absent outside a sealed bundle")
    provenance = json.loads(provenance_path.read_text(encoding="utf-8"))
    bundle = provenance.get("bundle")
    profile = bundle.get("name") if isinstance(bundle, dict) else None
    if not isinstance(profile, str) or not profile:
        raise AssertionError("red: sealed bundle has no valid PROVENANCE.json bundle.name")
    return f"SKIP  {plugin} not in bundle {profile}; target-local adapter leg is not shipped"


def _payload(**overrides: object) -> dict[str, object]:
    raw: dict[str, object] = {
        "schema_version": 1,
        "flow_id": FLOW_ID,
        "operation_ref": OPERATION_REF,
        "phase": "apply",
        "name": "smoke-target",
        "target_realpath": "/absolute/smoke-target",
        "reconciliation_id": "rec_smoke",
        "approved_fingerprint": "sha256:" + "d" * 64,
        "expected_source_surface_sha256": _SOURCE,
        "expected_release_surface_sha256": _RELEASE,
        "expected_manifest_etag": "etag-1",
        "expected_current_release_id": "rel-1",
        "expected_active_instance_id": "inst-1",
        "expected_active_start_token": "tok-1",
        "launch_topology": LaunchTopology.MATERIALIZED_SUPERVISOR,
        "expected_launchagent_label": "local.solet.smoke-target",
        "expected_launchagent_plist_sha256": _PLIST,
        "verification_modules": ["macos_self_deployment_plugin.plugin"],
        "timeout_seconds": 300,
    }
    raw.update(overrides)
    return raw


def _refusal_kind(raw: dict[str, object]) -> str:
    try:
        TargetReconciliationRequest.from_dict(raw)
    except TargetReconciliationError as exc:
        return exc.error_kind
    return "accepted"


def _protocol_checks() -> list[tuple[bool, str]]:
    checks = [
        (_refusal_kind(_payload()) == "accepted", "a well-formed request parses"),
    ]
    unknown = _payload()
    unknown["extra_field"] = "x"
    checks.append(
        (_refusal_kind(unknown) == "adapter_protocol_error", "an unknown key is refused"),
    )
    for key in sorted(_payload()):
        missing = {k: v for k, v in _payload().items() if k != key}
        checks.append(
            (
                _refusal_kind(missing) == "adapter_protocol_error",
                f"a request missing {key} is refused",
            ),
        )
    bad = (
        ({"flow_id": "macos.repository_setup"}, "adapter_protocol_error", "the setup flow id"),
        ({"operation_ref": "genesis::solet.run"}, "adapter_protocol_error", "a setup operation ref"),
        ({"schema_version": 2}, "adapter_protocol_error", "an unknown schema version"),
        ({"phase": "destroy"}, "adapter_protocol_error", "an unknown phase"),
        ({"launch_topology": "guessing"}, "unsupported_cutover_vintage", "an unknown vintage"),
        ({"timeout_seconds": 0}, "adapter_protocol_error", "a non-positive timeout"),
        ({"timeout_seconds": True}, "adapter_protocol_error", "a boolean timeout"),
        ({"verification_modules": "a.b"}, "adapter_protocol_error", "a non-list module set"),
        ({"verification_modules": ["a.b", "a.b"]}, "adapter_protocol_error", "duplicate modules"),
        ({"verification_modules": ["not a module"]}, "adapter_protocol_error", "a non-module name"),
        ({"name": ""}, "adapter_protocol_error", "an empty target name"),
    )
    checks.extend(
        (_refusal_kind(_payload(**override)) == kind, f"{label} is refused as {kind}")
        for override, kind, label in bad
    )
    return checks


def _identity_checks(root: Path) -> list[tuple[bool, str]]:
    mine = root / "mine"
    sibling = root / "sibling"
    for path in (mine, sibling):
        path.mkdir(parents=True, exist_ok=True)
    own = TargetReconciliationRequest.from_dict(
        _payload(target_realpath=str(mine)),
    )
    foreign = TargetReconciliationRequest.from_dict(
        _payload(target_realpath=str(sibling)),
    )
    relative = TargetReconciliationRequest.from_dict(
        _payload(target_realpath="relative/path"),
    )
    checks: list[tuple[bool, str]] = []
    try:
        own.require_is_own_target(mine)
        checks.append((True, "a request naming this installation is accepted"))
    except TargetReconciliationError:
        checks.append((False, "a request naming this installation is accepted"))
    for request, label in ((foreign, "a sibling target"), (relative, "a relative target path")):
        try:
            request.require_is_own_target(mine)
            checks.append((False, f"{label} is refused"))
        except TargetReconciliationError as exc:
            checks.append((exc.error_kind == "target_identity_mismatch", f"{label} is refused"))
    own_plist = _write_plist(
        mine / "own.plist",
        [str(mine / ".venv" / "bin" / "python3"), "-m", "ananta.cli"],
        label="local.solet.smoke-target",
    )
    good_label = TargetReconciliationRequest.from_dict(_payload(
        target_realpath=str(mine),
        expected_launchagent_plist_sha256=plist_sha256(own_plist),
    ))
    bad_label = TargetReconciliationRequest.from_dict(
        _payload(
            target_realpath=str(mine),
            expected_launchagent_label="local.solet.other",
            expected_launchagent_plist_sha256=plist_sha256(own_plist),
        ),
    )
    try:
        good_label.require_local_identity(mine, own_plist)
        checks.append((True, "the installed target label and launch scope are accepted"))
    except TargetReconciliationError:
        checks.append((False, "the installed target label and launch scope are accepted"))
    try:
        bad_label.require_local_identity(mine, own_plist)
        checks.append((False, "a label naming another solet is refused"))
    except TargetReconciliationError as exc:
        checks.append(
            (exc.error_kind == "launchagent_label_mismatch", "a label naming another solet is refused"),
        )
    return checks


def _write_plist(path: Path, arguments: list[str], *, label: str = "local.solet.smoke") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as stream:
        plistlib.dump({"Label": label, "ProgramArguments": arguments}, stream)
    return path


def _topology_checks(root: Path) -> list[tuple[bool, str]]:
    legacy = _write_plist(
        root / "legacy.plist",
        ["/Users/x/target/.venv/bin/python3", "-m", "ananta.cli", "--app-home", "/Users/x/target"],
    )
    materialized = _write_plist(
        root / "materialized.plist",
        ["/Users/x/target/releases/current/venv/bin/python3", "-m", "ananta.supervisor"],
    )
    stranger = _write_plist(root / "stranger.plist", ["/usr/bin/true"])
    relative = _write_plist(root / "relative.plist", ["python3", "-m", "ananta.cli"])
    empty = _write_plist(root / "empty.plist", [])
    return [
        (
            detect_topology(legacy) == LaunchTopology.LEGACY_DIRECT,
            "a direct ananta.cli launch reads as legacy_direct",
        ),
        (
            detect_topology(materialized) == LaunchTopology.MATERIALIZED_SUPERVISOR,
            "a releases/current launch reads as materialized_supervisor",
        ),
        (
            detect_topology(stranger) == LaunchTopology.UNSUPPORTED,
            "an unrecognized launch reads as unsupported, without raising",
        ),
        (
            detect_topology(relative) == LaunchTopology.UNSUPPORTED,
            "a relative interpreter reads as unsupported",
        ),
        (
            detect_topology(empty) == LaunchTopology.UNSUPPORTED,
            "an empty ProgramArguments reads as unsupported",
        ),
        (
            detect_topology(root / "absent.plist") == LaunchTopology.UNSUPPORTED,
            "a missing plist reads as unsupported, not as a crash",
        ),
    ]


def _payload_claim_checks() -> list[tuple[bool, str]]:
    return [
        (is_reconciliation_payload('{"flow_id": "macos.target_reconciliation"}'), "claims its own flow"),
        (
            not is_reconciliation_payload('{"flow_id": "macos.repository_setup"}'),
            "does not claim the setup flow",
        ),
        (not is_reconciliation_payload("{not json"), "does not claim malformed JSON"),
        (not is_reconciliation_payload("[]"), "does not claim a non-object payload"),
        (not is_reconciliation_payload("{}"), "does not claim a payload with no flow id"),
    ]


class _ExplodingInvoker:
    """Reaching this IS the failure on every refusal and on every probe."""

    def __call__(self, _: TargetReconciliationRequest) -> dict[str, object]:
        raise AssertionError("cutover invoked despite a refusal or a probe")


def _dispatch_checks(root: Path) -> list[tuple[bool, str]]:
    target = root / "dispatch-target"
    target.mkdir(parents=True, exist_ok=True)
    materialized = _write_plist(
        root / "d-materialized.plist",
        [str(target / "releases/current/venv/bin/python3"), "-m", "ananta.supervisor"],
        label="local.solet.dispatch-target",
    )
    legacy = _write_plist(
        root / "d-legacy.plist",
        [str(target / ".venv/bin/python3"), "-m", "ananta.cli"],
        label="local.solet.dispatch-target",
    )
    stranger = _write_plist(
        root / "d-stranger.plist",
        [str(target / "bin" / "unknown-launcher")],
        label="local.solet.dispatch-target",
    )
    wrong_label = _write_plist(
        root / "d-wrong-label.plist",
        [str(target / "releases/current/venv/bin/python3"), "-m", "ananta.supervisor"],
        label="local.solet.other",
    )

    def payload(plist: Path = materialized, **overrides: object) -> str:
        base: dict[str, object] = {
            "name": "dispatch-target",
            "target_realpath": str(target),
            "expected_launchagent_label": "local.solet.dispatch-target",
            "expected_launchagent_plist_sha256": plist_sha256(plist),
        }
        base.update(overrides)
        return json.dumps(_payload(**base))

    def kind(raw: str, plist: Path, invoker: object) -> str:
        try:
            dispatch_reconciliation(
                raw, installed_root=target, plist_path=plist, invoke_cutover=invoker,  # type: ignore[arg-type]
            )
        except TargetReconciliationError as exc:
            return exc.error_kind
        except AssertionError:
            return "invoked_anyway"
        return "returned"

    recorded: list[TargetReconciliationRequest] = []

    def recording(request: TargetReconciliationRequest) -> dict[str, object]:
        recorded.append(request)
        return {"status": "queued"}

    def failing(_: TargetReconciliationRequest) -> dict[str, object]:
        raise RuntimeError("router unreachable")

    probe_result = dispatch_reconciliation(
        payload(phase="probe"),
        installed_root=target,
        plist_path=materialized,
        invoke_cutover=_ExplodingInvoker(),  # type: ignore[arg-type]
    )
    apply_result = dispatch_reconciliation(
        payload(),
        installed_root=target,
        plist_path=materialized,
        invoke_cutover=recording,
    )
    original_materialized_payload = payload()
    _write_plist(
        materialized,
        [str(target / "releases/current/venv/bin/python3"), "-m", "ananta.supervisor", "--changed"],
        label="local.solet.dispatch-target",
    )
    return [
        (probe_result["status"] == "probed" and probe_result["mutated"] is False,
         "a probe returns findings and has no path to the cutover"),
        (apply_result["status"] == "invoked" and len(recorded) == 1,
         "an apply on an agreed vintage reaches the cutover exactly once"),
        (apply_result["observed_launch_topology"] == LaunchTopology.MATERIALIZED_SUPERVISOR,
         "the observed vintage is reported, not the claimed one"),
        (kind(payload(stranger), stranger, _ExplodingInvoker()) == "unsupported_cutover_vintage",
         "an unsupported installed vintage refuses before the cutover"),
        (kind(payload(legacy), legacy, _ExplodingInvoker()) == "topology_disagreement",
         "manager/adapter vintage disagreement refuses before the cutover"),
        (kind(payload(materialized, target_realpath=str(root / "elsewhere")), materialized, _ExplodingInvoker())
         == "target_identity_mismatch",
         "a request for another target refuses before the cutover"),
        (kind(original_materialized_payload, materialized, _ExplodingInvoker()) == "launchagent_plist_drift",
         "a same-topology plist mutation refuses before the cutover"),
        (kind(payload(wrong_label), wrong_label, _ExplodingInvoker()) == "launchagent_label_mismatch",
         "an installed plist label naming another solet refuses before the cutover"),
        (kind(payload(materialized), materialized, failing) == "cutover_failed",
         "a cutover failure passes through as cutover_failed"),
    ]


def _adapter_resolver_checks() -> list[tuple[bool, str]]:
    """The executable adapter must reach the refreshed factory, not a stub."""
    skip = _bundle_plugin_skip(_SELF_DEPLOYMENT_PLUGIN_SRC, "macos_self_deployment_plugin")
    if skip is not None:
        print(skip)
        return []
    from github_midwife_plugin import setup_adapter  # noqa: PLC0415
    from macos_self_deployment_plugin import target_local_cutover  # noqa: PLC0415

    request = TargetReconciliationRequest.from_dict(_payload())
    seen: list[TargetReconciliationRequest] = []
    original = target_local_cutover.run_target_local_cutover

    def factory(candidate: object) -> dict[str, object]:
        if not isinstance(candidate, TargetReconciliationRequest):
            raise AssertionError("adapter did not pass a typed reconciliation request")
        seen.append(candidate)
        return {"status": "queued", "factory": "target-local"}

    target_local_cutover.run_target_local_cutover = factory
    try:
        result = setup_adapter._resolve_cutover_invoker()(request)
    finally:
        target_local_cutover.run_target_local_cutover = original
    return [
        (
            seen == [request] and result == {"status": "queued", "factory": "target-local"},
            "adapter resolver invokes the refreshed target-local construction seam",
        ),
    ]


def main() -> int:
    root = Path("~/.ananta/releases").expanduser() / f"tgt-recon-smoke-{uuid.uuid4().hex[:8]}"
    root.mkdir(parents=True, exist_ok=True)
    try:
        checks = [
            *_protocol_checks(),
            *_identity_checks(root),
            *_topology_checks(root),
            *_payload_claim_checks(),
            *_dispatch_checks(root),
            *_adapter_resolver_checks(),
        ]
    finally:
        shutil.rmtree(root, ignore_errors=True)
    for passed, label in checks:
        print(f"{'PASS' if passed else 'FAIL'} {label}")
    return 0 if all(passed for passed, _ in checks) else 1


if __name__ == "__main__":
    raise SystemExit(main())
