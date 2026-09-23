#!/usr/bin/env python3
"""B1 §3 real-snapshot preflight smoke (no pytest) — producer + derive + gate.

Round-2 rework. Proves the §3 schema-preflight loop is "green for the RIGHT
reason" — the round-1 smoke used a producer that snapshotted LIVE source and a
gate that fetched its own old side, so it hid the round-2 gaps. This proves:

1. DRIFT-GUARD — the collector's inlined ``_reduce_to_snapshot`` (which must
   import only ``ananta.*`` to run against an old tree) stays byte-identical to
   ``schema_preflight.schemas_to_snapshot`` (the classifier-side reduce).
2. REAL PRODUCER ROUND-TRIP (B1·2 + B1·3) — a REAL ``ReleaseManager`` CoW-clones
   the repo and runs the REAL producer against the FROZEN candidate clone; the
   persisted ``VERSION`` snapshot reads back via ``current_schema_snapshot()``.
   The build only SUCCEEDS if the collector's provenance assert
   (``EXPECT_ROOT = staging/code``) passed — i.e. every plugin loaded from the
   CANDIDATE clone, NOT live source. That success IS the "from the candidate
   tree, not source" proof.
3. FAIL-CLOSED ASSERTS (Gap-2) — ``_assert_completeness`` /``_assert_provenance``
   raise on an under-collected / mis-provenanced tree (unit-level, the exact
   fail-closed mechanism the live subprocess relies on).
4. PURE GATE (all six cells, incl. the round-2 defensive cell) — present+present
   non-additive → REFUSE; candidate=None+release-exists → FAIL-CLOSED; current=
   None+release-exists → FAIL-CLOSED (the derive-returned-None guard); the
   bootstrap cells allow.
5. BASELINE DERIVE (B1·1) — the orchestrator's ``_resolve_current_snapshot``
   DERIVES the old snapshot from ``current/code`` when the current ``VERSION``
   has none, and FAILS CLOSED (raises) when the old tree can't be introspected;
   it never calls the collector when the current snapshot is already present.

Requires ``SOLET_NAME`` + ``APP_HOME`` (the collector gates discovery by the
profile manifest). Runs against a throwaway ``~/.ananta`` scratch root — NEVER
the live ledger. Run:
    SOLET_NAME=<name> APP_HOME=<repo>/profile .venv/bin/python3 \\
        plugins/macos_self_deployment_plugin/tests/schema_preflight_realsnapshot_smoke.py
"""

from __future__ import annotations

import copy
import os
import shutil
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

_PLUGIN_SRC = Path(__file__).resolve().parents[1] / "src"
_REPO_ROOT = Path(__file__).resolve().parents[3]
for _p in (str(_PLUGIN_SRC), str(_REPO_ROOT / "ananta" / "src")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from _release_manager_smoke_support import (  # noqa: E402
    SmokeRecorder,
    make_manager,
    scratch_root,
)
from ananta.types.column_types import ColumnType  # noqa: E402
from ananta.types.schema_types import (  # noqa: E402
    ColumnDefinition,
    SchemaDefinition,
    TableSchema,
)
from macos_self_deployment_plugin.plugin import MacosSelfDeploymentPlugin  # noqa: E402
from macos_self_deployment_plugin.release_manager import (  # noqa: E402
    CandidatePaths,
    ReleaseManagerError,
)
from macos_self_deployment_plugin.schema_preflight import (  # noqa: E402
    KIND_CANDIDATE_SNAPSHOT_MISSING,
    KIND_COLUMN_REMOVED,
    KIND_CURRENT_SNAPSHOT_UNRESOLVED,
    classify_snapshot_diff,
    schemas_to_snapshot,
)
from macos_self_deployment_plugin.schema_snapshot_collector import (  # noqa: E402
    _assert_completeness,
    _assert_provenance,
    _reduce_to_snapshot,
)
from macos_self_deployment_plugin.schema_snapshot_producer import (  # noqa: E402
    build_schema_snapshot_fn,
)
from macos_self_deployment_plugin.swap_orchestrator import (  # noqa: E402
    _resolve_current_snapshot,
)

_Snapshot = dict[str, dict[str, dict[str, dict[str, object]]]]


def _candidate(schema_snapshot: _Snapshot | None, *, code_root: Path | None = None) -> CandidatePaths:
    base = Path("/nonexistent/rel-b1")
    return CandidatePaths(
        release_id="rel-b1", release_dir=base,
        code_root=code_root if code_root is not None else base / "code",
        venv_python=base / "venv" / "bin" / "python3", version_file=base / "VERSION",
        missing_pth_targets=(), schema_snapshot=cast("dict[str, object] | None", schema_snapshot),
    )


def _drop_one_column(snapshot: _Snapshot) -> tuple[_Snapshot, str]:
    """Return a deep copy with one real column dropped (a genuine non-additive diff)."""
    variant = copy.deepcopy(snapshot)
    for tables in variant.values():
        for columns in tables.values():
            if len(columns) >= 2:
                victim = sorted(columns)[0]
                del columns[victim]
                return variant, victim
    msg = "no table with >=2 columns in the real snapshot — cannot derive a drop"
    raise AssertionError(msg)


def _make_plugin() -> MacosSelfDeploymentPlugin:
    plugin = MacosSelfDeploymentPlugin()
    plugin._solet_name = "smoke"  # noqa: SLF001
    return plugin


def _preflight(
    plugin: MacosSelfDeploymentPlugin, candidate_snapshot: _Snapshot | None,
    current_snapshot: _Snapshot | None, current_release_exists: bool,
) -> Any:
    """Call the pure gate, casting the nested snapshot dicts at the trust boundary."""
    return plugin._schema_preflight(  # noqa: SLF001
        _candidate(candidate_snapshot),
        current_snapshot=cast("dict[str, object] | None", current_snapshot),
        current_release_exists=current_release_exists,
    )


class _FakeRM:
    """Minimal ReleaseManager stand-in for the baseline-derive scenarios.

    Carries only the surface ``_resolve_current_snapshot`` reads:
    ``current_release`` / ``current_schema_snapshot`` / ``candidate_for``.
    """

    def __init__(
        self, *, current_snapshot: _Snapshot | None, current_release: str | None,
        old_code_root: Path | None = None,
    ) -> None:
        self._current_snapshot = current_snapshot
        self._current_release = current_release
        self._old_code_root = old_code_root

    @property
    def current_release(self) -> str | None:
        return self._current_release

    def current_schema_snapshot(self) -> dict[str, object] | None:
        return cast("dict[str, object] | None", self._current_snapshot)

    def candidate_for(self, release_id: str) -> CandidatePaths:
        del release_id
        if self._old_code_root is None:
            raise NotImplementedError("candidate_for unused in this scenario")
        return _candidate(None, code_root=self._old_code_root)


class _ModuleProbe:
    """A stand-in whose module file is under ``tests/`` — used as an off-root plugin."""


def test_drift_guard(rec: SmokeRecorder) -> None:
    print("1: drift-guard — collector inline reduce == schema_preflight.schemas_to_snapshot")
    schema = SchemaDefinition(
        namespace="demo_plugin",
        tables={
            "widget": TableSchema(
                table_name="widget",
                columns={
                    "id": ColumnDefinition(type=ColumnType.INTEGER, primary_key=True, not_null=True),
                    "label": ColumnDefinition(type=ColumnType.TEXT, not_null=True),
                    "weight": ColumnDefinition(type=ColumnType.INTEGER, default="0"),
                    "sku": ColumnDefinition(type=ColumnType.TEXT, unique=True),
                },
            ),
        },
    )
    canonical = schemas_to_snapshot({"demo_plugin": schema})
    inlined = _reduce_to_snapshot({"demo_plugin": schema})
    rec.check(inlined == canonical, "collector reduce is byte-identical to the classifier reduce")


def test_fail_closed_asserts(rec: SmokeRecorder) -> None:
    print("3: fail-closed asserts (Gap-2) — completeness + provenance raise on violation")
    try:
        _assert_completeness(cast("Any", SimpleNamespace(plugins={})), {"ghost_plugin"})
        rec.check(False, "completeness raises when a manifest plugin is undiscovered")
    except ValueError as exc:
        rec.check("ghost_plugin" in str(exc), "completeness names the missing plugin")
    # load-everything (manifest None) → nothing to satisfy → no raise.
    _assert_completeness(cast("Any", SimpleNamespace(plugins={})), None)
    rec.check(True, "completeness is a no-op when manifest gating is off (None)")
    # provenance: a plugin whose module file is OUTSIDE expect_root → raise.
    fake_plugin = _ModuleProbe()  # __module__ == this smoke file, under tests/
    try:
        _assert_provenance(
            cast("Any", SimpleNamespace(plugins={"x": fake_plugin})),
            Path("/nonexistent/expect-root"),
        )
        rec.check(False, "provenance raises when a module loads from outside EXPECT_ROOT")
    except ValueError as exc:
        rec.check("provenance violation" in str(exc), "provenance reports the violation")


def test_real_producer_round_trip(rec: SmokeRecorder, scratch: Path) -> _Snapshot:
    print("2: REAL producer round-trip — build_candidate(real fn) → VERSION → read back")
    releases_root = scratch / "releases"
    rm = make_manager(_REPO_ROOT, releases_root)
    snapshot_fn = build_schema_snapshot_fn(
        solet_name=os.environ["SOLET_NAME"],
        app_home=Path(os.environ["APP_HOME"]),
        source_root=_REPO_ROOT,
    )
    candidate = rm.build_candidate(schema_snapshot_fn=snapshot_fn)
    # The build only SUCCEEDS if the collector's provenance assert
    # (EXPECT_ROOT = staging/code) passed — i.e. ananta + every plugin loaded
    # from the CANDIDATE clone, NOT live source. So a non-empty snapshot here is,
    # by construction, a snapshot of the candidate tree (the round-2 B1·2 fix).
    snap = cast("_Snapshot | None", candidate.schema_snapshot)
    rec.check(snap is not None and len(snap) >= 1, "candidate VERSION carries a non-empty real snapshot")
    rec.check(snap is not None and "core" in snap, "snapshot includes the 'core' namespace")
    rec.check(
        snap is not None and any(ns != "core" for ns in snap),
        "snapshot includes plugin namespaces (plugin schemas collected from the clone)",
    )
    rec.check(
        str(candidate.code_root).startswith(str(releases_root)),
        "the snapshotted candidate code_root is the scratch clone, NOT live source (REPO_ROOT)",
    )
    rm.cutover(candidate)
    persisted = cast("_Snapshot | None", rm.current_schema_snapshot())
    rec.check(persisted == snap, "current_schema_snapshot() reads back the persisted VERSION snapshot")
    assert snap is not None
    return snap


def test_pure_gate(rec: SmokeRecorder, real: _Snapshot) -> _Snapshot:
    print("4: the pure fail-closed gate — six cells incl. the round-2 defensive cell")
    plugin = _make_plugin()
    variant, dropped = _drop_one_column(real)
    verdict = classify_snapshot_diff(real, variant)
    rec.check(not verdict.is_additive, f"dropping column {dropped!r} classifies non-additive")
    rec.check(
        any(c.kind == KIND_COLUMN_REMOVED for c in verdict.breaking_changes),
        "the non-additive diff is a column_removed change",
    )
    # present + present, non-additive → REFUSE.
    v = _preflight(plugin, variant, real, current_release_exists=True)
    rec.check(not v.is_additive, "present+present non-additive → REFUSE")
    # present + present, identical → additive.
    v = _preflight(plugin, real, real, current_release_exists=True)
    rec.check(v.is_additive, "present+present identical → additive allow")
    # candidate=None + release exists → FAIL CLOSED (producer failed).
    v = _preflight(plugin, None, real, current_release_exists=True)
    rec.check(
        not v.is_additive and any(c.kind == KIND_CANDIDATE_SNAPSHOT_MISSING for c in v.breaking_changes),
        "candidate=None + current-release-exists → FAIL-CLOSED (candidate_schema_snapshot_missing)",
    )
    # candidate=None + no current release → baseline allow.
    v = _preflight(plugin, None, None, current_release_exists=False)
    rec.check(v.is_additive, "candidate=None + no current release → baseline allow")
    # candidate=present + current=None + no current release → true bootstrap allow.
    v = _preflight(plugin, real, None, current_release_exists=False)
    rec.check(v.is_additive, "candidate=present + no current release → bootstrap allow")
    # candidate=present + current=None + release EXISTS → FAIL CLOSED (derive returned None).
    v = _preflight(plugin, real, None, current_release_exists=True)
    rec.check(
        not v.is_additive and any(c.kind == KIND_CURRENT_SNAPSHOT_UNRESOLVED for c in v.breaking_changes),
        "candidate=present + current=None + release-exists → FAIL-CLOSED (current_snapshot_unresolved)",
    )
    return variant


def test_baseline_derive(rec: SmokeRecorder, real: _Snapshot) -> None:
    print("5: baseline derive (B1·1) — derive old snapshot from current/code; fail-closed")
    snapshot_fn = build_schema_snapshot_fn(
        solet_name=os.environ["SOLET_NAME"],
        app_home=Path(os.environ["APP_HOME"]),
        source_root=_REPO_ROOT,
    )
    # happy: current snapshot None + current release exists → DERIVE from old code_root.
    rm = _FakeRM(current_snapshot=None, current_release="rel-old", old_code_root=_REPO_ROOT)
    derived = cast("_Snapshot | None", _resolve_current_snapshot(cast("Any", rm), snapshot_fn))
    rec.check(derived is not None and "core" in derived, "derive produced a real snapshot from current/code")
    # fail-closed: old tree cannot be introspected → snapshot_fn raises → propagates.
    rm_bad = _FakeRM(current_snapshot=None, current_release="rel-old", old_code_root=Path("/nonexistent/old/code"))
    try:
        _resolve_current_snapshot(cast("Any", rm_bad), snapshot_fn)
        rec.check(False, "derive against an un-introspectable old tree FAILS CLOSED")
    except ReleaseManagerError:
        rec.check(True, "derive against an un-introspectable old tree raises ReleaseManagerError")
    # present current snapshot → NEVER calls the collector (no derive).
    def _tripwire(_code_root: Path) -> dict[str, object]:
        msg = "collector must not run when the current snapshot is already present"
        raise AssertionError(msg)

    rm_present = _FakeRM(current_snapshot=real, current_release="rel-x")
    resolved = cast("_Snapshot | None", _resolve_current_snapshot(cast("Any", rm_present), _tripwire))
    rec.check(resolved == real, "a present current snapshot is returned WITHOUT deriving")


def main() -> int:
    print("=== schema_preflight_realsnapshot_smoke (B1 round-2) ===")
    os.environ.setdefault("APP_HOME", str(_REPO_ROOT / "profile"))
    rec = SmokeRecorder()
    scratch = scratch_root("realsnapshot")
    scratch.mkdir(parents=True, exist_ok=True)
    try:
        test_drift_guard(rec)
        test_fail_closed_asserts(rec)
        real = test_real_producer_round_trip(rec, scratch)
        test_pure_gate(rec, real)
        test_baseline_derive(rec, real)
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    return rec.report("schema_preflight_realsnapshot_smoke")


if __name__ == "__main__":
    sys.exit(main())
