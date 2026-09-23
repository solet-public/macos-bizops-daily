#!/usr/bin/env python3
"""S2 — the GTE-06 class-ender discriminator (no pytest).

Proves, in one deterministic run, BOTH halves of the design claim:

* the in-process preflight structurally CANNOT see a broken edit to an
  already-imported module (``importlib`` returns the ``sys.modules``-
  cached version — the documented cache-poisoning limit that slipped
  GTE-04's cutover #1), and
* the L2 probe subprocess CATCHES the same edit (fresh interpreter,
  fresh import off disk).

Mechanics: a REAL entry-point fixture (module + dist-info on
``sys.path``) is imported healthy by an in-process
``run_manifest_preflight`` run (poisoning this process's cache), then
the on-disk source is rewritten to raise at import. The in-process
re-run still PASSES (the limit); the probe subprocess FAILS with the
planted error (the fix). Positive control: with healthy source the
probe is GREEN.

Run:
    SOLET_NAME=<name> .venv/bin/python3 ananta/tests/lifecycle_management_service/preflight_probe_cache_poison_smoke.py
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _probe_fixture_support import (  # noqa: E402
    BROKEN_SOURCE,
    FIXTURE_PLUGIN_NAME,
    GOOD_SOURCE,
    run_probe_subprocess,
    write_fixture,
)
from ananta.services.lifecycle_management_service.manifest_preflight import (  # noqa: E402
    _build_partial_registry,
    run_manifest_preflight,
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


def _healthy_baseline(fixture_dir: Path) -> None:
    """[1-2] healthy source: in-process passes (and CACHES it); probe GREEN."""
    first = run_manifest_preflight({"plugins": [FIXTURE_PLUGIN_NAME]})
    _check(
        first.ok,
        f"[1] healthy source passes in-process preflight ({first.failures})",
    )
    exit_code, envelope, stderr = run_probe_subprocess(
        fixture_dir=fixture_dir, plugins=[FIXTURE_PLUGIN_NAME],
    )
    _check(
        exit_code == 0 and envelope is not None and envelope.get("ok") is True,
        f"[2] positive control: probe GREEN on healthy source "
        f"(exit={exit_code}, stderr tail: {stderr[-200:]!r})",
    )


def _poisoned_discriminator(fixture_dir: Path) -> None:
    """[3-4] the discriminator: in-process still passes; probe catches it."""
    # Plant the broken edit ON DISK; this process's cache still holds
    # the healthy version.
    write_fixture(fixture_dir, BROKEN_SOURCE)
    poisoned = run_manifest_preflight({"plugins": [FIXTURE_PLUGIN_NAME]})
    _check(
        poisoned.ok,
        "[3] cache-poisoning limit is REAL: in-process preflight still "
        f"passes on broken on-disk source ({poisoned.failures})",
    )
    exit_code, envelope, _stderr = run_probe_subprocess(
        fixture_dir=fixture_dir, plugins=[FIXTURE_PLUGIN_NAME],
    )
    probe_failures = (envelope or {}).get("failures") or []
    planted_seen = any(
        failure.get("error_class") == "RuntimeError"
        and "planted cache-poison probe target" in str(failure.get("message"))
        for failure in probe_failures
        if isinstance(failure, dict)
    )
    _check(
        exit_code == 3
        and envelope is not None
        and envelope.get("ok") is False
        and planted_seen,
        f"[4] probe subprocess CATCHES the planted edit the in-process "
        f"preflight cannot (exit={exit_code}, failures={probe_failures})",
    )


def _disabled_service_interfaces_are_not_registered() -> None:
    """Disabled service interfaces need no KB JSON overlay."""
    registry = _build_partial_registry({})
    processes = registry["processes"]
    shadow_docs = (
        REPO_ROOT / "ananta" / "knowledge_base" / "processes"
        / "io_interface_service" / "post_message.json",
        REPO_ROOT / "ananta" / "knowledge_base" / "processes"
        / "io_interface_service" / "deliver_artifact.json",
    )
    _check(
        isinstance(processes, dict)
        and "service_interface::io_interface_service::post_message" not in processes  # wint:negative-fixture
        and "service_interface::io_interface_service::deliver_artifact" not in processes,  # wint:negative-fixture
        "[5] disabled IO service-interface processes are absent from the KB overlay registry",
    )
    _check(
        not any(path.exists() for path in shadow_docs),
        "[6] disabled IO service-interface processes have no KB shadow documents",
    )


def run_smoke() -> int:
    print("=== preflight_probe_cache_poison_smoke (S2: in-process blind / probe sees) ===")
    with tempfile.TemporaryDirectory() as tmp:
        fixture_dir = Path(tmp) / "fixture"
        write_fixture(fixture_dir, GOOD_SOURCE)
        sys.path.insert(0, str(fixture_dir))
        try:
            _healthy_baseline(fixture_dir)
            _poisoned_discriminator(fixture_dir)
            _disabled_service_interfaces_are_not_registered()
        finally:
            sys.path.remove(str(fixture_dir))

    print(f"\npreflight_probe_cache_poison_smoke: {_passed} passed, {len(_failed)} failed")
    for label in _failed:
        print(f"  FAILED: {label}")
    return 1 if _failed else 0


if __name__ == "__main__":
    sys.exit(run_smoke())
