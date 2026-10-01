"""Standard-library command-line surface for the global ``solet`` manager."""

from __future__ import annotations

import argparse
import re
import shlex
import sys
from collections.abc import Sequence
from pathlib import Path

from .adapter_reconciliation import AdapterReconciliationManager
from .attest import AttestRequest, run_attest
from .cli_commands import (
    parse_decisions as _parse_decisions,
)
from .cli_commands import (
    run_create_command,
    run_inspect_command,
    run_list_seeds_command,
)
from .contract_reconciliation import ContractReconciliationManager
from .create import CreateManager
from .doctor import InstallationDoctor
from .errors import (
    ApprovalFingerprintMalformedError,
    ApprovalFingerprintRequiredError,
    InvocationError,
    ManagerError,
)
from .identity_reconciliation import IdentityReconciliationManager
from .imported_refusal import refuse_imported
from .lifecycle import LifecycleManager
from .models import MANAGER_VERSION, CommandResult, ExitCode
from .paths import ManagerPaths
from .release_observer_cli import add_release_proof_parser, run_release_proof
from .rendering import render_human, render_json
from .rollback_repair import RollbackRepairExecutor

__all__ = ["CreateManager", "_parse_decisions", "build_parser", "main", "run"]


_MANAGER_ONLY_VERBS = ("import", "update")
_MANAGER_EPILOG = """\
Existing-solet lifecycle lives in the separate `solet-manager` command, not here:
  solet-manager inspect   classify an existing solet checkout (read-only, no target code runs)
  solet-manager import    preview or enroll an existing solet
  solet-manager update    preview or apply the enrolled-channel update
Run `solet-manager --help` for its options."""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="solet",
        description="Create and operate local Solet instances.",
        epilog=_MANAGER_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--version", action="version", version=f"solet {MANAGER_VERSION}")
    parser.add_argument("--json", action="store_true", help="Render the same typed result as JSON.")
    parser.add_argument("--home", type=Path, help="Override all manager-owned config/state/cache roots.")
    parser.add_argument("--contract-dir", type=Path, help=argparse.SUPPRESS)
    parser.add_argument(
        "--seed-lock",
        type=Path,
        help="Top-level option; place before `create`.",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    create = commands.add_parser(
        "create",
        help="Preview, create, or resume a named instance.",
    )
    create.add_argument("name", nargs="?")
    create.add_argument("--target", type=Path)
    create.add_argument("--config", type=Path)
    create.add_argument(
        "--resume-stage",
        help=(
            "Require an existing transaction whose sole executable frontier is this "
            "named stage before resuming it."
        ),
    )
    create.add_argument(
        "--seed",
        help="Use one named seed from this install's live seed set.",
    )
    create.add_argument("--dry-run", action="store_true")
    create.add_argument("--yes", action="store_true")
    create.add_argument(
        "--approval-fingerprint",
        help="Required with --yes; copy the sha256 value from the reviewed dry-run.",
    )
    create.add_argument(
        "--decision",
        action="append",
        default=[],
        metavar="ID=VALUE",
        help="Select one exact value returned by a discovered-decision probe.",
    )
    create.add_argument("--json", action="store_true", default=argparse.SUPPRESS)
    autostart = create.add_mutually_exclusive_group()
    autostart.add_argument("--autostart", dest="autostart", action="store_true")
    autostart.add_argument("--no-autostart", dest="autostart", action="store_false")
    create.set_defaults(autostart=None)

    list_command = commands.add_parser(
        "list",
        help="List manager-created instances.",
    )
    list_command.add_argument("--json", action="store_true", default=argparse.SUPPRESS)
    list_seeds = commands.add_parser(
        "list-seeds",
        help="List named seeds available from this install's live tap view.",
    )
    list_seeds.add_argument("--json", action="store_true", default=argparse.SUPPRESS)
    status = commands.add_parser("status", help="Inspect one manager-created instance.")
    status.add_argument("name")
    status.add_argument("--json", action="store_true", default=argparse.SUPPRESS)
    start = commands.add_parser(
        "start",
        help="Start one managed instance through its target adapter.",
    )
    start.add_argument("name")
    start.add_argument("--json", action="store_true", default=argparse.SUPPRESS)
    doctor = commands.add_parser(
        "doctor",
        help="Run the installation acceptance oracle.",
    )
    doctor.add_argument("name")
    doctor.add_argument("--json", action="store_true", default=argparse.SUPPRESS)
    attest = commands.add_parser(
        "attest",
        help="Write installation_attestation.json: real hashes of the installed manager, "
        "seed checkout, running process and host, compared to the release manifest.",
    )
    attest.add_argument("name", nargs="?", help="One managed instance; default: every registered instance.")
    attest.add_argument("--against", help="A release_manifest.json path, or a manager release tag.")
    attest.add_argument(
        "--release-repository",
        help="OWNER/REPO holding the manager releases; required when --against names a tag.",
    )
    attest.add_argument("--output", type=Path, help="Where to write the attestation (default: manager state).")
    attest.add_argument("--json", action="store_true", default=argparse.SUPPRESS)
    inspect = commands.add_parser(
        "inspect",
        help="Actively probe a seed-built target by filesystem path; may run target binaries.",
        description=(
            "Active, identity-validated probe of a seed-built target. It may run binaries "
            "inside the target, and target mutation is not prevented. For a read-only "
            "classification of an existing solet checkout use `solet-manager inspect`."
        ),
    )
    inspect.add_argument("--target", type=Path, required=True)
    inspect.add_argument("--json", action="store_true", default=argparse.SUPPRESS)
    repair = commands.add_parser(
        "repair",
        help="Run one closed, failure-record-bound rollback recipe; live invocation is deliberately unavailable.",
    )
    repair.add_argument("--failure-record", type=Path, required=True)
    repair.add_argument("--failure-record-sha256", required=True)
    repair.add_argument("--json", action="store_true", default=argparse.SUPPRESS)
    reconcile_contract = commands.add_parser(
        "reconcile-contract",
        help="Preview or apply one release-declared persisted contract reconciliation.",
    )
    reconcile_contract.add_argument("name")
    reconcile_contract_mode = reconcile_contract.add_mutually_exclusive_group()
    reconcile_contract_mode.add_argument("--dry-run", action="store_true")
    reconcile_contract_mode.add_argument("--yes", action="store_true")
    reconcile_contract.add_argument("--approval-fingerprint")
    reconcile_contract.add_argument("--json", action="store_true", default=argparse.SUPPRESS)
    reconcile_adapter = commands.add_parser(
        "reconcile-adapter",
        help="Snapshot-only frozen-code refresh; live invocation is deliberately unavailable.",
    )
    reconcile_adapter.add_argument("name")
    reconcile_adapter_mode = reconcile_adapter.add_mutually_exclusive_group()
    reconcile_adapter_mode.add_argument("--dry-run", action="store_true")
    reconcile_adapter_mode.add_argument("--yes", action="store_true")
    reconcile_adapter.add_argument("--approval-fingerprint")
    reconcile_adapter.add_argument(
        "--snapshot-replay",
        action="store_true",
        help="Required: this refresh has no sanctioned live cutover interface.",
    )
    reconcile_adapter.add_argument("--json", action="store_true", default=argparse.SUPPRESS)
    reconcile_identity = commands.add_parser(
        "reconcile-identity",
        help="Preview or apply one receipt-bound in-field identity migration.",
    )
    reconcile_identity.add_argument("name")
    reconcile_identity_mode = reconcile_identity.add_mutually_exclusive_group()
    reconcile_identity_mode.add_argument("--dry-run", action="store_true")
    reconcile_identity_mode.add_argument("--yes", action="store_true")
    reconcile_identity.add_argument("--approval-fingerprint")
    reconcile_identity.add_argument("--json", action="store_true", default=argparse.SUPPRESS)
    add_release_proof_parser(commands)
    for verb in _MANAGER_ONLY_VERBS:
        pointer = commands.add_parser(verb, help=f"Not here: this is `solet-manager {verb}`.")
        pointer.add_argument("rest", nargs=argparse.REMAINDER)
    return parser


def _manager_pointer(args: argparse.Namespace) -> str:
    return f"`solet {args.command}` does not exist; run `{shlex.join(['solet-manager', args.command, *args.rest])}`."


def _reject_manager_only_verb(args: argparse.Namespace) -> None:
    if args.command in _MANAGER_ONLY_VERBS:
        raise InvocationError(_manager_pointer(args))


def run(argv: Sequence[str] | None = None) -> CommandResult:
    args = build_parser().parse_args(argv)
    _reject_manager_only_verb(args)
    if args.command in {"create", "reconcile-contract", "reconcile-adapter", "reconcile-identity"}:
        _validate_approval_carrier(args)
    if args.command in {"inspect", "release-proof"}:
        return _run_read_only_command(args)
    paths = ManagerPaths.resolve(explicit_home=args.home)
    refuse_imported(paths, args.command, getattr(args, "name", None))
    if args.command in {"list", "list-seeds"}:
        return _run_inventory_command(args, paths)
    if args.command == "status":
        candidate = Path.home() / "Solets" / args.name
        return LifecycleManager(paths).status(args.name, candidate_target=candidate)
    if args.command == "start":
        return LifecycleManager(paths).start(args.name)
    if args.command in {"doctor", "attest"}:
        return _run_diagnostic_command(args, paths)
    if args.command == "repair":
        return RollbackRepairExecutor(paths).run(
            args.failure_record,
            args.failure_record_sha256,
        )
    if args.command in {"reconcile-contract", "reconcile-adapter", "reconcile-identity"}:
        return _run_reconciliation_command(args, paths)
    if args.command == "create":
        return run_create_command(args, paths, CreateManager)
    raise AssertionError(f"unhandled command: {args.command}")


def _run_diagnostic_command(args: argparse.Namespace, paths: ManagerPaths) -> CommandResult:
    if args.command == "doctor":
        return InstallationDoctor(
            paths=paths,
            contract_directory=args.contract_dir,
        ).run(args.name)
    return run_attest(
        AttestRequest(paths, args.name, args.against, args.release_repository, args.output)
    )


def _run_reconcile_contract(args: argparse.Namespace, paths: ManagerPaths) -> CommandResult:
    return ContractReconciliationManager(
        paths=paths,
        contract_directory=args.contract_dir,
    ).run(
        args.name,
        dry_run=args.dry_run,
        approved_fingerprint=args.approval_fingerprint,
    )


def _run_read_only_command(args: argparse.Namespace) -> CommandResult:
    if args.command == "inspect":
        return run_inspect_command(args)
    paths = ManagerPaths.resolve(explicit_home=args.home)
    return run_release_proof(args, paths.state_dir, args.seed_lock)


def _run_reconciliation_command(args: argparse.Namespace, paths: ManagerPaths) -> CommandResult:
    if args.command == "reconcile-contract":
        return _run_reconcile_contract(args, paths)
    if args.command == "reconcile-adapter":
        return _run_reconcile_adapter(args, paths)
    if args.command == "reconcile-identity":
        return IdentityReconciliationManager(paths=paths).run(
            args.name,
            dry_run=args.dry_run,
            approved_fingerprint=args.approval_fingerprint,
        )
    raise AssertionError(f"unexpected reconciliation command: {args.command}")


def _run_reconcile_adapter(args: argparse.Namespace, paths: ManagerPaths) -> CommandResult:
    if not args.snapshot_replay:
        return CommandResult(
            kind="adapter_reconciliation",
            status="not_yet_invokable",
            message="reconcile-adapter has no sanctioned live cutover interface and refuses live use.",
            exit_code=ExitCode.HUMAN_ACTION,
            error_kind="cutover_interface_unavailable",
            repair="Use only a preserved snapshot replay while iss_dd6c87e6 remains open.",
            data={"live_cutover": "not_available_pending_iss_dd6c87e6"},
        )
    return AdapterReconciliationManager(
        paths=paths,
        seed_lock_path=_adapter_seed_lock(args),
    ).run(
        args.name,
        dry_run=args.dry_run,
        approved_fingerprint=args.approval_fingerprint,
    )


def _adapter_seed_lock(args: argparse.Namespace) -> Path:
    if args.seed_lock is not None:
        return args.seed_lock
    default = Path(sys.prefix) / "share" / "solet" / "seed.lock.json"
    if default.is_file():
        return default
    raise ManagerError(f"manager release seed lock is unavailable at {default}; reconcile-adapter cannot materialize an authenticated payload")


def _run_inventory_command(args: argparse.Namespace, paths: ManagerPaths) -> CommandResult:
    if args.command == "list":
        return LifecycleManager(paths).list_instances()
    return run_list_seeds_command()


def main(argv: Sequence[str] | None = None) -> None:
    parser = build_parser()
    parsed: argparse.Namespace | None = None
    try:
        parsed = parser.parse_args(argv)
        if parsed.command in _MANAGER_ONLY_VERBS:
            print(_manager_pointer(parsed), file=sys.stderr)
            raise SystemExit(int(ExitCode.INVALID))
        result = run(argv)
    except ManagerError as exc:
        result = CommandResult(
            kind="error",
            status="failed" if exc.exit_code == 1 else "awaiting_user",
            message=str(exc),
            exit_code=ExitCode(exc.exit_code),
            error_kind=exc.error_kind,
            repair=exc.repair,
        )
    use_json = bool(parsed is not None and getattr(parsed, "json", False))
    print(render_json(result) if use_json else render_human(result))
    raise SystemExit(int(result.exit_code))


def _validate_approval_carrier(args: argparse.Namespace) -> None:
    supplied = args.approval_fingerprint
    if args.yes and supplied is None:
        raise ApprovalFingerprintRequiredError("--yes requires --approval-fingerprint sha256:<64 lowercase hex digits>")
    if not args.yes and supplied is not None:
        raise ApprovalFingerprintRequiredError("--approval-fingerprint requires the paired --yes intent flag")
    if supplied is not None and re.fullmatch(r"sha256:[0-9a-f]{64}", supplied) is None:
        raise ApprovalFingerprintMalformedError("--approval-fingerprint must be sha256 followed by 64 lowercase hex digits")


if __name__ == "__main__":
    main()
