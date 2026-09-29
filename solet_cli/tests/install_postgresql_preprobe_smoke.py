#!/usr/bin/env python3
"""Regression checks for PostgreSQL installer pre-verification."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_ROOT / "solet_cli" / "src"))
sys.path.insert(0, str(_ROOT / "solet_cli" / "tests"))

from operation_probe_adapter_checks import _operation, _result, _transaction  # noqa: E402
from solet_manager import contracts, operation_executor  # noqa: E402
from solet_manager.adapters import OperationRequest, OperationResult  # noqa: E402
from solet_manager.contracts import ContractBundle  # noqa: E402
from solet_manager.models import CheckpointStatus  # noqa: E402

from bootstrap_adapter.homebrew import (  # noqa: E402
    HomebrewInstallError,
    _inspect_homebrew_plan,
    run_homebrew_install_required,
)
from bootstrap_adapter.models import AdapterRuntime  # noqa: E402


def _homebrew_plan_is_exact(output: str, *, kind: str, package: str) -> bool:
    return _inspect_homebrew_plan(output, kind=kind, package=package).exact


_CONTRACTS = _ROOT / "plugins/github_midwife_plugin/knowledge_base"
_LEGACY_FLOW = (
    _ROOT
    / "solet_cli/tests/fixtures/contracts/reconcile_contract_legacy_4ff38b3d"
    / "macos_setup_flow.json"
)
_LEGACY_DIGEST = "sha256:67c903ea332287f2c73f036ff055b793f50072ed3af0784d68a505801c040540"
_EXPECTED_PRE_PROBES = (
    "postgres_binary_version_valid",
    "postgres_ready",
    "pgvector_ready",
)
_INSTALLED_WARNING = (
    "Warning: pgvector 0.8.6 is already installed and up-to-date.\n"
    "To reinstall 0.8.6, run:\n  brew reinstall pgvector\n"
)
_VERSIONED_POSTGRESQL_PLAN = """\
==> Would install 1 formula:
postgresql@17 17.11
==> Downloading https://ghcr.io/v2/homebrew/core/postgresql/17/manifests/17.11
Already downloaded: /Users/admin/Library/Caches/Homebrew/postgresql@17.json
==> Would install 1 dependency for postgresql@17:
krb5
==> Would install 1 formula:
postgresql@17 17.11
==> Would install 1 dependency for postgresql@17:
krb5
"""
type _Run = Callable[..., subprocess.CompletedProcess[str]]


def _check(condition: bool, label: str) -> None:
    if not condition:
        raise AssertionError(label)


def _completed(
    command: list[str], code: int = 0, stdout: str = "", stderr: str = ""
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(command, code, stdout, stderr)


def _runtime(run: _Run) -> AdapterRuntime:
    return AdapterRuntime(
        run=run,
        which=lambda name: "/fixture/brew" if name == "brew" else None,
        now=lambda: datetime(2026, 9, 15, tzinfo=UTC),
        name="fixture",
        target=Path("/fixture"),
    )


def _check_fresh_install() -> None:
    calls: list[tuple[list[str], dict[str, object]]] = []

    def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append((list(command), kwargs))
        if "--dry-run" in command:
            return _completed(command, stdout="Would install 1 formula:\npgvector\n")
        return _completed(command)

    run_homebrew_install_required(_runtime(run), "/fixture/brew", "pgvector", "pgvector")
    _check(
        [command for command, _kwargs in calls]
        == [
            ["/fixture/brew", "install", "--dry-run", "pgvector"],
            ["/fixture/brew", "install", "pgvector"],
        ],
        "fresh pgvector install performs the reviewed mutation",
    )
    install_environment = calls[-1][1].get("env")
    _check(
        isinstance(install_environment, dict)
        and install_environment.get("HOMEBREW_NO_INSTALL_CLEANUP") == "1",
        "fresh package mutation disables implicit Homebrew cleanup",
    )


def _check_nonzero_dry_run_warning_is_refused() -> None:
    calls: list[list[str]] = []

    def run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(list(command))
        if "--dry-run" in command:
            return _completed(command, code=1, stderr=_INSTALLED_WARNING)
        if command[1:] == ["list", "--versions", "pgvector"]:
            return _completed(command, stdout="pgvector 0.8.6\n")
        raise AssertionError(f"unexpected mutation during retry: {command}")

    try:
        run_homebrew_install_required(_runtime(run), "/fixture/brew", "pgvector", "pgvector")
    except HomebrewInstallError as exc:
        _check(
            exc.outcome.returncode == 1
            and exc.outcome.stderr == _INSTALLED_WARNING,
            "nonzero dry-run warning is not a verified no-op",
        )
    else:
        raise AssertionError("nonzero dry-run warning was accepted as a verified no-op")
    _check(
        calls
        == [
            ["/fixture/brew", "install", "--dry-run", "pgvector"],
            ["/fixture/brew", "list", "--versions", "pgvector"],
        ],
        "nonzero dry-run warning stops before package mutation despite installed state",
    )


def _check_verified_already_installed_noop() -> None:
    calls: list[list[str]] = []

    def run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(list(command))
        if "--dry-run" in command:
            return _completed(command)
        if command[1:] == ["list", "--versions", "pgvector"]:
            return _completed(command, stdout="pgvector 0.8.6\n")
        raise AssertionError(f"unexpected mutation during verified no-op: {command}")

    outcome = run_homebrew_install_required(_runtime(run), "/fixture/brew", "pgvector", "pgvector")
    _check(
        outcome.returncode == 0 and outcome.output_complete and not outcome.stdout and not outcome.stderr,
        "successful complete empty dry-run and installed state verify a no-op",
    )
    _check(
        calls
        == [
            ["/fixture/brew", "install", "--dry-run", "pgvector"],
            ["/fixture/brew", "list", "--versions", "pgvector"],
        ],
        "verified no-op performs no package mutation",
    )


def _check_informational_nonzero_retry() -> None:
    calls: list[list[str]] = []

    def run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(list(command))
        if "--dry-run" in command:
            return _completed(command, stdout="Would install 1 formula:\npgvector\n")
        if command[1:] == ["install", "pgvector"]:
            return _completed(command, code=1, stderr=_INSTALLED_WARNING)
        if command[1:] == ["list", "--versions", "pgvector"]:
            return _completed(command, stdout="pgvector 0.8.6\n")
        raise AssertionError(f"unexpected command: {command}")

    run_homebrew_install_required(_runtime(run), "/fixture/brew", "pgvector", "pgvector")
    _check(
        calls[-1] == ["/fixture/brew", "list", "--versions", "pgvector"],
        "measured mutating-command nonzero triggers installed-state confirmation",
    )
    _check(
        calls.count(["/fixture/brew", "install", "pgvector"]) == 1,
        "informational nonzero is accepted without a reinstall attempt",
    )


def _check_later_install_preserves_pgvector() -> None:
    pgvector = {"formula_installed": True, "extension_registered": True}
    install_environment: dict[str, str] = {}

    def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        if "--dry-run" in command:
            return _completed(command, stdout="Would install 1 formula:\nnode\n")
        environment = kwargs.get("env")
        if isinstance(environment, dict):
            install_environment.update(environment)
        if install_environment.get("HOMEBREW_NO_INSTALL_CLEANUP") != "1":
            pgvector["formula_installed"] = False
        return _completed(command)

    run_homebrew_install_required(_runtime(run), "/fixture/brew", "node", "node")
    _check(
        pgvector == {"formula_installed": True, "extension_registered": True},
        "later package apply preserves an already-activated pgvector installation",
    )
    _check(
        install_environment.get("HOMEBREW_NO_INSTALL_CLEANUP") == "1",
        "later package apply explicitly suppresses implicit cleanup",
    )


def _check_versioned_postgresql_dry_run_plan() -> None:
    _check(
        _homebrew_plan_is_exact(
            _VERSIONED_POSTGRESQL_PLAN,
            kind="formula",
            package="postgresql@17",
        ),
        "versioned repeated PostgreSQL dry-run remains an exact reviewed plan",
    )
    _check(
        not _homebrew_plan_is_exact(
            _VERSIONED_POSTGRESQL_PLAN.replace("krb5\n", "krb5\nunapproved 1.0\n", 1),
            kind="formula",
            package="postgresql@17",
        ),
        "extra versioned package item is still refused",
    )
    _check(
        not _homebrew_plan_is_exact(
            _VERSIONED_POSTGRESQL_PLAN.replace(
                "postgresql@17 17.11", "postgresql@17 17.11 unexpected", 1
            ),
            kind="formula",
            package="postgresql@17",
        ),
        "malformed versioned package item is still refused",
    )


def _check_versioned_postgresql_dry_run_applies() -> None:
    commands: list[list[str]] = []

    def run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        commands.append(list(command))
        if "--dry-run" in command:
            return _completed(command, stdout=_VERSIONED_POSTGRESQL_PLAN)
        return _completed(command)

    run_homebrew_install_required(
        _runtime(run),
        "/fixture/brew",
        "postgresql@17",
        "PostgreSQL install",
    )
    _check(
        commands
        == [
            ["/fixture/brew", "install", "--dry-run", "postgresql@17"],
            ["/fixture/brew", "install", "postgresql@17"],
        ],
        "accepted versioned PostgreSQL dry-run reaches reviewed install",
    )


def _pre_probe_result(
    blocked_probe: str,
    observed: list[str],
    _registry: object,
    *,
    runner: str,
    request: OperationRequest,
) -> OperationResult:
    del runner
    observed.append(request.operation_id)
    if request.operation_id == blocked_probe:
        return _result(
            request,
            CheckpointStatus.BLOCKED,
            error_kind=f"fixture_{blocked_probe}_false",
        )
    return _result(request, CheckpointStatus.VERIFIED)


def _check_false_probe_does_not_preverify(
    bundle: ContractBundle,
    *,
    scenario: str,
    blocked_probe: str,
    expected_observed: tuple[str, ...],
) -> None:
    operation = _operation(bundle, "install_postgresql")
    observed: list[str] = []
    with tempfile.TemporaryDirectory(prefix="postgres_preprobe_") as raw:
        transaction = _transaction(bundle, Path(raw) / "target")
        with patch.object(
            operation_executor,
            "invoke_adapter",
            side_effect=lambda registry, runner, request: _pre_probe_result(
                blocked_probe,
                observed,
                registry,
                runner=runner,
                request=request,
            ),
        ):
            result = operation_executor._invoke_operation_probe(
                bundle=bundle,
                operation=operation,
                transaction=transaction,
                registry=object(),
                purpose="pre_apply",
                attempt=1,
            )
    _check(
        result.checkpoint_status is CheckpointStatus.BLOCKED
        and observed == list(expected_observed),
        f"{scenario} does not pre-verify install_postgresql",
    )


def main() -> int:
    bundle = ContractBundle.load(source_revision="a" * 40, directory=_CONTRACTS)
    operation = _operation(bundle, "install_postgresql")
    _check(
        operation.precondition_probe_ids == _EXPECTED_PRE_PROBES,
        "installer pre-probe requires binary, service readiness, and pgvector",
    )
    _check_false_probe_does_not_preverify(
        bundle,
        scenario="version-present-service-down",
        blocked_probe="postgres_ready",
        expected_observed=_EXPECTED_PRE_PROBES[:2],
    )
    _check_false_probe_does_not_preverify(
        bundle,
        scenario="version-present-service-up-pgvector-absent",
        blocked_probe="pgvector_ready",
        expected_observed=_EXPECTED_PRE_PROBES,
    )
    legacy_flow = json.loads(_LEGACY_FLOW.read_text(encoding="utf-8"))
    normalized = contracts._normalize_legacy_resume_flow_v1(_LEGACY_DIGEST, legacy_flow)
    legacy = normalized["operations"]["install_postgresql"]["idempotency"]
    _check(
        legacy["precondition_probe_refs"] == list(_EXPECTED_PRE_PROBES),
        "digest-pinned legacy resume strengthens the installer pre-probe",
    )
    _check_fresh_install()
    _check_nonzero_dry_run_warning_is_refused()
    _check_verified_already_installed_noop()
    _check_informational_nonzero_retry()
    _check_later_install_preserves_pgvector()
    _check_versioned_postgresql_dry_run_plan()
    _check_versioned_postgresql_dry_run_applies()
    print("install_postgresql_preprobe_smoke: 18/18 checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
