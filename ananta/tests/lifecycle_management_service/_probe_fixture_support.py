"""Shared fixture machinery for the GTE-06 probe smokes (S2/S5/S6).

Builds a REAL ``importlib.metadata``-visible plugin fixture — a module
plus a ``.dist-info`` directory that the entry-point scan discovers via
``sys.path`` / ``PYTHONPATH``. No mocks: both the in-process preflight
and the probe subprocess exercise the real entry-point route
(``importlib.metadata.entry_points(group="ananta.plugins")`` →
``ep.load()``), which is exactly where the cache-poisoning limit lives.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

FIXTURE_PLUGIN_NAME = "gte06_probe_fixture_plugin"
PROBE_MODULE = "ananta.services.lifecycle_management_service.preflight_probe"

GOOD_SOURCE = '''"""GTE-06 probe smoke fixture plugin (healthy version)."""


class Gte06ProbeFixturePlugin:
    """Minimal non-EdgeProcessProvider plugin: passes every preflight check."""

    def get_available_actions(self) -> list[object]:
        return []
'''

BROKEN_SOURCE = '''"""GTE-06 probe smoke fixture plugin (planted-broken version)."""

raise RuntimeError("planted cache-poison probe target")
'''


def write_fixture(fixture_dir: Path, source: str) -> None:
    """Write the fixture module + dist-info so the entry-point scan finds it."""
    fixture_dir.mkdir(parents=True, exist_ok=True)
    (fixture_dir / f"{FIXTURE_PLUGIN_NAME}.py").write_text(source)
    dist_info = fixture_dir / f"{FIXTURE_PLUGIN_NAME}-0.1.dist-info"
    dist_info.mkdir(exist_ok=True)
    (dist_info / "METADATA").write_text(
        "Metadata-Version: 2.1\n"
        "Name: gte06-probe-fixture-plugin\n"
        "Version: 0.1\n"
    )
    (dist_info / "entry_points.txt").write_text(
        "[ananta.plugins]\n"
        f"{FIXTURE_PLUGIN_NAME} = "
        f"{FIXTURE_PLUGIN_NAME}:Gte06ProbeFixturePlugin\n"
    )


def run_probe_subprocess(
    *, fixture_dir: Path | None, plugins: list[str], timeout: float = 300.0,
) -> tuple[int, dict[str, Any] | None, str]:
    """Run the REAL probe module as a REAL subprocess.

    ``fixture_dir`` (when given) rides ``PYTHONPATH`` so the fresh
    interpreter resolves the fixture package from THAT tree — the same
    mechanism by which the production probe resolves the candidate
    release's ``code/`` (interpreter-scoped path config).

    Returns ``(exit_code, envelope | None, stderr_text)``.
    """
    env = os.environ.copy()
    if fixture_dir is not None:
        existing = env.get("PYTHONPATH")
        env["PYTHONPATH"] = str(fixture_dir) + (
            os.pathsep + existing if existing else ""
        )
    proc = subprocess.run(
        [sys.executable, "-m", PROBE_MODULE],
        input=json.dumps({"plugins": plugins}).encode("utf-8"),
        capture_output=True,
        timeout=timeout,
        env=env,
        check=False,
    )
    envelope: Any = None
    try:
        envelope = json.loads(proc.stdout)
    except ValueError:
        envelope = None
    if not isinstance(envelope, dict):
        envelope = None
    return (
        proc.returncode,
        envelope,
        proc.stderr.decode("utf-8", errors="replace"),
    )
