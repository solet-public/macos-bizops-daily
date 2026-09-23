"""Create-command resolution, preview, approval, and execution."""

from __future__ import annotations

import argparse
import re
import sys
from collections.abc import Sequence
from pathlib import Path

from .config import CreateConfig, load_create_config
from .create import CreateManager
from .decision_prompt import prompt_discovered_decisions
from .errors import ConfigError, SourceError
from .inspect import inspect_target
from .models import CommandResult, ExitCode, JsonValue
from .paths import ManagerPaths
from .rendering import render_human
from .seed_lock_parser import parse_seed_lock
from .stage_resume import require_named_resume_stage

_SEED_NAME = re.compile(r"^[a-z][a-z0-9_-]{1,63}$")
_SEED_SET_UNAVAILABLE_REPAIR = (
    "Run `solet --seed-lock <path> create <name>` with the intended lock, or "
    "reinstall from a formula version that provides named seeds."
)


def run_create_command(
    args: argparse.Namespace,
    paths: ManagerPaths,
    manager_type: type[CreateManager],
) -> CommandResult:
    name = _create_name(args)
    flag_decisions = parse_decisions(args.decision)
    config = load_create_config(
        config_path=args.config,
        flag_name=name,
        flag_target=args.target,
        flag_autostart=args.autostart,
        flag_decisions=flag_decisions,
    )
    seed_lock = _resolve_seed_lock(args)
    manager = manager_type(
        paths=paths,
        contract_directory=args.contract_dir,
        seed_lock_path=seed_lock,
    )
    if args.resume_stage is not None:
        require_named_resume_stage(
            paths=paths,
            config=config,
            stage_id=args.resume_stage,
        )
    decisions = dict(config.decisions)
    sources = dict(config.decision_sources)
    preview, decisions, sources = _preview_with_interactive_decisions(
        manager,
        config,
        args,
        decisions,
        sources,
    )
    if args.dry_run or preview.status != "preview_ready":
        return preview
    fingerprint = str(preview.data["approval_fingerprint"])
    approval_stop = _approval_stop(args, preview, fingerprint)
    if approval_stop is not None:
        return approval_stop
    if args.resume_stage is not None:
        return manager.create(
            config,
            approved_fingerprint=fingerprint,
            decision_selections=decisions,
            decision_sources=sources,
            stop_after_stage=args.resume_stage,
        )
    return manager.create(
        config,
        approved_fingerprint=fingerprint,
        decision_selections=decisions,
        decision_sources=sources,
    )


def run_inspect_command(args: argparse.Namespace) -> CommandResult:
    """Run passive inspection using only the caller-supplied target path."""
    if not isinstance(args.target, Path):
        raise AssertionError("inspect parser did not provide a target path")
    return inspect_target(args.target)


def run_list_seeds_command() -> CommandResult:
    """List the optional, tap-backed seed set without making discovery fatal."""
    seed_root = _seed_set_root()
    unavailable = _seed_set_unavailable_message(seed_root)
    if unavailable is not None:
        return CommandResult(
            kind="seed_list",
            status="unavailable",
            message=_list_seed_set_unavailable_message(unavailable),
            exit_code=ExitCode.OK,
            repair=_SEED_SET_UNAVAILABLE_REPAIR,
            data={"seeds": [], "inventory": "live tap view"},
        )
    try:
        directories = sorted(
            (entry for entry in seed_root.iterdir() if entry.is_dir()),
            key=lambda entry: entry.name,
        )
    except OSError as exc:
        return CommandResult(
            kind="seed_list",
            status="unavailable",
            message=f"seed set cannot be read: {exc}",
            exit_code=ExitCode.OK,
            data={"seeds": [], "inventory": "live tap view"},
        )
    seeds: list[JsonValue] = []
    invalid: list[JsonValue] = []
    for directory in directories:
        try:
            fields = parse_seed_lock(directory / "seed.lock.json")
        except SourceError as exc:
            invalid.append({"name": directory.name, "error": str(exc)})
            continue
        seeds.append({"name": directory.name, "profile": fields.profile})
    message = "Available named seeds from the live tap view."
    if invalid:
        message = "Available named seeds from the live tap view; invalid entries are reported below."
    return CommandResult(
        kind="seed_list",
        status="available",
        message=message,
        exit_code=ExitCode.OK,
        data={"seeds": seeds, "invalid_seeds": invalid, "inventory": "live tap view"},
    )


def _resolve_seed_lock(args: argparse.Namespace) -> Path:
    if args.seed_lock is not None:
        return args.seed_lock
    if args.seed is None:
        default_lock = Path(sys.prefix) / "share" / "solet" / "seed.lock.json"
        if default_lock.is_file():
            return default_lock
        unavailable = _seed_set_unavailable_message(_seed_set_root())
        if unavailable is not None:
            raise SourceError(
                _list_seed_set_unavailable_message(unavailable),
                repair=_SEED_SET_UNAVAILABLE_REPAIR,
            )
        raise SourceError(
            f"default seed lock expected at {default_lock}; found no file. Run "
            "`solet list-seeds`, then rerun with `--seed <name>`, or provide "
            "`solet --seed-lock <path> create <name>`.",
            repair=(
                "Run `solet list-seeds`, then rerun create with `--seed <name>` "
                "or `solet --seed-lock <path> create <name>`."
            ),
        )
    return _resolve_named_seed_lock(args.seed)


def _resolve_named_seed_lock(name: str) -> Path:
    if _SEED_NAME.fullmatch(name) is None:
        raise ConfigError("--seed must be a valid named-seed identifier")
    seed_root = _seed_set_root()
    unavailable = _seed_set_unavailable_message(seed_root)
    if unavailable is not None:
        raise SourceError(unavailable)
    try:
        available = sorted(entry.name for entry in seed_root.iterdir() if entry.is_dir())
    except OSError as exc:
        raise SourceError(f"seed set cannot be read: {exc}") from exc
    candidate = seed_root / name / "seed.lock.json"
    if name not in available or not candidate.is_file():
        names = ", ".join(available) if available else "none"
        raise SourceError(f"seed `{name}` not found; available: {names}")
    fields = parse_seed_lock(candidate)
    if fields.profile != name:
        raise SourceError(
            f"seed directory {name!r} does not match lock profile {fields.profile!r}"
        )
    return candidate


def _seed_set_root() -> Path:
    return Path(sys.prefix) / "share" / "solet" / "seeds"


def _seed_set_unavailable_message(seed_root: Path) -> str | None:
    if seed_root.is_symlink() and not seed_root.exists():
        return (
            "the seed set for this install is no longer reachable (its tap appears "
            "to have been removed); run `solet list-seeds` for the same diagnostic, "
            "or `solet --seed-lock <path> create <name>` to bypass it"
        )
    if not seed_root.is_dir():
        return (
            "this install has no seed set — `--seed <name>` is unavailable; use "
            "`solet --seed-lock <path> create <name>` or reinstall from a formula "
            "version that supports named seeds"
        )
    return None


def _list_seed_set_unavailable_message(message: str) -> str:
    if message.startswith("the seed set for this install is no longer reachable"):
        return f"tap not reachable — {message}"
    return f"no seed set available — {message}"


def _create_name(args: argparse.Namespace) -> str | None:
    name = args.name
    if name is not None or args.config is not None:
        return name
    if args.json or not sys.stdin.isatty():
        raise ConfigError(
            "create requires NAME or --config when prompting is unavailable"
        )
    return input("Solet name: ").strip()


def _preview_with_interactive_decisions(
    manager: CreateManager,
    config: CreateConfig,
    args: argparse.Namespace,
    decisions: dict[str, JsonValue],
    sources: dict[str, str],
) -> tuple[CommandResult, dict[str, JsonValue], dict[str, str]]:
    preview = manager.preview(
        config,
        decision_selections=decisions,
        decision_sources=sources,
    )
    if args.yes or args.json or not sys.stdin.isatty():
        return preview, decisions, sources
    selected = prompt_discovered_decisions(preview, decisions)
    if selected == decisions:
        return preview, decisions, sources
    interactive_ids = set(selected) - set(decisions)
    sources.update(dict.fromkeys(interactive_ids, "interactive"))
    refreshed = manager.preview(
        config,
        decision_selections=selected,
        decision_sources=sources,
    )
    return refreshed, selected, sources


def _approval_stop(
    args: argparse.Namespace,
    preview: CommandResult,
    fingerprint: str,
) -> CommandResult | None:
    if args.yes:
        return _yes_approval_stop(args, preview, fingerprint)
    if args.json:
        return CommandResult(
            kind="create_preview",
            status="awaiting_user",
            message="Exact preview rendered; pass --yes to approve this recomputed fingerprint.",
            exit_code=ExitCode.HUMAN_ACTION,
            error_kind="approval_required",
            repair="Review this result, then rerun the same inputs with --yes.",
            data=preview.data,
        )
    print(render_human(preview))
    if sys.stdin.isatty() and input("Continue? [y/N] ").strip().lower() in {
        "y",
        "yes",
    }:
        return None
    return CommandResult(
        kind="create",
        status="awaiting_user",
        message="Creation was not approved; no mutation was performed.",
        exit_code=ExitCode.HUMAN_ACTION,
        error_kind="approval_required",
        repair="Review the preview and rerun when ready.",
        data={},
    )


def _yes_approval_stop(
    args: argparse.Namespace,
    preview: CommandResult,
    fingerprint: str,
) -> CommandResult | None:
    supplied = args.approval_fingerprint
    if not isinstance(supplied, str):
        raise AssertionError("validated --yes carrier lacks its fingerprint")
    if supplied == fingerprint:
        return None
    return CommandResult(
        kind="create_preview",
        status="awaiting_user",
        message="The supplied approval fingerprint does not match the freshly recomputed preview.",
        exit_code=ExitCode.HUMAN_ACTION,
        error_kind="probe_drift",
        repair=(
            "Rerun --dry-run, review the changed preview, and use its new fingerprint "
            "if acceptable."
        ),
        data=preview.data,
    )


def parse_decisions(values: Sequence[str]) -> dict[str, JsonValue]:
    decisions: dict[str, JsonValue] = {}
    for raw in values:
        decision_id, separator, selected = raw.partition("=")
        if (
            separator != "="
            or not decision_id
            or re.fullmatch(r"[a-z][a-z0-9_.-]{1,127}", decision_id) is None
        ):
            raise ConfigError(f"invalid --decision value: {raw!r}; expected ID=VALUE")
        if selected == "":
            _append_empty_decision(decisions, decision_id)
            continue
        _append_decision(decisions, decision_id, selected)
    return decisions


def _append_empty_decision(
    decisions: dict[str, JsonValue],
    decision_id: str,
) -> None:
    if decision_id in decisions:
        raise ConfigError(
            f"decision {decision_id!r} cannot combine an explicit empty answer with selections"
        )
    decisions[decision_id] = []


def _append_decision(
    decisions: dict[str, JsonValue],
    decision_id: str,
    selected: str,
) -> None:
    existing = decisions.get(decision_id)
    if existing is None:
        decisions[decision_id] = selected
    elif isinstance(existing, str):
        decisions[decision_id] = [existing, selected]
    elif isinstance(existing, list):
        if not existing:
            raise ConfigError(
                f"decision {decision_id!r} cannot combine an explicit empty answer with selections"
            )
        existing.append(selected)
    else:
        raise AssertionError("parsed decision has an unsupported internal shape")
