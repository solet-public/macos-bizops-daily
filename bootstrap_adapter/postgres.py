"""Homebrew PostgreSQL probes, plans, and approved host actions."""

from __future__ import annotations

import getpass
import ipaddress
import os
import re
import stat
import subprocess
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from .homebrew import CommandExecutionError, CommandOutcome
from .models import (
    INSTALL_TIMEOUT_SECONDS,
    SUPPORTED_POSTGRES_MAJOR,
    AdapterError,
    AdapterRuntime,
    PostgresObservation,
    RolePolicyObservation,
)
from .protocol import evidence, planned_action, resolve_brew_executable, run_public

_ADMIN_ROLE = getpass.getuser()
_POSTGRES_BINARIES = ("psql", "pg_isready", "createuser", "createdb")
_HOST_HBA_TYPES = frozenset(("host", "hostgssenc", "hostnogssenc", "hostnossl", "hostssl"))
_COMMAND_OUTPUT_LIMIT = 16_384
_POSTGRES_READY_TIMEOUT_SECONDS = 10
_POSTGRES_READY_POLL_SECONDS = 0.1
type HbaRecord = tuple[str, str, str, str | None, str]


def _default_scram_lines() -> tuple[str, ...]:
    return (
        f"local   all     {_ADMIN_ROLE}                                 trust",
        f"host    all     {_ADMIN_ROLE}         127.0.0.1/32            trust",
        f"host    all     {_ADMIN_ROLE}         ::1/128                 trust",
        "local   all     all                                     scram-sha-256",
        "host    all     all             127.0.0.1/32            scram-sha-256",
        "host    all     all             ::1/128                 scram-sha-256",
    )


def postgres_observation(runtime: AdapterRuntime) -> PostgresObservation:
    brew = resolve_brew_executable(runtime)
    brew_present = brew is not None
    formulas: list[str] = []
    if brew_present:
        formulas = _homebrew_formulas(runtime, brew)
    managed = any(name == "postgresql" or name.startswith("postgresql@") for name in formulas)
    binaries = postgres_binaries(runtime, formulas)
    psql = binaries["psql"]
    psql_present = psql is not None
    major = _postgres_major(runtime, psql)
    ready = _postgres_ready(runtime, binaries["pg_isready"])
    pgvector_available = _pgvector_available(runtime, psql) if ready else None
    return PostgresObservation(
        brew_present,
        brew,
        psql_present,
        managed,
        major,
        ready,
        pgvector_available,
    )


def _homebrew_formulas(runtime: AdapterRuntime, brew: str) -> list[str]:
    completed = run_public(runtime, [brew, "list", "--formula"])
    if completed is None or completed.returncode != 0:
        return []
    return completed.stdout.splitlines()


def postgres_binaries(
    runtime: AdapterRuntime,
    formulas: Sequence[str] | None = None,
) -> dict[str, str | None]:
    """Resolve one coherent PostgreSQL binary family through the runtime seam.

    An installed Homebrew formula may be keg-only, so its authoritative prefix
    takes precedence.  Prefer the supported major; otherwise select the
    highest installed versioned formula, then the unversioned formula.  When
    no formula is installed (or its prefix cannot be resolved), PATH remains
    the fallback.
    """

    known_formulas = _known_postgres_formulas(runtime, formulas)
    bin_directory = _installed_postgres_keg_bin(runtime, known_formulas)
    if bin_directory is None:
        return _path_postgres_binaries(runtime)
    return _keg_postgres_binaries(runtime, bin_directory)


def _known_postgres_formulas(
    runtime: AdapterRuntime,
    formulas: Sequence[str] | None,
) -> Sequence[str]:
    if formulas is not None:
        return formulas
    brew = resolve_brew_executable(runtime)
    return _homebrew_formulas(runtime, brew) if brew is not None else []


def _installed_postgres_formula(formulas: Sequence[str]) -> str | None:
    supported = f"postgresql@{SUPPORTED_POSTGRES_MAJOR}"
    if supported in formulas:
        return supported
    versioned = [(int(match.group(1)), formula) for formula in formulas if (match := re.fullmatch(r"postgresql@(\d+)", formula)) is not None]
    if versioned:
        return max(versioned)[1]
    return "postgresql" if "postgresql" in formulas else None


def _installed_postgres_keg_bin(runtime: AdapterRuntime, formulas: Sequence[str]) -> Path | None:
    formula = _installed_postgres_formula(formulas)
    if formula is None:
        return None
    brew = resolve_brew_executable(runtime)
    if brew is None:
        return None
    completed = run_public(runtime, [brew, "--prefix", formula])
    if completed is None or completed.returncode != 0:
        return None
    prefix = Path(completed.stdout.strip())
    if not prefix.is_absolute():
        return None
    return prefix / "bin"


def _path_postgres_binaries(runtime: AdapterRuntime) -> dict[str, str | None]:
    return {name: runtime.which(name) for name in _POSTGRES_BINARIES}


def _keg_postgres_binaries(
    runtime: AdapterRuntime,
    bin_directory: Path,
) -> dict[str, str | None]:
    return {name: str(bin_directory / name) if runtime.which(str(bin_directory / name)) is not None else runtime.which(name) for name in _POSTGRES_BINARIES}


def _postgres_command(
    binaries: dict[str, str | None],
    binary: str,
    arguments: Sequence[str],
) -> list[str]:
    resolved = binaries[binary]
    if resolved is None:
        raise AdapterError(f"PostgreSQL binary {binary} is unavailable")
    return [resolved, *arguments]


def _postgres_major(runtime: AdapterRuntime, psql: str | None) -> int | None:
    if psql is None:
        return None
    completed = run_public(runtime, [psql, "--version"])
    if completed is None or completed.returncode != 0:
        return None
    match = re.search(r"(\d+)(?:\.\d+)*", completed.stdout)
    return int(match.group(1)) if match else None


def _postgres_ready(runtime: AdapterRuntime, pg_isready: str | None) -> bool:
    if pg_isready is None:
        return False
    completed = run_public(runtime, [pg_isready, "-h", "localhost", "-p", "5432"])
    return completed is not None and completed.returncode == 0


def wait_for_postgres_ready(runtime: AdapterRuntime) -> bool:
    """Poll the existing readiness check for the bounded post-start window."""

    pg_isready = postgres_binaries(runtime)["pg_isready"]
    deadline = time.monotonic() + _POSTGRES_READY_TIMEOUT_SECONDS
    while True:
        if _postgres_ready(runtime, pg_isready):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(_POSTGRES_READY_POLL_SECONDS)


def _pgvector_available(runtime: AdapterRuntime, psql: str | None) -> bool | None:
    if psql is None:
        return None
    completed = run_public(
        runtime,
        [
            psql,
            "-U",
            _ADMIN_ROLE,
            "-d",
            "postgres",
            "-tAc",
            "SELECT 1 FROM pg_available_extensions WHERE name='vector'",
        ],
    )
    if completed is None or completed.returncode != 0:
        return None
    return completed.stdout.strip() == "1"


def psql_scalar(
    runtime: AdapterRuntime,
    *,
    database: str,
    statement: str,
    binaries: dict[str, str | None] | None = None,
) -> tuple[bool, str]:
    resolved_binaries = binaries if binaries is not None else postgres_binaries(runtime)
    psql = resolved_binaries["psql"]
    if psql is None:
        return False, ""
    completed = run_public(
        runtime,
        [psql, "-U", _ADMIN_ROLE, "-d", database, "-tAc", statement],
    )
    if completed is None or completed.returncode != 0:
        return False, ""
    return True, completed.stdout.strip()


def _resolve_pg_hba(runtime: AdapterRuntime) -> Path | None:
    brew = resolve_brew_executable(runtime)
    if brew is None:
        return None
    completed = run_public(runtime, [brew, "--prefix"])
    if completed is None or completed.returncode != 0:
        return None
    prefix = completed.stdout.strip()
    if not prefix or not Path(prefix).is_absolute():
        return None
    candidates = [directory / "pg_hba.conf" for directory in sorted((Path(prefix) / "var").glob("postgresql*")) if (directory / "pg_hba.conf").exists() or (directory / "pg_hba.conf").is_symlink()]
    return candidates[0] if len(candidates) == 1 else None


def _safe_hba_content(path: Path | None) -> str | None:
    if path is None:
        return None
    try:
        info = path.lstat()
    except OSError:
        return None
    safe_mode = stat.S_IMODE(info.st_mode) & 0o133 == 0
    if not stat.S_ISREG(info.st_mode) or path.is_symlink() or info.st_uid != os.getuid() or not safe_mode:
        return None
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return None


def _active_hba_records(content: str) -> tuple[HbaRecord, ...]:
    """Parse active simple pg_hba records, excluding comments and blanks."""

    records: list[HbaRecord] = []
    for raw_line in content.splitlines():
        parts = raw_line.split("#", 1)[0].split()
        if not parts:
            continue
        connection_type = parts[0].lower()
        if connection_type == "local" and len(parts) >= 4:
            records.append((connection_type, parts[1], parts[2], None, parts[3].lower()))
        elif connection_type in _HOST_HBA_TYPES and len(parts) >= 5:
            records.append((connection_type, parts[1], parts[2], parts[3], parts[4].lower()))
    return tuple(records)


def _recognized_trust_layout(records: Sequence[HbaRecord]) -> bool:
    blanket_auth = [record for record in records if record[1:3] == ("all", "all")]
    return bool(blanket_auth) and all(record[4] == "trust" for record in blanket_auth)


def _hba_address_matches(address: str | None, expected: str) -> bool:
    if address in {"samehost", "samenet"}:
        return True
    if address is None:
        return False
    try:
        return ipaddress.ip_address(expected) in ipaddress.ip_network(address, strict=False)
    except ValueError:
        # An unrecognized address syntax is not proof that it cannot preempt
        # the required localhost rule, so fail closed.
        return True


def _first_matching_hba_method(
    records: Sequence[HbaRecord],
    *,
    connection_type: str,
    address: str | None,
) -> str | None:
    for record_type, database, user, record_address, method in records:
        if database != "all" or user != "all":
            continue
        if connection_type == "local":
            if record_type == "local":
                return method
            continue
        if record_type in {"host", connection_type} and address is not None and _hba_address_matches(record_address, address):
            return method
    return None


def _effective_default_scram(records: Sequence[HbaRecord]) -> bool:
    required_connections = (
        ("local", None),
        ("hostssl", "127.0.0.1"),
        ("hostnossl", "127.0.0.1"),
        ("hostgssenc", "127.0.0.1"),
        ("hostnogssenc", "127.0.0.1"),
        ("hostssl", "::1"),
        ("hostnossl", "::1"),
        ("hostgssenc", "::1"),
        ("hostnogssenc", "::1"),
    )
    return all(_first_matching_hba_method(records, connection_type=connection_type, address=address) == "scram-sha-256" for connection_type, address in required_connections)


def _inspect_pg_hba(path: Path | None) -> tuple[bool, bool, bool]:
    """Return safe-file, recognized-layout, and complete-SCRAM facts."""

    content = _safe_hba_content(path)
    if content is None:
        return False, False, False
    records = _active_hba_records(content)
    if _effective_default_scram(records):
        return True, True, True
    return True, _recognized_trust_layout(records), False


def _base_policy(
    runtime: AdapterRuntime,
    *,
    role_exists: bool,
    database_exists: bool,
    hba_path: Path | None,
    hba_facts: tuple[bool, bool, bool],
    error_kind: str | None = None,
) -> RolePolicyObservation:
    hba_safe, hba_recognized, scram_present = hba_facts
    return RolePolicyObservation(
        role_exists,
        database_exists,
        None,
        None,
        False if not role_exists else None,
        None,
        None,
        None,
        hba_path,
        hba_safe,
        hba_recognized,
        scram_present,
        error_kind,
    )


def role_policy_observation(runtime: AdapterRuntime) -> RolePolicyObservation:
    hba_path = _resolve_pg_hba(runtime)
    hba_facts = _inspect_pg_hba(hba_path)
    binaries = postgres_binaries(runtime)
    ok_role, role_raw = psql_scalar(
        runtime,
        database="postgres",
        statement=f"SELECT 1 FROM pg_roles WHERE rolname='{runtime.name}'",
        binaries=binaries,
    )
    ok_db, db_raw = psql_scalar(
        runtime,
        database="postgres",
        statement=f"SELECT 1 FROM pg_database WHERE datname='{runtime.name}'",
        binaries=binaries,
    )
    if not ok_role or not ok_db:
        return _base_policy(
            runtime,
            role_exists=False,
            database_exists=False,
            hba_path=hba_path,
            hba_facts=hba_facts,
            error_kind="postgres_catalog_probe_failed",
        )
    role_exists = role_raw == "1"
    database_exists = db_raw == "1"
    if role_exists != database_exists:
        return _base_policy(
            runtime,
            role_exists=role_exists,
            database_exists=database_exists,
            hba_path=hba_path,
            hba_facts=hba_facts,
            error_kind="postgres_role_database_inconsistent",
        )
    if not role_exists:
        return _base_policy(
            runtime,
            role_exists=False,
            database_exists=False,
            hba_path=hba_path,
            hba_facts=hba_facts,
        )
    return _existing_role_policy(runtime, hba_path, hba_facts, binaries)


def _existing_role_policy(
    runtime: AdapterRuntime,
    hba_path: Path | None,
    hba_facts: tuple[bool, bool, bool],
    binaries: dict[str, str | None],
) -> RolePolicyObservation:
    name = runtime.name
    probes = (
        psql_scalar(
            runtime,
            database="postgres",
            statement=(f"SELECT rolsuper,rolcreaterole,rolcreatedb,rolreplication,rolbypassrls FROM pg_roles WHERE rolname='{name}'"),
            binaries=binaries,
        ),
        psql_scalar(
            runtime,
            database="postgres",
            statement=f"SELECT pg_get_userbyid(datdba) FROM pg_database WHERE datname='{name}'",
            binaries=binaries,
        ),
        psql_scalar(
            runtime,
            database=name,
            statement=f"SELECT pg_get_userbyid(nspowner) FROM pg_namespace WHERE nspname='{name}'",
            binaries=binaries,
        ),
        psql_scalar(
            runtime,
            database="postgres",
            statement=f"SELECT COALESCE(datacl::text, '') FROM pg_database WHERE datname='{name}'",
            binaries=binaries,
        ),
        psql_scalar(
            runtime,
            database=name,
            statement="SELECT 1 FROM pg_extension WHERE extname='vector'",
            binaries=binaries,
        ),
    )
    if not all(ok for ok, _value in probes):
        return _base_policy(
            runtime,
            role_exists=True,
            database_exists=True,
            hba_path=hba_path,
            hba_facts=hba_facts,
            error_kind="postgres_policy_probe_failed",
        )
    role_flags, database_owner, schema_owner, acl_text, vector_raw = (value for _ok, value in probes)
    acl_entries = acl_text.strip("{}").split(",") if acl_text else []
    public_revoked = bool(acl_text) and all(not entry.lstrip('"').startswith("=") for entry in acl_entries if entry)
    hba_safe, hba_recognized, scram_present = hba_facts
    return RolePolicyObservation(
        True,
        True,
        role_flags == "f|f|f|f|f",
        database_owner == name,
        bool(schema_owner),
        schema_owner == name if schema_owner else None,
        public_revoked,
        vector_raw == "1",
        hba_path,
        hba_safe,
        hba_recognized,
        scram_present,
    )


def postgres_install_actions(observed: PostgresObservation) -> list[dict[str, Any]]:
    actions: list[dict[str, Any]] = []
    if not observed.psql_present and not observed.homebrew_managed:
        actions.append(
            planned_action(
                "postgres.install_homebrew_formula",
                "Install PostgreSQL 17 with Homebrew",
                "package_install",
                "homebrew:postgresql@17",
                "postgres_binary_absent",
            ),
        )
    if not observed.ready:
        actions.append(
            planned_action(
                "postgres.start_homebrew_service",
                "Start the PostgreSQL 17 Homebrew service",
                "service_start",
                "homebrew:postgresql@17",
                "postgres_service_not_running",
            ),
        )
    # A stopped service leaves package discovery unknown.  The planned service
    # start can make that absence observable mid-apply, so unknown must still
    # schedule the package action before approval rather than expanding it
    # after the service comes up.
    if observed.pgvector_available is not True:
        actions.append(
            planned_action(
                "postgres.install_pgvector_formula",
                "Install the pgvector Homebrew formula",
                "package_install",
                "homebrew:pgvector",
                "pgvector_not_available",
            ),
        )
    return actions


def pgvector_package_available(observed: PostgresObservation) -> bool:
    """Return only a positive package discovery result as available.

    A stopped service leaves discovery unknown; it must never satisfy the
    install operation's postcondition merely because no absence was observed.
    """

    return observed.pgvector_available is True


def postgres_configure_actions(
    observed: PostgresObservation,
    policy: RolePolicyObservation | None,
    name: str,
) -> list[dict[str, Any]]:
    if not observed.ready or policy is None:
        return [
            planned_action(
                "postgres.start_homebrew_service",
                "Start the PostgreSQL 17 Homebrew service",
                "service_start",
                "homebrew:postgresql@17",
                "postgres_service_not_running",
            ),
        ]
    actions: list[dict[str, Any]] = []
    if not policy.role_exists:
        actions.extend(
            (
                planned_action(
                    "postgres.create_role",
                    "Create the non-superuser per-solet PostgreSQL role",
                    "database_role_create",
                    f"postgres-role:{name}",
                    "postgres_role_absent",
                ),
                planned_action(
                    "postgres.create_database",
                    "Create the per-solet PostgreSQL database",
                    "database_create",
                    f"postgres-database:{name}",
                    "postgres_database_absent",
                ),
            ),
        )
    conditional = (
        (
            policy.schema_exists is not True,
            ("postgres.create_schema", "Create the per-solet owned PostgreSQL schema", "database_schema_create", f"postgres-schema:{name}", "postgres_schema_absent"),
        ),
        (
            policy.vector_installed is not True,
            ("postgres.activate_pgvector", "Activate pgvector in the per-solet database", "database_extension_create", f"postgres-database:{name}", "pgvector_not_active"),
        ),
        (
            policy.public_connect_revoked is not True,
            ("postgres.revoke_public_connect", "Revoke PUBLIC connect and temporary privileges", "database_acl_change", f"postgres-database:{name}", "postgres_public_connect_open"),
        ),
    )
    actions.extend(planned_action(*spec) for needed, spec in conditional if needed)
    if not policy.scram_present:
        actions.extend(
            (
                planned_action(
                    "postgres.edit_pg_hba_scram",
                    "Require SCRAM authentication in pg_hba.conf",
                    "file_edit",
                    "$POSTGRES_DATA/pg_hba.conf",
                    "postgres_hba_scram_invalid",
                ),
                planned_action(
                    "postgres.reload_configuration",
                    "Reload PostgreSQL authentication configuration",
                    "service_reload",
                    "postgresql:configuration",
                    "postgres_hba_changed",
                ),
            ),
        )
    return actions


def postgres_evidence(
    runtime: AdapterRuntime,
    observed: PostgresObservation,
) -> list[dict[str, Any]]:
    facts: tuple[tuple[str, bool | int | None, bool | int], ...] = (
        ("homebrew_present", observed.brew_present, True),
        ("homebrew_managed", observed.homebrew_managed, True),
        ("postgres_major", observed.major, SUPPORTED_POSTGRES_MAJOR),
        ("postgres_ready", observed.ready, True),
        ("pgvector_available", observed.pgvector_available, True),
    )
    return [
        evidence(
            runtime,
            evidence_id=f"postgres.{name}",
            kind="postgres_probe",
            status="verified" if value == expected else "pending",
            summary=f"PostgreSQL host fact {name} was probed without mutation.",
            observed=value,
            expected=expected,
            source="localhost-postgresql",
        )
        for name, value, expected in facts
    ] + [
        evidence(
            runtime,
            evidence_id="postgres.homebrew_executable",
            kind="executable_resolution",
            status="verified" if observed.brew_path is not None else "pending",
            summary="Homebrew was resolved before PostgreSQL actions were planned.",
            observed=observed.brew_path,
            expected="absolute executable path",
            source=observed.brew_path or "unresolved",
        )
    ]


def role_policy_evidence(
    runtime: AdapterRuntime,
    policy: RolePolicyObservation,
) -> list[dict[str, Any]]:
    facts = (
        ("role_exists", policy.role_exists),
        ("database_exists", policy.database_exists),
        ("role_safe", policy.role_safe),
        ("database_owner_matches", policy.database_owner_matches),
        ("schema_exists", policy.schema_exists),
        ("schema_owner_matches", policy.schema_owner_matches),
        ("public_connect_revoked", policy.public_connect_revoked),
        ("vector_installed", policy.vector_installed),
        ("hba_safe", policy.hba_safe),
        ("hba_layout_recognized", policy.hba_layout_recognized),
        ("scram_present", policy.scram_present),
    )
    return [
        evidence(
            runtime,
            evidence_id=f"postgres_policy.{name}",
            kind="postgres_policy",
            status="verified" if value is True else "pending",
            summary=f"Per-solet PostgreSQL policy fact {name} was checked.",
            observed=value,
            expected=True,
            source="localhost-postgresql",
        )
        for name, value in facts
    ]


def postgres_incompatibility(observed: PostgresObservation) -> str | None:
    if not observed.brew_present:
        return "homebrew_missing"
    if observed.psql_present and not observed.homebrew_managed:
        return "postgres_non_homebrew_install"
    if observed.major is not None and observed.major != SUPPORTED_POSTGRES_MAJOR:
        return "postgres_wrong_major"
    if observed.homebrew_managed and observed.major is None:
        return "postgres_binary_unavailable"
    return None


def unsafe_policy_error(policy: RolePolicyObservation) -> str | None:
    checks = (
        (policy.error_kind is not None, policy.error_kind),
        (not policy.hba_safe, "postgres_hba_unsafe_file"),
        (not policy.hba_layout_recognized, "postgres_hba_unrecognized_layout"),
        (policy.role_exists and policy.role_safe is not True, "postgres_role_privilege_mismatch"),
        (policy.database_exists and policy.database_owner_matches is not True, "postgres_database_owner_mismatch"),
        (policy.schema_exists and policy.schema_owner_matches is not True, "postgres_schema_owner_mismatch"),
    )
    return next((error for failed, error in checks if failed), None)


def run_required(
    runtime: AdapterRuntime,
    command: list[str],
    label: str,
) -> CommandOutcome:
    started = time.monotonic()
    try:
        completed = runtime.run(
            command,
            capture_output=True,
            text=True,
            timeout=INSTALL_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as exc:
        raise CommandExecutionError(
            f"{label} timed out",
            outcome=CommandOutcome(
                None,
                True,
                max(0, int((time.monotonic() - started) * 1000)),
                "",
                "",
            ),
        ) from exc
    except OSError as exc:
        raise CommandExecutionError(
            f"{label} could not execute",
            outcome=CommandOutcome(
                None,
                False,
                max(0, int((time.monotonic() - started) * 1000)),
                "",
                "",
            ),
        ) from exc
    outcome = CommandOutcome(
        completed.returncode,
        False,
        max(0, int((time.monotonic() - started) * 1000)),
        completed.stdout[:_COMMAND_OUTPUT_LIMIT],
        completed.stderr[:_COMMAND_OUTPUT_LIMIT],
    )
    if completed.returncode != 0:
        diagnostics = completed.stderr.strip() or completed.stdout.strip()
        detail = f": {diagnostics}" if diagnostics else ""
        raise CommandExecutionError(f"{label} failed (exit {completed.returncode}){detail}", outcome=outcome)
    return outcome


def apply_postgres_configuration(
    runtime: AdapterRuntime,
    policy: RolePolicyObservation,
    actions: Sequence[dict[str, Any]],
) -> None:
    name = runtime.name
    action_ids = {str(item["id"]) for item in actions}
    binaries = postgres_binaries(runtime)
    if "postgres.create_role" in action_ids:
        run_required(
            runtime,
            _postgres_command(binaries, "createuser", ["-U", _ADMIN_ROLE, name]),
            "PostgreSQL role creation",
        )
        run_required(
            runtime,
            _postgres_command(binaries, "createdb", ["-U", _ADMIN_ROLE, "-O", name, name]),
            "PostgreSQL database creation",
        )
    if "postgres.create_schema" in action_ids:
        run_required(
            runtime,
            _postgres_command(binaries, "psql", ["-U", _ADMIN_ROLE, "-d", name, "-v", "ON_ERROR_STOP=1", "-c", f'CREATE SCHEMA IF NOT EXISTS "{name}" AUTHORIZATION "{name}";']),
            "PostgreSQL schema creation",
        )
    if "postgres.activate_pgvector" in action_ids:
        run_required(
            runtime,
            _postgres_command(binaries, "psql", ["-U", _ADMIN_ROLE, "-d", name, "-v", "ON_ERROR_STOP=1", "-c", "CREATE EXTENSION IF NOT EXISTS vector;"]),
            "pgvector activation",
        )
    if "postgres.revoke_public_connect" in action_ids:
        run_required(
            runtime,
            _postgres_command(binaries, "psql", ["-U", _ADMIN_ROLE, "-d", "postgres", "-v", "ON_ERROR_STOP=1", "-c", f'REVOKE CONNECT, TEMP ON DATABASE "{name}" FROM PUBLIC;']),
            "PostgreSQL PUBLIC privilege revoke",
        )
    if "postgres.edit_pg_hba_scram" in action_ids:
        if policy.hba_path is None:
            raise AdapterError("pg_hba.conf path disappeared before apply")
        atomic_write_scram_hba(policy.hba_path)
        _safe, _recognized, scram_present = _inspect_pg_hba(policy.hba_path)
        if not scram_present:
            raise AdapterError("pg_hba.conf SCRAM policy verification failed after apply")
    if "postgres.reload_configuration" in action_ids:
        run_required(
            runtime,
            _postgres_command(binaries, "psql", ["-U", _ADMIN_ROLE, "-d", "postgres", "-c", "SELECT pg_reload_conf();"]),
            "PostgreSQL configuration reload",
        )


def _hba_with_default_scram(content: str) -> str:
    lines_required = _default_scram_lines()
    if _effective_default_scram(_active_hba_records(content)):
        return content
    block = "\n".join(lines_required) + "\n"
    lines = content.splitlines(keepends=True)
    for index, line in enumerate(lines):
        parts = line.split()
        if len(parts) >= 4 and parts[1:3] == ["all", "all"] and parts[-1] == "trust":
            lines.insert(index, block)
            return "".join(lines)
    return block + content


def atomic_write_scram_hba(path: Path) -> None:
    safe, recognized, present = _inspect_pg_hba(path)
    if not safe or not recognized:
        raise AdapterError("pg_hba.conf safety or layout changed before apply")
    if present:
        return
    replacement = _hba_with_default_scram(path.read_text(encoding="utf-8"))
    temporary = path.with_name(f".{path.name}.solet-{os.getpid()}")
    if temporary.exists():
        raise AdapterError("pg_hba.conf atomic-write sibling already exists")
    descriptor = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
        stat.S_IMODE(path.stat().st_mode),
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(replacement)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except Exception:
        if temporary.exists():
            temporary.unlink()
        raise
