"""Quality-service verb bodies — list_gates / run_gate / run_test.

Thin orchestration over :mod:`gate_registry` (the server-side allowlist +
argv builder) and :mod:`bounded_subprocess` (server-side argv, timeout,
bounded output). No caller-supplied path/argv/flag ever reaches the shell —
the gate/smoke NAME is the only caller input, and it is validated against the
server-side registry / register before any subprocess runs.

``SOLET_NAME`` is passed through explicitly on every run: the god-class
gate imports in-scope modules whose vault-scoped constants fail loud without
it (KB ``22_testing/03``).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from platform_dev_surface_plugin.bounded_subprocess import run_bounded
from platform_dev_surface_plugin.quality import gate_registry
from platform_dev_surface_plugin.quality.landing_verify import (
    detect_scope_regex_gaps,
    predict_append_only_register_merge,
    verify_hash_manifest,
)

# run_test bounds: the full suite runs 60+ smokes in series (is_long_running);
# each smoke carries its own per-smoke timeout inside run_smokes.py.
_SUITE_WALL_TIMEOUT = 900
_PER_SMOKE_TIMEOUT = 120

_SMOKE_REGISTER_REL = "quality_gates/gate_smokes.txt"
_RUN_SMOKES_REL = "quality_gates/run_smokes.py"

# The automake/Meson/CTest SKIP_RETURN_CODE convention, matching
# run_smokes.py's own _SKIP_EXIT_CODE — a smoke exits this code to report a
# disclosed, non-blocking dependency gap, not a genuine failure. Undeclared-
# dependency audit: workbench/2026-08-08_undeclared_system_dependencies_findings_d3-impl.md.
_SMOKE_SKIP_EXIT_CODE = 77


class QualityGateError(ValueError):
    """Typed rejection: an unknown gate name or an unregistered smoke path."""


def _last_meaningful_line(output: str) -> str:
    """The last non-blank line — gate verdicts / smoke summaries land at the end."""
    for line in reversed(output.splitlines()):
        stripped = line.strip()
        if stripped:
            return stripped
    return ""


class QualityOperations:
    """Bind the quality verbs to a concrete repo root + solet identity."""

    def __init__(self, repo_root: Path, solet_name: str) -> None:
        self._repo_root = repo_root
        self._solet_name = solet_name
        self._venv_python = repo_root / ".venv" / "bin" / "python3"
        if not self._venv_python.exists():
            raise FileNotFoundError(
                "platform_dev_surface_plugin: repo venv interpreter not found at "
                f"{self._venv_python}"
            )

    def _env(self) -> dict[str, str]:
        return {"SOLET_NAME": self._solet_name}

    def _read_smoke_register(self) -> list[str]:
        """Parse ``gate_smokes.txt`` into repo-relative smoke paths (# comments out)."""
        register = self._repo_root / _SMOKE_REGISTER_REL
        entries: list[str] = []
        for raw in register.read_text(encoding="utf-8").splitlines():
            line = raw.split("#", 1)[0].strip()
            if line:
                entries.append(line)
        return entries

    def list_gates(self) -> dict[str, Any]:
        """Enumerate the server-side gate registry + the smoke register. No execution."""
        gates = [
            {
                "name": spec.name,
                "kind": spec.kind,
                "description": spec.description,
                "timeout_seconds": spec.timeout_seconds,
                "directly_runnable": spec.directly_runnable,
                "run_via": (
                    "run_gate"
                    if spec.directly_runnable
                    else gate_registry.AGGREGATE_GATE_NAME
                ),
            }
            for spec in gate_registry.all_gates()
        ]
        smokes = self._read_smoke_register()
        return {"gates": gates, "smokes": smokes, "smoke_count": len(smokes)}

    def run_gate(self, gate: str) -> dict[str, Any]:
        """Run ONE directly-runnable gate by allowlisted name; report the verdict."""
        spec = gate_registry.runnable_gate(gate)
        if spec is None:
            raise QualityGateError(
                f"unknown or non-directly-runnable gate '{gate}'. Runnable gates: "
                f"{', '.join(gate_registry.runnable_gate_names())}. The coherence "
                f"trio runs via the '{gate_registry.AGGREGATE_GATE_NAME}' aggregate."
            )
        argv = gate_registry.build_gate_argv(spec, self._repo_root, self._venv_python)
        result = run_bounded(
            argv,
            cwd=self._repo_root,
            timeout=spec.timeout_seconds,
            extra_env=self._env(),
        )
        return {
            "gate": spec.name,
            "passed": result.exit_code == 0,
            # Static gates never emit the smoke SKIP convention; always False.
            "skipped": False,
            "exit_code": result.exit_code,
            "timed_out": result.timed_out,
            "summary": _last_meaningful_line(result.output),
            "output": result.output,
            "truncated": result.truncated,
            "output_chars_total": result.output_chars_total,
        }

    def run_test(self, smoke: str | None = None) -> dict[str, Any]:
        """Run the full smoke suite, or one registered smoke by path."""
        if smoke is None:
            argv = [
                str(self._venv_python),
                str(self._repo_root / _RUN_SMOKES_REL),
                "--register",
                str(self._repo_root / _SMOKE_REGISTER_REL),
                "--timeout",
                str(_PER_SMOKE_TIMEOUT),
            ]
            target = "suite"
        else:
            if smoke not in self._read_smoke_register():
                raise QualityGateError(
                    f"smoke '{smoke}' is not in the gate register "
                    f"({_SMOKE_REGISTER_REL}); only registered smokes may be run."
                )
            argv = [str(self._venv_python), str(self._repo_root / smoke)]
            target = smoke
        result = run_bounded(
            argv,
            cwd=self._repo_root,
            timeout=_SUITE_WALL_TIMEOUT,
            extra_env=self._env(),
        )
        # Single-smoke path: the smoke's own raw exit code is ground truth for
        # SKIP. Suite path: run_smokes.py's exit code already encodes its own
        # pass/fail policy over any skips it saw (see its --fail-on-skip) --
        # this verb doesn't currently parse the suite's per-smoke skip lines
        # out of `output`, so `skipped` stays False there rather than guessing
        # from prose.
        skipped = smoke is not None and result.exit_code == _SMOKE_SKIP_EXIT_CODE
        return {
            "target": target,
            "passed": result.exit_code == 0,
            "skipped": skipped,
            "exit_code": result.exit_code,
            "timed_out": result.timed_out,
            "summary": _last_meaningful_line(result.output),
            "output": result.output,
            "truncated": result.truncated,
            "output_chars_total": result.output_chars_total,
        }

    def verify_hash_manifest(
        self, unit_id: str, root_path: str, manifest: dict[str, str],
    ) -> dict[str, Any]:
        """Rehash a landing manifest and disclose editable-install shadowing."""
        return verify_hash_manifest(
            unit_id=unit_id, root_path=root_path, manifest=manifest,
        )

    def predict_gate_smokes_merge(
        self,
        base_ref: str,
        lane_root_path: str,
        current_master: str,
        register_path: str = "quality_gates/gate_smokes.txt",
    ) -> dict[str, Any]:
        """Predict a tracked-debt-register merge without changing any Git state."""
        return predict_append_only_register_merge(
            base_ref=base_ref,
            lane_root_path=lane_root_path,
            current_master=current_master,
            register_path=register_path,
        )

    def detect_scope_regex_gaps(self, paths: list[str]) -> dict[str, Any]:
        """Report landing paths which need static analysis beyond the aggregate gate."""
        return detect_scope_regex_gaps(paths)
