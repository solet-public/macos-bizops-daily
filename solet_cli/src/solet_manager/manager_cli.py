"""Step-1-restricted ``solet-manager`` executable surface.

This intentionally does not delegate to :mod:`solet_manager.cli`: that module
already owns operational commands which are outside the contract-foundation
packet.  The restricted binary is useful for packaging verification only.
"""

from __future__ import annotations

import argparse
import re
from collections.abc import Callable, Sequence
from pathlib import Path

from .errors import InvocationError, ManagerError
from .existing_install_doctor import run_doctor
from .existing_install_inspection import (
    ExistingInstallInspectionRequest,
    inspect_existing_install,
    load_installed_inspection_metadata,
)
from .import_enrollment import ImportRequest, enroll_import, preview_import
from .models import MANAGER_VERSION, CommandResult, ExitCode, JsonValue
from .paths import ManagerPaths
from .rendering import render_human, render_json
from .update_execution import UpdateRequest, apply_update, preview_update_instance
from .update_reconcile import abandon_update, preview_reconcile, reconcile_update, release_pointer

_RESULT_KINDS = {
    "import": "existing_install_import",
    "update": "existing_install_update",
    "inspect": "existing_install_inspection",
    "doctor": "existing_install_doctor",
    "reconcile": "existing_install_reconcile",
}
_CHECKPOINT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="solet-manager", description="Solet Manager contract foundation."
    )
    parser.add_argument("--version", action="version", version=f"solet-manager {MANAGER_VERSION}")
    commands = parser.add_subparsers(dest="command")
    inspect = commands.add_parser("inspect", help="Read-only classification of an existing Solet checkout; runs no target code (unlike `solet inspect`).")
    inspect.add_argument("--target", type=Path, required=True)
    inspect.add_argument("--channel", required=True)
    inspect.add_argument("--json", action="store_true", dest="as_json")
    imported = commands.add_parser("import", help="Preview or enroll an existing Solet.")
    imported.add_argument("name")
    imported.add_argument("--target", type=Path, required=True)
    imported.add_argument("--channel", required=True)
    mode = imported.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--yes", action="store_true")
    imported.add_argument("--approval-fingerprint")
    imported.add_argument("--json", action="store_true", dest="as_json")
    update = commands.add_parser(
        "update",
        help=(
            "Preview or apply the exact enrolled-channel update. Below source_advanced the source "
            "axis moves (Step 4); at source_advanced --dry-run renders the runtime plan and --yes "
            "executes or resumes it under its own approval fingerprint (Step 5)."
        ),
    )
    update.add_argument("name")
    update_mode = update.add_mutually_exclusive_group(required=True)
    update_mode.add_argument("--dry-run", action="store_true")
    update_mode.add_argument("--yes", action="store_true")
    update.add_argument("--approval-fingerprint")
    update.add_argument("--backup-checkpoint", dest="backup_checkpoint", help="Verified backup checkpoint id a forward-only platform migration requires (Step 6, D7).")
    update.add_argument("--json", action="store_true", dest="as_json")
    doctor = commands.add_parser("doctor", help="Diagnose an imported instance under the contract its state selects (candidate, verified, or diagnostic); target-read-only.")
    doctor.add_argument("name")
    doctor.add_argument("--json", action="store_true", dest="as_json")
    reconcile = commands.add_parser("reconcile", help="Recover a terminal update: plan/mint a successor, abandon or retire before the fast-forward, or release a stale pointer.")
    reconcile.add_argument("name")
    reconcile_mode = reconcile.add_mutually_exclusive_group(required=True)
    reconcile_mode.add_argument("--dry-run", action="store_true", help="Plan a successor operation; writes nothing.")
    reconcile_mode.add_argument("--yes", action="store_true", help="With --approval-fingerprint: mint the successor. With --abandon or --release-pointer: perform that Manager-state repair.")
    reconcile_form = reconcile.add_mutually_exclusive_group()
    reconcile_form.add_argument("--abandon", action="store_true", help="Abandon a nonterminal, or retire a terminal, update whose HEAD is still the baseline.")
    reconcile_form.add_argument("--release-pointer", action="store_true", dest="release_pointer", help="Release a pointer naming a promoted, abandoned, retired, or verified-import journal.")
    reconcile.add_argument("--approval-fingerprint")
    reconcile.add_argument("--json", action="store_true", dest="as_json")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command is None:
        return 0
    # This resolution is deliberately at the restricted CLI boundary.  The
    # inspection module receives the resolved infrastructure object and never
    # reads environment/home state itself.
    manager_paths = ManagerPaths.resolve()
    try:
        result = _COMMANDS[str(args.command)](args, manager_paths)
    except ManagerError as exc:
        result = _manager_error_result(args.command, exc)
    except ValueError as exc:
        error_kind = str(exc)
        invalid = error_kind == "target_identity_invalid"
        result = CommandResult(
            _RESULT_KINDS[args.command],
            "invalid" if invalid else "failed",
            "Existing Solet inspection could not be completed.",
            ExitCode.INVALID if invalid else ExitCode.FAILED,
            error_kind if invalid else "inspection_failed",
            data={"inspection_error": error_kind},
        )
    print(render_json(result) if args.as_json else render_human(result))
    return int(result.exit_code)


def _manager_error_result(command: str, exc: ManagerError) -> CommandResult:
    """Project a stable manager error onto the closed result/exit contract."""
    status = {2: "invalid", 3: "blocked"}.get(exc.exit_code, "failed")
    return CommandResult(
        _RESULT_KINDS[command],
        status,
        str(exc),
        ExitCode(exc.exit_code),
        exc.error_kind,
        exc.repair,
        data={"error_kind": exc.error_kind},
    )


def _inspect_result(args: argparse.Namespace, manager_paths: ManagerPaths) -> CommandResult:
    return inspect_existing_install(
        ExistingInstallInspectionRequest(
            target=args.target,
            channel=args.channel,
            manager_paths=manager_paths,
        ),
        metadata_loader=load_installed_inspection_metadata,
    ).to_command_result()


def _doctor_result(args: argparse.Namespace, manager_paths: ManagerPaths) -> CommandResult:
    return run_doctor(UpdateRequest(_instance_name(args), manager_paths))


def _instance_name(args: argparse.Namespace) -> str:
    name = str(args.name)
    if not name.islower() or not name.replace("-", "").isalnum():
        raise InvocationError("instance name must be lowercase alphanumeric with hyphens")
    return name


def _update_result(args: argparse.Namespace, manager_paths: ManagerPaths) -> CommandResult:
    name = _instance_name(args)
    selections: dict[str, JsonValue] = {}
    checkpoint = args.backup_checkpoint
    if checkpoint is not None:
        if not isinstance(checkpoint, str) or _CHECKPOINT.fullmatch(checkpoint) is None:
            raise InvocationError("--backup-checkpoint must be a plain checkpoint identifier")
        selections["backup_checkpoint_id"] = checkpoint
    request = UpdateRequest(name, manager_paths, operator_selections=selections)
    if args.dry_run:
        return preview_update_instance(request)
    return apply_update(request, args.approval_fingerprint)


def _reconcile_result(args: argparse.Namespace, manager_paths: ManagerPaths) -> CommandResult:
    request = UpdateRequest(_instance_name(args), manager_paths)
    repair = args.abandon or args.release_pointer
    if args.dry_run:
        if repair:
            raise InvocationError("--abandon and --release-pointer require --yes, not --dry-run")
        return preview_reconcile(request)
    if repair and args.approval_fingerprint is not None:
        raise InvocationError("--abandon and --release-pointer take no approval fingerprint")
    if args.abandon:
        return abandon_update(request)
    if args.release_pointer:
        return release_pointer(request)
    return reconcile_update(request, args.approval_fingerprint)


def _import_result(args: argparse.Namespace, manager_paths: ManagerPaths) -> CommandResult:
    request = ImportRequest(args.name, args.target, args.channel, manager_paths)
    if args.dry_run:
        return preview_import(request).to_command_result()
    if not args.yes or not args.approval_fingerprint:
        raise ValueError("approval_fingerprint_required")
    return enroll_import(request, args.approval_fingerprint).to_command_result()


_COMMANDS: dict[str, Callable[[argparse.Namespace, ManagerPaths], CommandResult]] = {
    "import": _import_result,
    "update": _update_result,
    "doctor": _doctor_result,
    "reconcile": _reconcile_result,
    "inspect": _inspect_result,
}
