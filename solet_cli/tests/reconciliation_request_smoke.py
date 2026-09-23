"""The Manager-produced reconciliation envelope is the seed's exact wire table (design section 7.2, D4/D7).

Legs: ``frozenset(envelope) == _REQUEST_KEYS`` byte-for-byte; the seed's own
``TargetReconciliationRequest.from_json`` accepts it; no ``CutoverTerms``-only
member (``active_color``, ``adapter_module_sha256``, ``adapter_module_replaced``)
is on the wire; the ``rec_`` id grammar and the sorted ``verification_modules``
are enforced; every phase in the closed set builds and any other phase is
refused; the seed's ``dispatch_reconciliation`` on a real fixture plist returns
``probed``/``mutated=false`` for the probe phase and refuses a changed plist
digest and a disagreeing topology before any byte changes; the response parser
classifies ``ok`` and ``blocked`` envelopes and rejects a malformed reply.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Callable
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import cast

_ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(_ROOT / "solet_cli" / "src"), str(_ROOT / "solet_setup_contracts" / "src"), str(_ROOT / "plugins" / "github_midwife_plugin" / "src")]
from github_midwife_plugin import target_reconciliation as seed  # noqa: E402
from github_midwife_plugin.autostart import render_launchagent_plist  # noqa: E402
from solet_manager.cutover_receipts import CutoverTerms  # noqa: E402
from solet_manager.errors import AdapterProtocolError, StateConflictError  # noqa: E402
from solet_manager.launch_topology import derive_launch_topology, plist_sha256  # noqa: E402
from solet_manager.reconciliation_request import (  # noqa: E402
    RECONCILIATION_WIRE_KEYS,
    TERMS_ONLY_MEMBERS,
    build_reconciliation_envelope,
    parse_reconciliation_response,
)

_CHECKS = 0


def _check(condition: object, label: str) -> None:
    global _CHECKS
    _CHECKS += 1
    if not condition:
        raise AssertionError(label)


def _terms(plist_digest: str, topology: str = "legacy_direct") -> CutoverTerms:
    return CutoverTerms(
        current_release_id="rel_baseline",
        active_color="blue",
        active_instance_id="inst_1",
        active_start_token="Mon Sep 18 10:00:00 2026",
        manifest_etag="etag-1",
        launch_topology=topology,
        launchagent_label="local.solet.fixture",
        launchagent_plist_sha256=plist_digest,
        adapter_module_sha256="sha256:" + "a" * 64,
        adapter_module_replaced=False,
        source_surface_sha256="sha256:" + "b" * 64,
        release_surface_sha256="sha256:" + "c" * 64,
        verification_modules=("ananta.cli", "macos_self_deployment_plugin.plugin"),
    )


def _envelope(terms: CutoverTerms, target: Path, phase: str = "probe") -> dict[str, object]:
    return dict(build_reconciliation_envelope(phase=phase, name="fixture", target_realpath=str(target), reconciliation_id="rec_" + "1" * 32, approved_fingerprint="sha256:" + "d" * 64, terms=terms, timeout_seconds=30))


def _expect(kind: type[Exception], action: Callable[[], object], label: str) -> Exception:
    try:
        action()
    except kind as exc:
        _check(True, label)
        return exc
    raise AssertionError(f"{label}: no {kind.__name__} raised")


def _check_envelopes(terms: CutoverTerms, target: Path, digest: str) -> None:
    for phase in ("probe", "apply", "recover"):
        envelope = _envelope(terms, target, phase)
        _check(frozenset(envelope) == RECONCILIATION_WIRE_KEYS == seed._REQUEST_KEYS, f"{phase}: envelope keys equal the seed wire table byte-for-byte")  # noqa: SLF001
        _check(not (TERMS_ONLY_MEMBERS & frozenset(envelope)), f"{phase}: no CutoverTerms-only member on the wire")
        parsed = seed.TargetReconciliationRequest.from_json(json.dumps(envelope, sort_keys=True))
        observed = (parsed.phase, parsed.verification_modules, parsed.expected_launchagent_plist_sha256)
        _check(observed == (phase, terms.verification_modules, digest), f"{phase}: seed parses the Manager envelope")
    _check(len(RECONCILIATION_WIRE_KEYS) == 19, "nineteen wire fields")


def _check_builder_refusals(terms: CutoverTerms, target: Path, digest: str) -> None:
    for bad in ({"phase": "verify"}, {"reconciliation_id": "abc"}, {"approved_fingerprint": "nope"}, {"timeout_seconds": 0}):
        kwargs = {"phase": "probe", "name": "fixture", "target_realpath": str(target), "reconciliation_id": "rec_" + "1" * 32, "approved_fingerprint": "sha256:" + "d" * 64, "terms": terms, "timeout_seconds": 30, **bad}
        _expect(StateConflictError, lambda: build_reconciliation_envelope(**kwargs), f"refused {bad}")  # type: ignore[arg-type]  # noqa: B023
    _expect(StateConflictError, lambda: _terms(digest, "unknown_vintage"), "unsupported topology refused at the terms")


def _check_seed_dispatch(terms: CutoverTerms, target: Path, plist_path: Path, digest: str) -> dict[str, object]:
    invoked: list[object] = []

    def dispatch(envelope: dict[str, object]) -> dict[str, object]:
        return seed.dispatch_reconciliation(json.dumps(envelope), installed_root=target, plist_path=plist_path, invoke_cutover=lambda request: invoked.append(request) or {})

    probe = dispatch(_envelope(terms, target, "probe"))
    _check((probe["status"], probe["mutated"], invoked) == ("probed", False, []), "seed probe phase is read-only")
    _check((probe["observed_launchagent_plist_sha256"], probe["observed_launch_topology"]) == (digest, "legacy_direct"), "seed measures, never echoes")
    drifted = _envelope(_terms("sha256:" + "0" * 64), target, "apply")
    exc = _expect(seed.TargetReconciliationError, lambda: dispatch(drifted), "plist drift refused")
    _check((cast(seed.TargetReconciliationError, exc).error_kind, invoked) == ("launchagent_plist_drift", []), "changed plist digest refused before any spawn")
    disagreeing = _envelope(_terms(digest, "materialized_supervisor"), target, "apply")
    exc = _expect(seed.TargetReconciliationError, lambda: dispatch(disagreeing), "topology disagreement refused")
    _check((cast(seed.TargetReconciliationError, exc).error_kind, invoked) == ("topology_disagreement", []), "Manager topology disagreeing with the seed is refused before any byte")
    return probe


def _check_response_parsing(probe: dict[str, object]) -> None:
    parsed_ok = parse_reconciliation_response(json.dumps({"schema_version": 1, "flow_id": seed.FLOW_ID, "status": "ok", "result": probe}))
    _check((parsed_ok.status, parsed_ok.probed, parsed_ok.mutated, parsed_ok.observed_launch_topology) == ("ok", True, False, "legacy_direct"), "ok reply parsed")
    blocked = parse_reconciliation_response(json.dumps({"schema_version": 1, "flow_id": seed.FLOW_ID, "status": "blocked", "error_kind": "launchagent_plist_drift", "message": "x"}))
    _check((blocked.refused, blocked.error_kind) == (True, "launchagent_plist_drift"), "blocked reply parsed as data")
    for malformed in ("{}", "[]", json.dumps({"schema_version": 1, "flow_id": "other", "status": "ok"}), json.dumps({"schema_version": 1, "flow_id": seed.FLOW_ID, "status": "weird"})):
        _expect(AdapterProtocolError, lambda: parse_reconciliation_response(malformed), "malformed reply refused")  # noqa: B023


def main() -> int:
    with TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        target = root / "target"
        (target / "plugins" / "github_midwife_plugin" / "src" / "github_midwife_plugin").mkdir(parents=True)
        home = root / "home"
        (home / "Library" / "LaunchAgents").mkdir(parents=True)
        plist_path = home / "Library" / "LaunchAgents" / "local.solet.fixture.plist"
        plist_path.write_bytes(render_launchagent_plist("fixture", target, home, template_text=None, stamp=None, stamped=True))
        raw = plist_path.read_bytes()
        digest = plist_sha256(raw)
        _check(derive_launch_topology(raw) == seed.detect_topology(plist_path) == "legacy_direct", "Manager topology derivation mirrors the seed's")
        terms = _terms(digest)
        _check_envelopes(terms, target, digest)
        _check_builder_refusals(terms, target, digest)
        probe = _check_seed_dispatch(terms, target, plist_path, digest)
        _check_response_parsing(probe)
    print(f"reconciliation_request_smoke OK: {_CHECKS} checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
