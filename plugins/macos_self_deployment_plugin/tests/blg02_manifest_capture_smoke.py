#!/usr/bin/env python3
"""BLG-02 per-release manifest capture smoke (no pytest).

The §3 derive path snapshots an OLD release's ``code/`` to build the OLD side of
the schema diff. Before this fix the collector always read the LIVE manifest at
``APP_HOME`` — but ``apply_manifest`` commits the INCOMING deploy's manifest
before delegating a restart, so by derive time the live manifest describes the
NEW profile, not the old tree. A deploy that GROWS the manifest therefore tripped
``_assert_completeness`` on a plugin the old tree never had and false-REFUSED a
perfectly safe deploy.

The fix captures each release's own plugin set into its ``VERSION`` at build
time and has the derive pass that set to the collector as
``MANIFEST_PLUGIN_OVERRIDE``.

This smoke is written to fail if the fix is reverted, not merely to pass while
it is present — see check group 6, which asserts the OLD behaviour still raises
on the same inputs. A green here without that group would be green for the wrong
reason: every other group would also pass against a collector that ignored the
override entirely, because they would all be reading the same live manifest.

Needs no live profile and no database: the live-manifest leg stubs the loader
and asserts DELEGATION, and the producer leg fakes the subprocess. ``APP_HOME``
is honoured if exported but is not required — ``run_smokes`` does not set it,
and a smoke that bails without it is a smoke that silently never runs. Run:
    .venv/bin/python3 \\
        plugins/macos_self_deployment_plugin/tests/blg02_manifest_capture_smoke.py
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

_PLUGIN_SRC = Path(__file__).resolve().parents[1] / "src"
_REPO_ROOT = Path(__file__).resolve().parents[3]
for _p in (str(_PLUGIN_SRC), str(_REPO_ROOT / "ananta" / "src")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import macos_self_deployment_plugin.schema_snapshot_collector as collector_mod  # noqa: E402
from _release_manager_smoke_support import SmokeRecorder  # noqa: E402
from macos_self_deployment_plugin.release_manager import (  # noqa: E402
    CandidatePaths,
    ReleaseBuilder,
    ReleaseManagerError,
    _read_manifest_plugins,
)
from macos_self_deployment_plugin.schema_preflight import (  # noqa: E402
    classify_snapshot_diff,
)
from macos_self_deployment_plugin.schema_snapshot_collector import (  # noqa: E402
    _assert_completeness,
    _resolve_manifest,
)
from macos_self_deployment_plugin.schema_snapshot_producer import (  # noqa: E402
    build_schema_snapshot_fn,
)
from macos_self_deployment_plugin.swap_orchestrator import (  # noqa: E402
    _resolve_current_snapshot,
)

# The old tree shipped two plugins; the incoming deploy adds a third. This is
# the manifest-GROW shape that produced the false-REFUSE.
_OLD_PLUGINS = ("alpha_plugin", "beta_plugin")
_LIVE_PLUGINS = ("alpha_plugin", "beta_plugin", "gamma_plugin")


def _candidate(
    *, manifest_plugins: tuple[str, ...] | None, schema_snapshot: dict[str, object] | None = None
) -> CandidatePaths:
    base = Path("/nonexistent/rel-blg02")
    return CandidatePaths(
        release_id="rel-blg02",
        release_dir=base,
        code_root=base / "code",
        venv_python=base / "venv" / "bin" / "python3",
        version_file=base / "VERSION",
        missing_pth_targets=(),
        schema_snapshot=schema_snapshot,
        manifest_plugins=manifest_plugins,
    )


def _fake_release_manager(old: CandidatePaths) -> Any:
    """A release manager whose CURRENT release has no persisted snapshot.

    That is the only state in which the derive runs at all, so it is the only
    state in which this defect is reachable.
    """
    return SimpleNamespace(
        current_release="rel-blg02",
        current_schema_snapshot=lambda: None,
        candidate_for=lambda _release_id: old,
    )


def _recording_snapshot_fn() -> tuple[Any, list[tuple[Path, tuple[str, ...] | None]]]:
    """A collector stand-in that records exactly what the derive handed it."""
    calls: list[tuple[Path, tuple[str, ...] | None]] = []

    def _fn(
        code_root: Path, manifest_override: tuple[str, ...] | None = None
    ) -> dict[str, object]:
        calls.append((code_root, manifest_override))
        return {"core": {}}

    return _fn, calls


def _check_derive_handoff(rec: SmokeRecorder) -> None:
    print("1: the derive hands the collector the OLD release's captured set")
    fn, calls = _recording_snapshot_fn()
    _resolve_current_snapshot(
        (_fake_release_manager(_candidate(manifest_plugins=_OLD_PLUGINS))), fn
    )
    rec.check(len(calls) == 1, "derive invoked the collector exactly once")
    rec.check(
        calls and calls[0][1] == _OLD_PLUGINS,
        f"override is the OLD captured set, not the live one (got {calls[0][1] if calls else None})",
    )


def _check_fallback_and_shortcircuit(rec: SmokeRecorder) -> None:
    print("2: a release with NO captured set falls back to today's behaviour")
    fn2, calls2 = _recording_snapshot_fn()
    _resolve_current_snapshot(
        (_fake_release_manager(_candidate(manifest_plugins=None))), fn2
    )
    rec.check(
        calls2 and calls2[0][1] is None,
        "no captured set → override is None → collector reads the live manifest",
    )

    print("3: an already-persisted snapshot still short-circuits the derive")
    fn3, calls3 = _recording_snapshot_fn()
    present = SimpleNamespace(
        current_release="rel-blg02",
        current_schema_snapshot=lambda: {"core": {"t": {}}},
        candidate_for=lambda _r: _candidate(manifest_plugins=_OLD_PLUGINS),
    )
    _resolve_current_snapshot(cast("Any", present), fn3)
    rec.check(not calls3, "collector never runs when the OLD snapshot is already present")


def _check_collector_override(rec: SmokeRecorder, app_home: str) -> None:
    print("4: the collector prefers the override, and reads the live manifest without one")
    saved = os.environ.get("MANIFEST_PLUGIN_OVERRIDE")
    try:
        os.environ["MANIFEST_PLUGIN_OVERRIDE"] = json.dumps(sorted(_OLD_PLUGINS))
        rec.check(
            _resolve_manifest(app_home) == set(_OLD_PLUGINS),
            "override present → collector uses it verbatim",
        )
        # NEGATIVE CONTROL: the unset leg must actually be unset. Leaving the
        # variable in the environment would make this leg re-measure the leg
        # above and report a false green.
        del os.environ["MANIFEST_PLUGIN_OVERRIDE"]
        # Assert DELEGATION rather than comparing against a real profile: the
        # collector must call ``load_manifest_plugin_set`` with the app_home it
        # was given. Comparing "not equal to the override" would also pass if
        # the function returned a hardcoded constant, and would need a real
        # profile on disk — which ``run_smokes`` does not provide.
        seen: list[str] = []
        real_loader = collector_mod.load_manifest_plugin_set
        collector_mod.load_manifest_plugin_set = (  # type: ignore[assignment]
            lambda home: (seen.append(home), {"live_sentinel_plugin"})[1]
        )
        try:
            live = _resolve_manifest(app_home)
        finally:
            collector_mod.load_manifest_plugin_set = real_loader  # type: ignore[assignment]
        rec.check(
            live == {"live_sentinel_plugin"} and seen == [app_home],
            "override absent → collector delegates to the live profile manifest loader",
        )
        for bad, label in (("not json", "malformed JSON"), ('{"a": 1}', "a JSON object")):
            os.environ["MANIFEST_PLUGIN_OVERRIDE"] = bad
            try:
                _resolve_manifest(app_home)
                rec.check(False, f"{label} override must raise, not fall back")
            except ValueError:
                rec.check(True, f"{label} override raises rather than silently falling back")
    finally:
        if saved is None:
            os.environ.pop("MANIFEST_PLUGIN_OVERRIDE", None)
        else:
            os.environ["MANIFEST_PLUGIN_OVERRIDE"] = saved


def _check_version_readback(rec: SmokeRecorder) -> None:
    print("5: VERSION read-back — absence degrades, malformation REPORTS (never raises)")
    rec.check(
        _read_manifest_plugins({}, "rel-x") == (None, None),
        "absent manifest_plugins → (None, None), the pre-capture compat rung",
    )
    rec.check(
        _read_manifest_plugins({"manifest_plugins": list(_OLD_PLUGINS)}, "rel-x")
        == (_OLD_PLUGINS, None),
        "a captured list reads back as a tuple, order preserved, no error",
    )
    # REGRESSION (reviewer Q5): rehydrate must never raise on this field —
    # rollback walks this path, and manifest_plugins says nothing about whether
    # a release BOOTS. Raising here made a garbage field into
    # ROLLBACK_TARGET_UNBOOTABLE on an intact release.
    for bad in ("nope", {"a": 1}, [1, 2], []):
        try:
            plugins, error = _read_manifest_plugins({"manifest_plugins": bad}, "rel-x")
        except ReleaseManagerError:
            rec.check(False, f"read-back of {bad!r} must REPORT, not raise (rollback walks this)")
            continue
        rec.check(
            plugins is None and error is not None,
            f"malformed manifest_plugins {bad!r} → reported as an error, not raised",
        )
    # REGRESSION (reviewer Q1): [] is malformation, not an empty capture. No
    # writer can emit it, and tolerating it collapses the §3 gate to
    # unconditionally-additive.
    _, empty_error = _read_manifest_plugins({"manifest_plugins": []}, "rel-x")
    rec.check(
        empty_error is not None and "EMPTY" in empty_error,
        "an EMPTY manifest_plugins is rejected as malformed, not tolerated as a capture",
    )


def _check_fail_open_chain_is_closed(rec: SmokeRecorder) -> None:
    """Reviewer Q1 + Q5, end to end: the two defects the second reviewer found."""
    print("5b: the empty-set fail-open chain is closed at BOTH ends")
    # Collector end: an empty override must refuse rather than discover zero
    # plugins and pass the completeness assert vacuously.
    saved = os.environ.get("MANIFEST_PLUGIN_OVERRIDE")
    try:
        os.environ["MANIFEST_PLUGIN_OVERRIDE"] = "[]"
        try:
            _resolve_manifest("/nonexistent/app_home")
            rec.check(False, "an EMPTY override must refuse — it collapses the gate to additive")
        except ValueError as exc:
            rec.check("EMPTY" in str(exc), "collector independently refuses an EMPTY override")
    finally:
        if saved is None:
            os.environ.pop("MANIFEST_PLUGIN_OVERRIDE", None)
        else:
            os.environ["MANIFEST_PLUGIN_OVERRIDE"] = saved
    # THE reason it matters, asserted rather than described: an empty OLD
    # snapshot is reported as unconditionally additive, because the classifier
    # iterates the OLD side.
    verdict = classify_snapshot_diff({}, {"core": {"t": {"c": {}}}})
    rec.check(
        verdict.is_additive,
        "an EMPTY old snapshot IS unconditionally additive — the fail-open this guards",
    )

    print("5d: the WRITE end refuses an empty capture too (third instance of the class)")
    # The reviewer's round-2 finding: the same writer invariant was enforced on
    # the read side only. build_candidate is public and its manifest_plugins is
    # caller-supplied, so () could be written as [] — a release built carrying a
    # field that permanently disqualifies it as a derive source, silently.
    builder = SimpleNamespace(build=ReleaseBuilder.build)
    try:
        builder.build(cast("Any", SimpleNamespace()), manifest_plugins=())
        rec.check(False, "an EMPTY manifest_plugins must be refused at BUILD time")
    except ReleaseManagerError as exc:
        rec.check("non-empty" in str(exc), "build refuses an empty capture before writing VERSION")
    except (AttributeError, TypeError):
        # The guard must fire BEFORE any real build work touches self.
        rec.check(False, "build's empty-capture guard must precede all other work")

    print("5c: a rejected capture refuses at the point of USE, not on rollback")
    old = _candidate(manifest_plugins=None)
    object.__setattr__(old, "manifest_plugins_error", "release rel-x has a malformed thing")
    fn, calls = _recording_snapshot_fn()
    try:
        _resolve_current_snapshot(_fake_release_manager(old), fn)
        rec.check(False, "the derive must refuse when the capture was rejected")
    except ReleaseManagerError as exc:
        rec.check("§3 derive refused" in str(exc), "the DERIVE refuses on a rejected capture")
    rec.check(not calls, "and it refuses BEFORE running the collector")


def _check_mutation_discriminator(rec: SmokeRecorder) -> None:
    print("6: MUTATION DISCRIMINATOR — the OLD behaviour still fails on these inputs")
    # The old tree discovered only its own two plugins.
    discovered = cast("Any", SimpleNamespace(plugins=dict.fromkeys(_OLD_PLUGINS)))
    try:
        _assert_completeness(discovered, set(_LIVE_PLUGINS))
        rec.check(False, "pre-fix path (live manifest) should have raised on manifest-GROW")
    except ValueError as exc:
        rec.check(
            "gamma_plugin" in str(exc),
            "pre-fix path DOES false-refuse, naming the plugin the old tree never had",
        )
    # ...and the fixed path, given the same trees, does not.
    try:
        _assert_completeness(discovered, set(_OLD_PLUGINS))
        rec.check(True, "post-fix path (captured manifest) passes on the SAME trees")
    except ValueError as exc:
        rec.check(False, f"post-fix path must not raise: {exc}")
    # The assert must still catch a GENUINE under-collection, or the fix has
    # bought availability by disabling the check it was protecting.
    try:
        _assert_completeness(
            cast("Any", SimpleNamespace(plugins={"alpha_plugin": None})), set(_OLD_PLUGINS)
        )
        rec.check(False, "a genuinely missing plugin must still raise")
    except ValueError as exc:
        rec.check("beta_plugin" in str(exc), "genuine under-collection still refuses")


def _check_producer_scrubs_inherited(rec: SmokeRecorder, app_home: str) -> None:
    print("7: the producer scrubs an INHERITED override when none is passed")
    inherited = dict(os.environ)
    inherited["MANIFEST_PLUGIN_OVERRIDE"] = json.dumps(["stale_plugin"])
    saved_env = os.environ.copy()
    try:
        os.environ.update(inherited)
        captured: dict[str, str] = {}

        class _Result:
            returncode = 0
            stdout = "{}"
            stderr = ""

        import macos_self_deployment_plugin.schema_snapshot_producer as producer_mod

        real_run = producer_mod.subprocess.run

        def _fake_run(_cmd: Any, **kwargs: Any) -> Any:
            captured.update(kwargs["env"])
            return _Result()

        producer_mod.subprocess.run = _fake_run  # type: ignore[assignment]
        try:
            fn = build_schema_snapshot_fn(
                solet_name="smoke", app_home=Path(app_home), source_root=_REPO_ROOT
            )
            fn(_REPO_ROOT)
            rec.check(
                "MANIFEST_PLUGIN_OVERRIDE" not in captured,
                "no override passed → an inherited value is scrubbed, not leaked to the collector",
            )
            fn(_REPO_ROOT, _OLD_PLUGINS)
            rec.check(
                captured.get("MANIFEST_PLUGIN_OVERRIDE") == json.dumps(sorted(_OLD_PLUGINS)),
                "override passed → serialized to the subprocess env",
            )
        finally:
            producer_mod.subprocess.run = real_run  # type: ignore[assignment]
    finally:
        os.environ.clear()
        os.environ.update(saved_env)


def main() -> int:
    rec = SmokeRecorder()
    # No real profile is required: the live-manifest leg asserts DELEGATION with
    # a stubbed loader, and the producer leg fakes the subprocess. So this value
    # only has to be a stable, recognizable path — ``run_smokes`` does not export
    # APP_HOME, and a smoke that bails without it is a smoke that never runs.
    app_home = os.environ.get("APP_HOME") or str(_REPO_ROOT / "profile")
    _check_derive_handoff(rec)
    _check_fallback_and_shortcircuit(rec)
    _check_collector_override(rec, app_home)
    _check_version_readback(rec)
    _check_fail_open_chain_is_closed(rec)
    _check_mutation_discriminator(rec)
    _check_producer_scrubs_inherited(rec, app_home)
    return rec.report("BLG-02 manifest capture")


if __name__ == "__main__":
    raise SystemExit(main())
