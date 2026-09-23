"""Focused operation-route assertions used by the bootstrap adapter smoke."""

from __future__ import annotations

import subprocess
from pathlib import Path
from types import ModuleType
from typing import Any


def check_python_install_route(
    *,
    module: ModuleType,
    root: Path,
    request_factory: Any,
    execute: Any,
    completed: Any,
    check: Any,
    fingerprint: str,
) -> None:
    """Apply must prove the long-lived interpreter selected by the manager."""
    selected = root / "selected-python3.13"
    selected.write_text("fixture")
    request = request_factory(
        root,
        operation_id="install_python_runtime",
        operation_ref="setup::python.install_313",
        phase="apply",
        purpose=None,
        approval=fingerprint,
    )
    calls: list[list[str]] = []

    def runner(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(list(command))
        if command == [str(selected), "--version"]:
            return completed(stdout="Python 3.13.7\n")
        return completed()

    applied = execute(
        module,
        request,
        runner,
        lambda _name: None,
        base_python=str(selected),
    )
    check(
        applied["checkpoint_status"] == "applied"
        and [str(selected), "--version"] in calls,
        "Python apply proves the selected long-lived interpreter before reporting applied",
    )
    unselected = execute(module, request, runner, lambda _name: None)
    check(
        unselected["checkpoint_status"] == "blocked"
        and unselected["error_kind"] == "python_runtime_resolution_required",
        "Python apply cannot claim success without a selected long-lived interpreter",
    )


def check_homebrew_install_route(
    *,
    module: ModuleType,
    root: Path,
    request_factory: Any,
    execute: Any,
    completed: Any,
    check: Any,
    fingerprint: str,
) -> None:
    """The reviewed Homebrew stop is a closed operation with no installer command."""
    request = request_factory(
        root,
        operation_id="request_homebrew_install",
        operation_ref="setup::homebrew.request_install",
    )
    calls: list[list[str]] = []

    def runner(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(list(command))
        if command[1:] == ["--version"]:
            return completed(1)
        return completed()

    missing = execute(module, request, runner, lambda _name: None)
    check(
        missing["checkpoint_status"] == "awaiting_user"
        and missing["error_kind"] == "homebrew_missing"
        and missing["repair"] == "Install Homebrew from its reviewed official distribution path, then resume.",
        "Homebrew operation presents the reviewed human action without a fetched installer",
    )
    check(
        calls
        == [
            ["/opt/homebrew/bin/brew", "--version"],
            ["/usr/local/bin/brew", "--version"],
        ],
        "Homebrew operation only validates standard executable candidates while absent",
    )
    apply = request_factory(
        root,
        operation_id="request_homebrew_install",
        operation_ref="setup::homebrew.request_install",
        phase="apply",
        purpose=None,
        approval=fingerprint,
    )
    apply_missing = execute(module, apply, runner, lambda _name: None)
    check(
        apply_missing["checkpoint_status"] == "awaiting_user"
        and apply_missing["error_kind"] == "homebrew_missing"
        and all(command[1:] == ["--version"] for command in calls),
        "Homebrew apply remains a human action instead of executing an installer",
    )
    from bootstrap_adapter import routes

    declared = routes._ROUTES.pop("request_homebrew_install")  # noqa: SLF001
    try:
        unregistered = execute(module, request, runner, lambda _name: None)
    finally:
        routes._ROUTES["request_homebrew_install"] = declared  # noqa: SLF001
    check(
        unregistered["error_kind"] == "adapter_missing",
        "removing the exact Homebrew operation route fails closed",
    )


def check_absolute_homebrew_fallback(
    *,
    module: ModuleType,
    root: Path,
    request_factory: Any,
    execute: Any,
    completed: Any,
    check: Any,
) -> None:
    """An absolute Homebrew fallback closes all Homebrew-gated routes."""
    brew = "/opt/homebrew/bin/brew"
    prefix = root / "absolute-homebrew"
    keg_bin = prefix / "opt/postgresql@17/bin"
    hba = prefix / "var/postgresql@17/pg_hba.conf"
    hba.parent.mkdir(parents=True)
    hba.write_text(
        "local all all scram-sha-256\n"
        "host all all 127.0.0.1/32 scram-sha-256\n"
        "host all all ::1/128 scram-sha-256\n",
    )
    hba.chmod(0o600)
    calls: list[list[str]] = []

    def runner(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(list(command))
        joined = " ".join(command)
        if command == [brew, "--version"]:
            return completed(stdout="Homebrew 4.4.0\n")
        if command == [brew, "list", "--formula"]:
            return completed(stdout="postgresql@17\npgvector\n")
        if command == [brew, "--prefix", "postgresql@17"]:
            return completed(stdout=f"{prefix / 'opt/postgresql@17'}\n")
        if command == [brew, "--prefix"]:
            return completed(stdout=f"{prefix}\n")
        if command == [str(keg_bin / "psql"), "--version"]:
            return completed(stdout="psql (PostgreSQL) 17.2\n")
        if command[:1] == [str(keg_bin / "pg_isready")]:
            return completed()
        if "rolsuper" in joined:
            return completed(stdout="f|f|f|f|f\n")
        if "pg_get_userbyid(datdba)" in joined or "pg_get_userbyid(nspowner)" in joined:
            return completed(stdout="adaptertest\n")
        if "datacl" in joined:
            return completed(stdout="adaptertest=CTc/adaptertest\n")
        if "pg_available_extensions" in joined or "pg_extension" in joined:
            return completed(stdout="1\n")
        if "pg_roles" in joined or "pg_database" in joined:
            return completed(stdout="1\n")
        return completed()

    def which(name: str) -> str | None:
        if name == "brew":
            return None
        return name if name.startswith(str(keg_bin)) else None

    requests = (
        request_factory(
            root,
            operation_id="request_homebrew_install",
            operation_ref="setup::homebrew.request_install",
        ),
        request_factory(
            root,
            operation_id="install_postgresql",
            operation_ref="bootstrap::postgres.install",
        ),
        request_factory(
            root,
            operation_id="configure_postgresql",
            operation_ref="bootstrap::postgres.configure_solet",
            public_inputs={"solet_name": "adaptertest"},
        ),
    )
    outcomes = [execute(module, request, runner, which) for request in requests]
    check(
        all(outcome["checkpoint_status"] == "verified" for outcome in outcomes),
        "absolute Homebrew fallback verifies every Homebrew-gated operation",
    )
    check(
        all(outcome["error_kind"] != "homebrew_missing" for outcome in outcomes),
        "absolute Homebrew fallback never reports Homebrew missing",
    )
    check(
        [brew, "--version"] in calls,
        "absolute Homebrew fallback executes its version probe",
    )
