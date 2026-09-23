"""Focused contract checks for named journal-frontier resume selection."""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import solet_manager.cli as cli_module  # noqa: E402
import solet_manager.cli_commands as cli_commands  # noqa: E402
from solet_manager.config import CreateConfig  # noqa: E402
from solet_manager.errors import StateConflictError  # noqa: E402
from solet_manager.models import CommandResult, ExitCode  # noqa: E402
from solet_manager.paths import ManagerPaths  # noqa: E402
from solet_manager.stage_resume import require_named_resume_stage  # noqa: E402
from solet_manager.transaction import canonical_sha256  # noqa: E402

_CHECKS = 0


def _check(condition: object, label: str) -> None:
    global _CHECKS
    _CHECKS += 1
    if not condition:
        raise AssertionError(label)


def _raises(callback: object, text: str, label: str) -> None:
    try:
        callback()  # type: ignore[operator]
    except StateConflictError as exc:
        _check(text in str(exc), label)
    else:
        _check(False, label)


def main() -> int:
    root = Path("/private/tmp/stage-resume-cli-smoke")
    paths = ManagerPaths(root / "config", root / "state", root / "cache")
    config = CreateConfig(name="fixture", target=root / "target", autostart=False)
    journal = SimpleNamespace(
        name=config.name,
        target=str(config.target),
        input_fingerprint=canonical_sha256(config.to_identity_dict()),
        flow_source_revision="a" * 40,
        flow_contract_digest="b" * 64,
        answers={},
    )
    bundle = SimpleNamespace(stages={"models": {}})
    with patch("solet_manager.stage_resume.load_transaction", return_value=None):
        _raises(
            lambda: require_named_resume_stage(paths=paths, config=config, stage_id="models"),
            "requires an existing transaction",
            "fresh creates cannot masquerade as stage resumes",
        )
    with (
        patch("solet_manager.stage_resume.load_transaction", return_value=journal),
        patch("solet_manager.stage_resume.ContractBundle.load", return_value=bundle),
        patch("solet_manager.stage_resume.current_frontier_stage_ids", return_value=("models",)),
    ):
        require_named_resume_stage(paths=paths, config=config, stage_id="models")
        _check(True, "the exact singleton frontier is accepted")
        _raises(
            lambda: require_named_resume_stage(paths=paths, config=config, stage_id="unknown"),
            "names no declared stage",
            "undeclared stages fail before create can mutate",
        )
    with (
        patch("solet_manager.stage_resume.load_transaction", return_value=journal),
        patch("solet_manager.stage_resume.ContractBundle.load", return_value=bundle),
        patch(
            "solet_manager.stage_resume.current_frontier_stage_ids",
            return_value=("models", "genesis"),
        ),
    ):
        _raises(
            lambda: require_named_resume_stage(paths=paths, config=config, stage_id="models"),
            "sole executable frontier",
            "concurrent frontiers require an explicit preserved boundary",
        )
    _cli_integration(paths, config)
    print(f"stage_resume_cli_smoke OK: {_CHECKS} checks passed")
    return 0


def _cli_integration(paths: ManagerPaths, config: CreateConfig) -> None:
    args = cli_module.build_parser().parse_args(
        [
            "--seed-lock",
            "/private/tmp/stage-resume-cli-smoke/seed.lock.json",
            "create",
            config.name,
            "--target",
            str(config.target),
            "--resume-stage",
            "models",
            "--dry-run",
        ]
    )
    _check(args.resume_stage == "models", "create parser carries the named stage")
    calls: list[str] = []

    class _Manager:
        def __init__(self, **_kwargs: object) -> None:
            calls.append("manager")

    preview = CommandResult(
        kind="create_preview",
        status="preview_ready",
        message="fixture",
        exit_code=ExitCode.OK,
    )
    with (
        patch.object(cli_commands, "load_create_config", return_value=config),
        patch.object(cli_commands, "_resolve_seed_lock", return_value=Path("/seed.lock")),
        patch.object(
            cli_commands,
            "require_named_resume_stage",
            side_effect=lambda **_kwargs: calls.append("guard"),
        ),
        patch.object(
            cli_commands,
            "_preview_with_interactive_decisions",
            side_effect=lambda *_args: (preview, {}, {}),
        ),
    ):
        result = cli_commands.run_create_command(args, paths, _Manager)
    _check(result is preview, "guarded dry-run returns the ordinary preview")
    _check(calls == ["manager", "guard"], "named-stage guard runs before any preview")


if __name__ == "__main__":
    raise SystemExit(main())
