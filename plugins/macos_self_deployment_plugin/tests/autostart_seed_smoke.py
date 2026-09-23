#!/usr/bin/env python3
"""§4.5 role-1 autostart seed/bake smoke (no pytest).

Design ``2026-06-27_true_local_blue_green_materialized_artifacts_design.md``
§4.5 role 1, Architect verdict 2026-06-28: the cold-start plist must BAKE
the literal ``<releases_root>/current/venv/bin/python3`` symlink path (no
dev-venv fallback — launchd resolves ``current`` at exec time, robust to
every flip with zero re-render), and the install verb must GUARANTEE
``current`` exists by SEEDING release-0 from the working tree when none
exists. Removing the fallback without the seed would exec a dangling path
on a virgin box (ReleaseManager is passive on first boot), so the two are
landed together.

Scenarios (each the EXACT behavior the fix establishes):
  (i)   install with no ``current`` → seeds release-0 → the baked path resolves.
  (ii)  the literal path survives a cutover flip with NO re-render.
  (iii) ``current`` absent → the resolver returns the literal ``current`` path
        (NOT the dev venv / sys.executable) — fail-loud, no silent fallback.

Drives the real plugin/AutostartManager against a SYNTHETIC source tree
under ``~/.ananta`` scratch (NEVER ``/tmp``); ``launchctl`` is stubbed with
``/usr/bin/true`` so the operator's real LaunchAgents are never touched.

Run:
    .venv/bin/python3 plugins/macos_self_deployment_plugin/tests/autostart_seed_smoke.py
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

_PLUGIN_SRC = Path(__file__).resolve().parents[1] / "src"
_REPO_ROOT = Path(__file__).resolve().parents[3]
for _p in (str(_PLUGIN_SRC), str(_REPO_ROOT / "ananta" / "src")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from _release_manager_smoke_support import (  # noqa: E402
    SmokeRecorder,
    build_fake_source,
    make_manager,
    scratch_root,
)
from ananta.interfaces.lifecycle_result_types import AutostartStatus  # noqa: E402
from macos_self_deployment_plugin.autostart_manager import AutostartManager  # noqa: E402
from macos_self_deployment_plugin.plugin import MacosSelfDeploymentPlugin  # noqa: E402
from macos_self_deployment_plugin.release_manager import (  # noqa: E402
    CURRENT_LINK_NAME,
    VENV_DIRNAME,
    ReleaseManager,
)


def _baked_interpreter_path(releases_root: Path) -> Path:
    return releases_root / CURRENT_LINK_NAME / VENV_DIRNAME / "bin" / "python3"


def _make_autostart(source: Path, releases_root: Path, scratch: Path) -> AutostartManager:
    """Real AutostartManager pointed at scratch; launchctl stubbed with /usr/bin/true."""
    return AutostartManager(
        solet_name="smoke",
        project_root=source,
        plist_dir=scratch / "agents",
        releases_root=releases_root,
        log_dir=scratch / "logs",
        launchctl_path="/usr/bin/true",
    )


def _make_plugin(rm: ReleaseManager, am: AutostartManager) -> MacosSelfDeploymentPlugin:
    plugin = MacosSelfDeploymentPlugin()
    plugin._solet_name = "smoke"  # noqa: SLF001
    plugin.set_release_manager(rm)
    plugin.set_autostart_manager(am)
    return plugin


def run_smoke() -> int:
    rec = SmokeRecorder()
    print("=== autostart_seed_smoke (§4.5 role 1: bake literal current + seed-at-install) ===")
    scratch = scratch_root("autostart-seed")
    scratch.mkdir(parents=True, exist_ok=True)
    try:
        source = build_fake_source(scratch)
        releases_root = scratch / "releases"
        baked = _baked_interpreter_path(releases_root)

        # ---- (iii) current ABSENT → literal current path, NO fallback ----
        am0 = _make_autostart(source, releases_root, scratch)
        resolved = am0._resolve_autostart_interpreter()  # noqa: SLF001
        dev_venv = str(source / ".venv" / "bin" / "python3")
        rec.check(
            resolved == str(baked),
            f"current-absent → baked literal current path (not dev venv): {resolved}",
        )
        rec.check(
            resolved != dev_venv and not Path(resolved).is_file(),
            "current-absent → NO dev-venv fallback; path is fail-loud (does not resolve)",
        )

        # ---- (i) install with no current → SEEDS release-0 → baked path resolves ----
        rm = make_manager(source, releases_root)
        am = _make_autostart(source, releases_root, scratch)
        plugin = _make_plugin(rm, am)
        rec.check(rm.current_release is None, "pre-install: no current release")
        result = plugin.install_autostart(dry_run=False)
        rec.check(
            result.status == AutostartStatus.SUCCESS,
            f"install_autostart succeeded: {result.status} ({result.message})",
        )
        rec.check(rm.current_release is not None, "install SEEDED release-0 (current now set)")
        rec.check(
            baked.is_file(),
            "the baked literal current/venv/bin/python3 now RESOLVES to a real file",
        )
        seeded_rel = os.path.basename(os.readlink(releases_root / CURRENT_LINK_NAME))

        # ---- (ii) literal path survives a cutover flip with NO re-render ----
        plist_before = am.plist_path.read_bytes()
        candidate2 = rm.build_candidate()
        rm.cutover(candidate2)
        flipped_rel = os.path.basename(os.readlink(releases_root / CURRENT_LINK_NAME))
        rec.check(
            flipped_rel != seeded_rel,
            f"cutover flipped current {seeded_rel} → {flipped_rel}",
        )
        rec.check(
            am._resolve_autostart_interpreter() == str(baked),  # noqa: SLF001
            "the baked interpreter path is UNCHANGED after the flip (literal symlink path)",
        )
        rec.check(
            am.plist_path.read_bytes() == plist_before,
            "the on-disk plist is byte-identical after the flip — NO re-render needed",
        )
        rec.check(
            baked.is_file() and os.path.basename(os.path.dirname(
                os.path.dirname(os.path.dirname(os.path.realpath(baked))))) == flipped_rel,
            "baked path now resolves (via current) to the NEW release's interpreter",
        )
    finally:
        shutil.rmtree(scratch, ignore_errors=True)

    return rec.report("autostart_seed_smoke")


if __name__ == "__main__":
    sys.exit(run_smoke())
