#!/usr/bin/env python3
"""Run one physical, read-only gate sweep over a composed landing-wave tree."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path

from quality_gates.candidate_tree import validate_candidate_manifest


class LandingWaveBatteryError(RuntimeError):
    """A one-sweep attempt did not preserve its exact candidate subject."""


@dataclass(frozen=True)
class GateCommandEvidence:
    """A single command in the one physical sweep."""

    argv: tuple[str, ...]
    exit_code: int
    stdout_sha256: str
    stderr_sha256: str


@dataclass(frozen=True)
class WaveBatteryReceipt:
    """Complete evidence that exactly one composed-subject sweep was attempted."""

    schema_version: str
    physical_sweeps: int
    command_count: int
    before_tree_sha256: str
    after_tree_sha256: str
    commands: tuple[GateCommandEvidence, ...]


def _tree_sha256(candidate_root: Path, manifest: Path) -> str:
    digest = hashlib.sha256()
    for relpath in validate_candidate_manifest(candidate_root, manifest):
        path = candidate_root / relpath
        mode = stat.S_IMODE(path.lstat().st_mode)
        payload = os.fsencode(os.readlink(path)) if path.is_symlink() else path.read_bytes()
        digest.update(relpath.encode("utf-8"))
        digest.update(b"\0")
        digest.update(f"{mode:o}".encode("ascii"))
        digest.update(b"\0")
        digest.update(hashlib.sha256(payload).digest())
    return digest.hexdigest()


def _run_gate_command(
    raw_command: Sequence[str],
    *,
    root: Path,
    runner: Callable[..., subprocess.CompletedProcess[str]],
    timeout_seconds: float,
) -> GateCommandEvidence:
    """Run and retain the exact evidence for one registered gate command."""
    command = tuple(raw_command)
    if not command or any(not value for value in command):
        raise LandingWaveBatteryError("gate command must be a non-empty argv sequence")
    completed = runner(
        command,
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
        timeout=timeout_seconds,
    )
    return GateCommandEvidence(
        argv=command,
        exit_code=completed.returncode,
        stdout_sha256=hashlib.sha256(completed.stdout.encode()).hexdigest(),
        stderr_sha256=hashlib.sha256(completed.stderr.encode()).hexdigest(),
    )


def run_wave_battery(
    candidate_root: Path,
    candidate_manifest: Path,
    commands: Sequence[Sequence[str]],
    *,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    timeout_seconds: float = 300.0,
) -> WaveBatteryReceipt:
    """Run each declared gate once, then refuse if bytes changed or a gate failed.

    The caller constructs ``commands`` from the registered Steps 1.5–9.25.
    This function is intentionally not a second gate planner and never invokes
    Git mutation.  It retains every exit/log digest so a partial failure is
    observable rather than being reported as an invented successful sweep.
    """
    if not commands:
        raise LandingWaveBatteryError("one physical sweep requires at least one gate command")
    root = candidate_root.resolve()
    manifest = candidate_manifest.resolve()
    before = _tree_sha256(root, manifest)
    evidence = [
        _run_gate_command(
            command, root=root, runner=runner, timeout_seconds=timeout_seconds,
        )
        for command in commands
    ]
    after = _tree_sha256(root, manifest)
    receipt = WaveBatteryReceipt(
        schema_version="landing_wave_battery.v1",
        physical_sweeps=1,
        command_count=len(evidence),
        before_tree_sha256=before,
        after_tree_sha256=after,
        commands=tuple(evidence),
    )
    if before != after:
        raise LandingWaveBatteryError(
            "a gate changed the composed candidate; the sweep is invalid and must not be committed"
        )
    failed = [item for item in evidence if item.exit_code != 0]
    if failed:
        raise LandingWaveBatteryError(
            f"one physical sweep failed: {[item.argv for item in failed]!r}"
        )
    return receipt


def main(argv: list[str] | None = None) -> int:
    """Run a manually declared JSON argv list once and render its evidence."""
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate-root", type=Path, required=True)
    parser.add_argument("--candidate-manifest", type=Path, required=True)
    parser.add_argument("--commands-file", type=Path, required=True)
    arguments = parser.parse_args(argv)
    try:
        raw = json.loads(arguments.commands_file.read_text(encoding="utf-8"))
        if not isinstance(raw, list) or not all(isinstance(item, list) for item in raw):
            raise LandingWaveBatteryError("commands file must be a JSON list of argv lists")
        commands = tuple(tuple(str(part) for part in item) for item in raw)
        receipt = run_wave_battery(
            arguments.candidate_root, arguments.candidate_manifest, commands,
        )
    except (LandingWaveBatteryError, OSError, UnicodeError, json.JSONDecodeError) as exc:
        print(f"landing_wave_battery REFUSED: {exc}")
        return 2
    print(json.dumps(asdict(receipt), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
