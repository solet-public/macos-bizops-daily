#!/usr/bin/env python3
"""Focused M2 controls for byte-exact cutover approval fingerprints."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from solet_manager.cutover_fingerprint import cutover_fingerprint  # noqa: E402
from solet_manager.cutover_receipts import CutoverTerms  # noqa: E402

_CHECKS = 0


def _check(condition: bool, label: str) -> None:
    global _CHECKS
    _CHECKS += 1
    if not condition:
        raise AssertionError(label)


def _terms(*, current_release_id: str = "rel_prior") -> CutoverTerms:
    return CutoverTerms(
        current_release_id=current_release_id,
        active_color="blue",
        active_instance_id="inst_prior",
        active_start_token="start_prior",
        manifest_etag="manifest_prior",
        launch_topology="materialized_supervisor",
        launchagent_label="local.solet.fixture",
        launchagent_plist_sha256="sha256:" + "1" * 64,
        adapter_module_sha256="sha256:" + "2" * 64,
        adapter_module_replaced=True,
        source_surface_sha256="sha256:" + "3" * 64,
        release_surface_sha256="sha256:" + "4" * 64,
        verification_modules=(
            "github_midwife_plugin.setup_adapter",
            "macos_self_deployment_plugin.plugin",
        ),
    )


def _fingerprint(terms: CutoverTerms) -> str:
    return cutover_fingerprint(
        name="fixture",
        target="/fixture/target",
        seed={"commit": "a" * 40},
        files=(
            {
                "path": "/fixture/target/plugins/example.py",
                "before_sha256": "a" * 64,
                "after_sha256": "b" * 64,
            },
        ),
        terms=terms,
    )


def main() -> int:
    baseline = _fingerprint(_terms())
    _check(baseline == _fingerprint(_terms()), "same complete approval facts are byte-identical")
    _check(
        baseline != _fingerprint(_terms(current_release_id="rel_moved")),
        "current release drift invalidates approval before mutation",
    )
    print(f"cutover_fingerprint_smoke: {_CHECKS} checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
