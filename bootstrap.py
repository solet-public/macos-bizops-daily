#!/usr/bin/env python3
"""Layer 0 — stdlib-only genesis bootstrap shim.

STRICTLY stdlib. No third-party imports, not even transitively (verified
by `tests/... bootstrap_stdlib_only_smoke.py`'s clean-interpreter AST
check). Lives outside any platform-importing package, at the repo root,
because it runs BEFORE the venv exists — it cannot import anything that
imports `ananta` or a plugin.

Three-layer architecture (design doc §2 — the two boundary lines are
load-bearing):
  * Layer -1 (README + the driving agent's shell) confirms/installs git
    and Python 3.13 — the two things needed to even reach this file.
  * Layer 0 (THIS FILE) confirms/prepares HOST prerequisites: Homebrew,
    Postgres server + pgvector extension, this solet's OWN
    non-superuser role AND its own database (both named after it, from
    SOLET_NAME) + PUBLIC-connect revoke on that db + localhost
    default-scram auth (NO credential VALUE — the scram password is
    generated and vault-stored by Layer 1, in-venv, because the vault
    substrate needs the venv to exist), and the local LM Server + `nomic`
    embeddings endpoint. Then creates the venv and installs the MINIMAL
    SEED (`ananta` + `github_midwife_plugin`), fail-loud. Then hands off
    in-venv.
  * Layer 1 (`github_midwife_plugin`, in-venv) completes the
    profile-driven allowlist install, materializes the profile, seeds
    the scram password, launches, and installs autostart.

No layer installs the dependency it needs to exist to run.

Adaptive host-state (operator ruling 2026-07-09, build spec §10.2): each
probe below reports one of a SMALL, NAMED set of states (an `enum.Enum`)
rather than a boolean or free-text message, so a driving agent (and
Slice H's README ladders) can branch on named states instead of parsing
prose. Every step is: probe first (read-only) → if already healthy,
skip → else print the exact command(s) it is about to run and confirm
via the injectable `confirm` callback → act → re-probe to verify.
Divergent, non-golden-path states (wrong Postgres version, a
non-Homebrew install, a pre-existing role in an inconsistent state,
Homebrew itself absent) are surfaced as `needs_user_action` — a
SANCTIONED stop-and-ask, not a failure — never auto-"fixed" by force
(no uninstall, no initdb over existing data, no role/db drops; reuse a
healthy compatible install rather than force a parallel one).

Invocation: `python3.13 bootstrap.py`. Layer -1 already confirmed the
interpreter is 3.13 before this file ever runs; per the KB "Critical
Development Guidelines v2" ("always assume Python 3.13; no version checks or compatibility code for older
Python"), this file does not re-check or re-exec for that precondition.
"""

from __future__ import annotations

import enum
import getpass
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

_REPOSITORY_ROOT = Path(__file__).resolve().parent
if str(_REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPOSITORY_ROOT))

from bootstrap_adapter.models import AdapterError, AdapterRuntime  # noqa: E402
from bootstrap_adapter.postgres import atomic_write_scram_hba, postgres_binaries  # noqa: E402
from bootstrap_adapter.protocol import resolve_brew_executable  # noqa: E402

__all__ = ["execute_adapter_request"]


def execute_adapter_request(raw: object, **kwargs: Any) -> dict[str, Any]:
    """Load the stdlib adapter facade only when its transport is requested."""

    from bootstrap_adapter import execute_adapter_request as execute

    return execute(raw, **kwargs)


def operation_adapter_main() -> int:
    """Preserve the historical script while lazily entering adapter mode."""

    from bootstrap_adapter import operation_adapter_main as run_adapter

    return run_adapter()

# The solet-name grammar, inlined from
# `github_midwife_plugin.constants.NAME_PATTERN` (== `is_valid_solet_name`).
# Layer 0 is STRICTLY stdlib and runs BEFORE the venv exists, so it cannot import
# the plugin -- this duplicate is deliberate (same rationale as the inlined
# BUILD_BACKEND_PACKAGES list below); keep the two in exact sync. `fullmatch`
# (not `match`): `$` matches just before a trailing newline, so `match` would let
# `"x\nhost=evil"` through -- but this name is interpolated straight into the
# admin psql catalog probes / `REVOKE` SQL / `createuser`/`createdb` argv below,
# BEFORE the advertised Layer-1 validation, so it must fail closed here first.
_NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_-]{1,62}$")


def _require_solet_name() -> str:
    """This solet's name IS its Postgres database name (operator ruling
    2026-07-11: "database per solet, named after it"). Layer -1 sets
    SOLET_NAME for the whole bootstrap->genesis chain; bootstrap CONSUMES
    it and fails loud rather than defaulting -- a silent default would create a
    mis-named database the newborn's state plugin never connects to. Resolved
    at import (like _ADMIN_ROLE below) so every db-touching step in this file
    sees one identity, and the process refuses to start without a name.

    Validated against `_NAME_PATTERN` HERE, at the sole derivation boundary, so
    every downstream SQL/argv site (the pg_roles/pg_database probes, the ACL
    probe, the `REVOKE` on the quoted db identifier, createuser/createdb) sees an
    already-safe name -- a malformed or malicious SOLET_NAME (quotes,
    semicolons, spaces, a leading hyphen) can never reach the admin psql layer.
    This is the Layer-0 half of the fix for the same injection class the Layer-1
    validator already closed; the pattern forbids the quote/space/semicolon
    metacharacters those sinks would otherwise be vulnerable to.
    """
    name = os.environ.get("SOLET_NAME", "").strip()
    if not name:
        raise RuntimeError("SOLET_NAME env var is required -- it is this solet's database name (database per solet, named after it). The driving agent must export it for the bootstrap->genesis chain.")
    if not _NAME_PATTERN.fullmatch(name):
        raise RuntimeError(
            f"SOLET_NAME {name!r} is not a valid solet name: it must "
            f"match {_NAME_PATTERN.pattern} (a lowercase letter, then 1-62 chars "
            "from [a-z0-9_-]). This name is used verbatim as a Postgres role, "
            "database, and schema identifier -- names with quotes, semicolons, "
            "spaces, or a leading hyphen are refused before any database call."
        )
    return name


_OPERATION_ADAPTER_FLAG = "--operation-adapter"
# The ordinary bootstrap contract still fails at import when SOLET_NAME is
# absent.  The adapter transport is the one deliberate exception: its closed
# request carries the name, so binding it from ambient process state would
# create two competing identities.  `_bind_adapter_solet_name` validates and
# installs that single request identity before any database helper can run.
_DATABASE = "" if _OPERATION_ADAPTER_FLAG in sys.argv[1:] else _require_solet_name()
# This solet's OWN Postgres role. db = schema = role = SOLET_NAME
# (operator per-solet-isolation ruling, 2026-07-12): a non-superuser role
# named after the solet, owning its own database. The same single identity
# as _DATABASE -- no second derivation, no shared cluster role.
_ROLE_NAME = _DATABASE
# The Postgres admin/superuser role. Homebrew Postgres initdb's the superuser
# as the OS login user (KB 20/03), so resolve it dynamically rather than
# hardcoding an operator identity -- getpass.getuser() is env-first
# (LOGNAME/USER/...) then the pwd database, and raises OSError if none resolve
# (that raise IS the fail-loud; genesis has no sudo path). This is what lets a
# newborn bootstrap on any machine, not just the operator's.
_ADMIN_ROLE = getpass.getuser()
_SEED_PACKAGE_NAME = "github_midwife_plugin"
# The seed plugin's pyproject.toml pins macos_vault_plugin (an HONEST pin:
# genesis imports its keychain at module level, before profile_install's
# allowlist pass could ever supply it), so Layer 0 must editable-install the
# local copy BEFORE the seed plugin -- pip otherwise tries to resolve the pin
# from PyPI and fails ("no versions"). macos_vault_plugin itself pins only
# `ananta` locally, so the three-package order below is the full closure.
# Cold-agent acceptance finding F-5, 2026-07-12.
_VAULT_PACKAGE_NAME = "macos_vault_plugin"
_MESSAGING_PACKAGE_NAME = "agent_messaging_plugin"
_REQUIRED_DISTRIBUTIONS: tuple[tuple[str, str], ...] = (
    ("solet-setup-contracts", "solet_setup_contracts"),
    ("ananta", "ananta"),
    ("macos-vault-plugin", f"plugins/{_VAULT_PACKAGE_NAME}"),
    (_SEED_PACKAGE_NAME, f"plugins/{_SEED_PACKAGE_NAME}"),
    (_MESSAGING_PACKAGE_NAME, f"plugins/{_MESSAGING_PACKAGE_NAME}"),
)
_SUPPORTED_POSTGRES_MAJOR = 17
_LM_SERVER_BASE_URL = "http://localhost:1234/v1"
# The exact serve-time identifier openai_embeddings_plugin sends as `model`. A substring
# match on "nomic" is not sufficient: several nomic-embed-text-v1.5 builds exist, they are
# not interchangeable (f16 vs Q4_K_M produce different vectors), and the wrong one produces
# no fail-loud signal downstream. See
# plugins/github_midwife_plugin/knowledge_base/profile_templates/lm_studio_models.yaml.
_REQUIRED_EMBEDDING_MODEL_ID = "text-embedding-nomic-embed-text-v1.5-embedding"
_PROBE_TIMEOUT_S = 10
_INSTALL_TIMEOUT_S = 300
_ASSUME_YES_ENV = "SOLET_ASSUME_YES"
_FORMULA_KEG_MARKER = "/Cellar/solet/"
_FINGERPRINT_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")
_ADAPTER_ID_PATTERN = re.compile(r"^[a-z][a-z0-9_.-]{1,127}$")
_ADAPTER_REF_PATTERN = re.compile(r"^[a-z][a-z0-9_]*::[a-z][a-z0-9_.]*$")
_ADAPTER_INPUT_KEY_PATTERN = re.compile(r"^[a-z][a-z0-9_]{1,127}$")
_SECRET_FIELD_PATTERN = re.compile(
    r"password|secret|token|credential|private_key|oauth_code",
    re.IGNORECASE,
)

# The default-scram pg_hba block (KB 20/03; per-role isolation R3,
# 2026-07-12), inserted immediately ABOVE the blanket `trust` block
# (pg_hba is first-match-wins). Two halves:
#   * admin-trust: the OS login superuser (_ADMIN_ROLE) stays on `trust`
#     so `psql -U <admin>` needs no password -- re-asserted here because
#     the `all all scram` lines below would otherwise catch it too.
#   * all-databases scram: EVERY other role (each per-solet role) is
#     password-gated over localhost, with NO per-role and NO per-db line.
#     A per-role or per-db line would leave the NEXT solet's role/db
#     un-gated (silently passwordless) -- the fall-through class this
#     structurally kills. Any per-solet db is covered with zero
#     per-birth hba edits. An existing machine's prior lines (e.g. a previous
#     own `ananta` scram lines) are left byte-identical -- this only
#     INSERTS, never rewrites.
_DEFAULT_SCRAM_LINES: tuple[str, ...] = (
    f"local   all     {_ADMIN_ROLE}                                 trust",
    f"host    all     {_ADMIN_ROLE}         127.0.0.1/32            trust",
    f"host    all     {_ADMIN_ROLE}         ::1/128                 trust",
    "local   all     all                                     scram-sha-256",
    "host    all     all             127.0.0.1/32            scram-sha-256",
    "host    all     all             ::1/128                 scram-sha-256",
)


class BootstrapError(RuntimeError):
    """Raised when a step cannot complete safely (a hard failure, not a stop-and-ask)."""


class HomebrewState(enum.Enum):
    PRESENT = "present"
    ABSENT = "absent"


class PostgresState(enum.Enum):
    ABSENT = "absent"
    RUNNING_HEALTHY_COMPATIBLE = "running_healthy_compatible"
    RUNNING_WRONG_VERSION = "running_wrong_version"
    PRESENT_NOT_RUNNING = "present_not_running"
    NON_HOMEBREW_INSTALL = "non_homebrew_install"


class PgvectorState(enum.Enum):
    AVAILABLE = "available"
    NOT_INSTALLED = "not_installed"


class RoleDbState(enum.Enum):
    ABSENT = "absent"
    PRESENT_HEALTHY = "present_healthy"
    ROLE_EXISTS_UNKNOWN_PASSWORD = "role_exists_unknown_password"


class LMServerState(enum.Enum):
    RUNNING_CORRECT_MODEL = "running_correct_model"
    RUNNING_NO_MATCHING_MODEL = "running_no_matching_model"
    ABSENT = "absent"


class VenvState(enum.Enum):
    ABSENT = "absent"
    INCOMPLETE = "incomplete"
    INTERPRETER_DANGLING = "interpreter_dangling"
    PRESENT_CLOSED = "present_closed"


Runner = Callable[..., subprocess.CompletedProcess[str]]
Confirmer = Callable[[str], bool]


def confirm_interactive(message: str) -> bool:
    """Real confirmer: print the message, prompt on stdin. Default for a real run.

    Agent-driven runs that have already inspected the printed action plan can
    opt in explicitly with ``SOLET_ASSUME_YES=1``. This is intentionally an
    environment flag rather than blind ``yes |`` piping: the transcript records
    that the driver meant to approve bootstrap's named, probe-derived actions.

    A non-interactive stdin (an agent-driven run with no live TTY) raises
    EOFError from input(); that is a DECLINE, not a crash — the calling step
    surfaces its own `needs_user_action` naming what was declined, and a
    re-run with a terminal or ``SOLET_ASSUME_YES=1`` resumes at the same
    step. Caught live by the 2026-07-12 cold-agent seed acceptance test.
    """
    print(message)
    if os.environ.get(_ASSUME_YES_ENV, "").strip().lower() in {"1", "true", "yes", "y"}:
        print(f"{_ASSUME_YES_ENV}=1 set -- proceeding without stdin prompt.")
        return True
    try:
        reply = input("Proceed? [y/N] ").strip().lower()
    except EOFError:
        print("stdin is not interactive -- treating as decline. Re-run from a terminal, or pipe an explicit 'y' to confirm this step.")
        return False
    return reply in ("y", "yes")


def _default_http_get(url: str, timeout: int) -> bytes:
    with urllib.request.urlopen(url, timeout=timeout) as response:  # noqa: S310
        return response.read()


HttpGetter = Callable[[str, int], bytes]


@dataclass
class BootstrapContext:
    """Mutable orchestration ledger threaded through each step.

    `target`/`venv_dir` are caller-supplied -- no cwd assumption.
    `run`/`confirm`/`http_get` are injectable so every step is testable
    offline against a fixture tree with a mocked subprocess, an
    auto-answering confirmer, and a fake embeddings-endpoint response --
    no real brew/psql/venv/network touched in a smoke.
    """

    target: Path
    run: Runner
    confirm: Confirmer
    http_get: HttpGetter = _default_http_get
    venv_dir: Path = field(init=False)
    steps: list[dict[str, Any]] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.venv_dir = self.target / ".venv"


def _print_command(cmd: Sequence[str]) -> None:
    print(f"$ {' '.join(cmd)}")


# ── Homebrew ─────────────────────────────────────────────────────────


def _resolution_runtime(ctx: BootstrapContext) -> AdapterRuntime:
    """Adapt Layer 0's injected seams to the shared executable resolvers."""

    return AdapterRuntime(ctx.run, shutil.which, datetime.now, _DATABASE, ctx.target)


def _brew_executable(ctx: BootstrapContext) -> str | None:
    return resolve_brew_executable(_resolution_runtime(ctx))


def _postgres_executables(ctx: BootstrapContext) -> dict[str, str | None]:
    return postgres_binaries(_resolution_runtime(ctx))


def _postgres_command(ctx: BootstrapContext, executable: str, *arguments: str) -> list[str]:
    resolved = _postgres_executables(ctx)[executable]
    if resolved is None:
        raise BootstrapError(f"PostgreSQL executable `{executable}` is unavailable")
    return [resolved, *arguments]


def _run_host_command(
    ctx: BootstrapContext,
    command: list[str],
    *,
    timeout: int,
) -> subprocess.CompletedProcess[str]:
    try:
        return ctx.run(command, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise BootstrapError(f"host executable `{command[0]}` could not run: {exc}") from exc


def _run_confirmed_brew_step(
    ctx: BootstrapContext,
    *,
    step_name: str,
    state: str,
    command_arguments: Sequence[Sequence[str]],
    message_prefix: str,
    declined_detail: str,
    completed_state: str,
) -> dict[str, Any]:
    brew = _brew_executable(ctx)
    if brew is None:
        return {
            "step_name": step_name,
            "status": "needs_user_action",
            "state": state,
            "detail": "Homebrew executable is unavailable",
        }
    commands = [[brew, *arguments] for arguments in command_arguments]
    message = f"{message_prefix}\n  " + "\n  ".join(f"$ {' '.join(command)}" for command in commands)
    if not ctx.confirm(message):
        return {
            "step_name": step_name,
            "status": "needs_user_action",
            "state": state,
            "detail": declined_detail,
        }
    for command in commands:
        _print_command(command)
        result = _run_host_command(ctx, command, timeout=_INSTALL_TIMEOUT_S)
        if result.returncode != 0:
            raise BootstrapError(f"`{' '.join(command)}` failed (exit {result.returncode})")
    return {"step_name": step_name, "status": "completed", "state": completed_state}


def probe_homebrew(ctx: BootstrapContext) -> HomebrewState:
    return HomebrewState.PRESENT if _brew_executable(ctx) is not None else HomebrewState.ABSENT


def ensure_homebrew(ctx: BootstrapContext) -> dict[str, Any]:
    """Homebrew absence is ALWAYS a stop-and-ask -- bootstrap.py never
    pipes Homebrew's installer script itself (the exact curl|bash trust
    boundary genesis exists to avoid, per the design doc's framing).
    """
    if probe_homebrew(ctx) is HomebrewState.PRESENT:
        return {"step_name": "homebrew", "status": "skipped", "state": "present"}
    return {
        "step_name": "homebrew",
        "status": "needs_user_action",
        "state": "absent",
        "detail": ("Homebrew not found on PATH. Install it yourself from https://brew.sh (bootstrap.py does not auto-run installer scripts fetched from the internet), then re-run bootstrap.py."),
    }


# ── Postgres + pgvector ─────────────────────────────────────────────


def _psql_version_major(ctx: BootstrapContext) -> int | None:
    psql = _postgres_executables(ctx)["psql"]
    if psql is None:
        return None
    try:
        result = _run_host_command(ctx, [psql, "--version"], timeout=_PROBE_TIMEOUT_S)
    except BootstrapError:
        return None
    match = re.search(r"(\d+)(?:\.\d+)*", result.stdout)
    return int(match.group(1)) if match else None


def _postgres_accepting_connections(ctx: BootstrapContext) -> bool:
    pg_isready = _postgres_executables(ctx)["pg_isready"]
    if pg_isready is None:
        return False
    try:
        result = _run_host_command(
            ctx,
            [pg_isready, "-h", "localhost", "-p", "5432"],
            timeout=_PROBE_TIMEOUT_S,
        )
    except BootstrapError:
        return False
    return result.returncode == 0


def _postgres_is_homebrew_managed(ctx: BootstrapContext) -> bool:
    brew = _brew_executable(ctx)
    if brew is None:
        return False
    try:
        result = _run_host_command(ctx, [brew, "list", "--formula"], timeout=_PROBE_TIMEOUT_S)
    except BootstrapError:
        return False
    return any(line.startswith("postgresql") for line in result.stdout.splitlines())


def probe_postgres(ctx: BootstrapContext) -> tuple[PostgresState, str]:
    major = _psql_version_major(ctx)
    if major is None:
        return PostgresState.ABSENT, "no resolved `psql` executable"
    if not _postgres_is_homebrew_managed(ctx):
        return PostgresState.NON_HOMEBREW_INSTALL, (f"psql {major}.x found but not Homebrew-managed (Postgres.app / EDB / other install channel) -- adaptive handling for that channel is out of scope for the golden path.")
    if not _postgres_accepting_connections(ctx):
        return PostgresState.PRESENT_NOT_RUNNING, "Homebrew postgresql installed but not accepting connections"
    if major != _SUPPORTED_POSTGRES_MAJOR:
        return PostgresState.RUNNING_WRONG_VERSION, (f"running major version {major}; supported major is {_SUPPORTED_POSTGRES_MAJOR}")
    return PostgresState.RUNNING_HEALTHY_COMPATIBLE, f"running Homebrew postgresql {major}.x"


def ensure_postgres(ctx: BootstrapContext) -> dict[str, Any]:
    state, detail = probe_postgres(ctx)
    if state is PostgresState.RUNNING_HEALTHY_COMPATIBLE:
        return {"step_name": "postgres", "status": "skipped", "state": state.value, "detail": detail}
    if state in (PostgresState.NON_HOMEBREW_INSTALL, PostgresState.RUNNING_WRONG_VERSION):
        return {
            "step_name": "postgres",
            "status": "needs_user_action",
            "state": state.value,
            "detail": (f"{detail}. Reuse a healthy compatible install rather than force a parallel one -- present the upgrade-vs-parallel-install decision to the user; bootstrap.py never uninstalls or overwrites an existing install."),
        }
    if state is PostgresState.PRESENT_NOT_RUNNING:
        return _run_confirmed_brew_step(
            ctx,
            step_name="postgres",
            state=state.value,
            command_arguments=(("services", "start", "postgresql"),),
            message_prefix="Homebrew postgresql is installed but not running. Will run:",
            declined_detail="user declined to start postgresql",
            completed_state="started",
        )

    # ABSENT: install via Homebrew.
    return _run_confirmed_brew_step(
        ctx,
        step_name="postgres",
        state=state.value,
        command_arguments=(
            ("install", "postgresql"),
            ("services", "start", "postgresql"),
        ),
        message_prefix="Postgres not found. Will run:",
        declined_detail="user declined to install postgresql",
        completed_state="installed_and_started",
    )


def probe_pgvector(ctx: BootstrapContext) -> PgvectorState:
    try:
        result = _run_host_command(
            ctx,
            _postgres_command(ctx, "psql", "-U", _ADMIN_ROLE, "-d", "postgres", "-tAc", "SELECT 1 FROM pg_available_extensions WHERE name='vector'"),
            timeout=_PROBE_TIMEOUT_S,
        )
    except BootstrapError:
        return PgvectorState.NOT_INSTALLED
    return PgvectorState.AVAILABLE if result.stdout.strip() == "1" else PgvectorState.NOT_INSTALLED


def ensure_pgvector(ctx: BootstrapContext) -> dict[str, Any]:
    state = probe_pgvector(ctx)
    if state is PgvectorState.AVAILABLE:
        return {"step_name": "pgvector", "status": "skipped", "state": state.value}
    completed = _run_confirmed_brew_step(
        ctx,
        step_name="pgvector",
        state=state.value,
        command_arguments=(("install", "pgvector"),),
        message_prefix="pgvector extension not available. Will run:",
        declined_detail="user declined to install pgvector",
        completed_state="installed",
    )
    if completed["status"] != "completed":
        return completed
    state_after = probe_pgvector(ctx)
    if state_after is not PgvectorState.AVAILABLE:
        raise BootstrapError("pgvector install reported success but the extension is still not available")
    return completed


# ── Role + database + scram (NO credential value -- Layer 1's job) ─


def probe_role_and_db(ctx: BootstrapContext) -> RoleDbState:
    role_exists = (
        _run_host_command(
            ctx,
            _postgres_command(ctx, "psql", "-U", _ADMIN_ROLE, "-d", "postgres", "-tAc", f"SELECT 1 FROM pg_roles WHERE rolname='{_ROLE_NAME}'"),
            timeout=_PROBE_TIMEOUT_S,
        ).stdout.strip()
        == "1"
    )
    db_exists = (
        _run_host_command(
            ctx,
            _postgres_command(ctx, "psql", "-U", _ADMIN_ROLE, "-d", "postgres", "-tAc", f"SELECT 1 FROM pg_database WHERE datname='{_DATABASE}'"),
            timeout=_PROBE_TIMEOUT_S,
        ).stdout.strip()
        == "1"
    )
    if role_exists and db_exists:
        return RoleDbState.PRESENT_HEALTHY
    if not role_exists and not db_exists:
        return RoleDbState.ABSENT
    # Exactly one of (this solet's own role, its own db) exists -- a
    # genuinely INCONSISTENT partial state under per-role isolation (each
    # solet's role AND db are BOTH named after it, so a clean second
    # solet on an already-provisioned machine is fully ABSENT here, not
    # half-present -- it takes the normal create path below). Layer 0 never
    # drops/resets anything it did not create, so this partial state surfaces
    # as needs_user_action rather than being auto-reconciled.
    return RoleDbState.ROLE_EXISTS_UNKNOWN_PASSWORD


def _pg_hba_path(ctx: BootstrapContext) -> Path | None:
    brew = _brew_executable(ctx)
    if brew is None:
        return None
    try:
        prefix = _run_host_command(ctx, [brew, "--prefix"], timeout=_PROBE_TIMEOUT_S).stdout.strip()
    except BootstrapError:
        return None
    if not prefix:
        return None
    candidates = sorted(Path(prefix, "var").glob("postgresql@*"))
    if not candidates:
        return None
    return candidates[-1] / "pg_hba.conf"


def _public_connect_revoked(ctx: BootstrapContext) -> bool:
    """True iff the R4 PUBLIC revoke is in effect on this solet's db.

    A NULL `datacl` is Postgres's built-in default ACL -- PUBLIC holds
    CONNECT+TEMP -- and any grantee-less aclitem (rendered `=...`) is an
    explicit PUBLIC grant. Either way a sibling solet's role could open a
    connection, the exact hole R4 closes (cold-run finding D3,
    2026-07-13: the create path bundled the revoke but a manually-reconciled
    PRESENT_HEALTHY db skipped it silently).
    """
    out = _run_host_command(
        ctx,
        _postgres_command(ctx, "psql", "-U", _ADMIN_ROLE, "-d", "postgres", "-tAc", f"SELECT COALESCE(datacl::text, '') FROM pg_database WHERE datname='{_DATABASE}'"),
        timeout=_PROBE_TIMEOUT_S,
    ).stdout.strip()
    if not out:
        return False
    entries = out.strip("{}").split(",")
    return all(not entry.lstrip('"').startswith("=") for entry in entries if entry)


def _scram_lines_present(pg_hba_path: Path) -> bool:
    if not pg_hba_path.is_file():
        return False
    content = pg_hba_path.read_text()
    return all(line in content for line in _DEFAULT_SCRAM_LINES)


def _inconsistent_role_db_report(state: RoleDbState) -> dict[str, Any]:
    return {
        "step_name": "role_and_db",
        "status": "needs_user_action",
        "state": state.value,
        "detail": (
            f"exactly one of (role={_ROLE_NAME!r}, db={_DATABASE!r}) already exists -- "
            "an inconsistent partial state. Under per-solet isolation both are "
            "named after this solet, so a clean second solet on an "
            "already-provisioned machine has NEITHER yet (it takes the normal create "
            "path). bootstrap.py never drops or resets a role/database it did not "
            "create. Reconcile by hand (create the missing half, or drop the stray "
            "one if it is safe), then RE-RUN bootstrap.py."
        ),
    }


def _role_db_action_plan(
    state: RoleDbState,
    *,
    scram_ok: bool,
    revoke_ok: bool,
    vector_ok: bool,
    pg_hba_path: Path,
) -> list[str]:
    """The stop-and-present-facts action list for ensure_role_and_db's confirm."""
    actions: list[str] = []
    if state is RoleDbState.ABSENT:
        actions.append(f"createuser -U {_ADMIN_ROLE} {_ROLE_NAME}  (non-superuser)")
        actions.append(f"createdb -U {_ADMIN_ROLE} -O {_ROLE_NAME} {_DATABASE}")
        actions.append("CREATE EXTENSION IF NOT EXISTS vector  (per-db activation, D12)")
        actions.append(f'REVOKE CONNECT, TEMP ON DATABASE "{_DATABASE}" FROM PUBLIC  (per-solet isolation, R4)')
    else:
        if not revoke_ok:
            # A reconciled/pre-existing db (cold-run finding D3): PUBLIC can still connect.
            # Idempotent, creates/drops nothing -- enforcing R4 on this
            # solet's OWN db is exactly what the ruling's wizard prescribes.
            actions.append(f'REVOKE CONNECT, TEMP ON DATABASE "{_DATABASE}" FROM PUBLIC  (R4 -- missing on this pre-existing db)')
        if not vector_ok:
            # A reconciled/pre-existing db (cold-boot finding D12): brew installing
            # pgvector's files makes the extension AVAILABLE machine-wide but
            # never activates it in any specific database. Idempotent.
            actions.append("CREATE EXTENSION IF NOT EXISTS vector  (D12 -- missing on this pre-existing db)")
    if not scram_ok:
        actions.append(f"insert the default-scram block above the trust block in {pg_hba_path} + reload (KB 20/03, R3)")
    return actions


def _apply_role_db_actions(
    ctx: BootstrapContext,
    state: RoleDbState,
    *,
    scram_ok: bool,
    revoke_ok: bool,
    vector_ok: bool,
    pg_hba_path: Path,
) -> None:
    """Execute exactly what _role_db_action_plan presented (same branch logic)."""
    if state is RoleDbState.ABSENT:
        _create_role_db_and_revoke(ctx)
    else:
        if not revoke_ok:
            _revoke_public_connect(ctx)
        if not vector_ok:
            _create_vector_extension(ctx)
    if not scram_ok:
        _write_default_scram_block(ctx, pg_hba_path)


def ensure_role_and_db(ctx: BootstrapContext) -> dict[str, Any]:
    state = probe_role_and_db(ctx)
    if state is RoleDbState.ROLE_EXISTS_UNKNOWN_PASSWORD:
        return _inconsistent_role_db_report(state)

    pg_hba_path = _pg_hba_path(ctx)
    if pg_hba_path is None:
        raise BootstrapError("could not resolve pg_hba.conf path via `brew --prefix`")

    scram_ok = _scram_lines_present(pg_hba_path)
    # The revoke/vector-extension checks are only probeable (and only
    # meaningful) once the db exists -- the ABSENT create path bundles both
    # into _create_role_db_and_revoke.
    revoke_ok = state is RoleDbState.PRESENT_HEALTHY and _public_connect_revoked(ctx)
    vector_ok = state is RoleDbState.PRESENT_HEALTHY and _vector_extension_installed(ctx)

    if state is RoleDbState.PRESENT_HEALTHY and scram_ok and revoke_ok and vector_ok:
        return {"step_name": "role_and_db", "status": "skipped", "state": "present_healthy_scram_revoke_and_vector_configured"}

    actions = _role_db_action_plan(state, scram_ok=scram_ok, revoke_ok=revoke_ok, vector_ok=vector_ok, pg_hba_path=pg_hba_path)
    if not ctx.confirm("Will perform:\n  " + "\n  ".join(actions)):
        return {"step_name": "role_and_db", "status": "needs_user_action", "state": state.value, "detail": "user declined role/db/scram setup"}

    _apply_role_db_actions(ctx, state, scram_ok=scram_ok, revoke_ok=revoke_ok, vector_ok=vector_ok, pg_hba_path=pg_hba_path)
    return {"step_name": "role_and_db", "status": "completed", "state": "ready"}


def _create_role_db_and_revoke(ctx: BootstrapContext) -> None:
    """createuser (plain -> non-superuser, R2) + createdb -O + per-db vector
    extension activation (D12) + the R4 PUBLIC revoke, all as the trust-
    superuser admin role. Per-solet isolation (2026-07-12): the newborn
    owns its own db, and only its owner role (plus the admin superuser) may
    connect to it.
    """
    for cmd in (
        _postgres_command(ctx, "createuser", "-U", _ADMIN_ROLE, _ROLE_NAME),
        _postgres_command(ctx, "createdb", "-U", _ADMIN_ROLE, "-O", _ROLE_NAME, _DATABASE),
    ):
        _print_command(cmd)
        result = _run_host_command(ctx, cmd, timeout=_PROBE_TIMEOUT_S)
        if result.returncode != 0:
            raise BootstrapError(
                f"`{' '.join(cmd)}` failed (exit {result.returncode}). "
                f"bootstrap.py assumes the Homebrew Postgres convention that the "
                f"superuser is your OS login user (resolved here as {_ADMIN_ROLE!r} "
                f"via getpass.getuser()); if that role cannot create the "
                f"role/database, initialize Homebrew Postgres under your login "
                f"user or grant it superuser."
            )
    # D12 (a cold-boot finding, 2026-07-13): a freshly created database
    # never has ANY extension activated -- ensure_pgvector only makes the
    # extension's FILES available machine-wide via brew. Every
    # macos_free_minimal schema that declares a vector column (e.g.
    # session_ledger_summary::embeddings) crash-loops at first boot without
    # this: `psycopg.errors.UndefinedObject: type "vector" does not exist`.
    _create_vector_extension(ctx)
    # R4 (per-solet isolation): close the default PUBLIC-can-connect grant
    # so a SIBLING solet's role cannot even open a connection to this db
    # (its own owner role keeps implicit ALL; the admin superuser bypasses).
    _revoke_public_connect(ctx)


def _revoke_public_connect(ctx: BootstrapContext) -> None:
    """The R4 `REVOKE ... FROM PUBLIC` statement, shared by the create path and
    the reconciled-db repair path (cold-run finding D3). Idempotent."""
    revoke_cmd = _postgres_command(
        ctx,
        "psql",
        "-U",
        _ADMIN_ROLE,
        "-d",
        "postgres",
        "-v",
        "ON_ERROR_STOP=1",
        "-c",
        f'REVOKE CONNECT, TEMP ON DATABASE "{_DATABASE}" FROM PUBLIC;',
    )
    _print_command(revoke_cmd)
    result = _run_host_command(ctx, revoke_cmd, timeout=_PROBE_TIMEOUT_S)
    if result.returncode != 0:
        raise BootstrapError(f"R4 `REVOKE CONNECT, TEMP ON DATABASE {_DATABASE!r} FROM PUBLIC` failed (exit {result.returncode})")


def _vector_extension_installed(ctx: BootstrapContext) -> bool:
    """True iff the ``vector`` extension is CREATEd in THIS solet's own
    database (D12). Extensions are per-database, not per-cluster/per-role --
    ``ensure_pgvector`` installing the pgvector files via brew makes the
    extension AVAILABLE machine-wide but does not activate it in any specific
    database. A freshly createdb'd database never has it.
    """
    out = _run_host_command(
        ctx,
        _postgres_command(ctx, "psql", "-U", _ADMIN_ROLE, "-d", _DATABASE, "-tAc", "SELECT 1 FROM pg_extension WHERE extname='vector'"),
        timeout=_PROBE_TIMEOUT_S,
    ).stdout.strip()
    return out == "1"


def _create_vector_extension(ctx: BootstrapContext) -> None:
    """``CREATE EXTENSION IF NOT EXISTS vector`` on THIS solet's own
    database (D12), as the admin role -- extension activation needs
    superuser or an explicit CREATE grant, and the per-solet owner role
    is deliberately non-superuser (R2). Idempotent -- IF NOT EXISTS makes
    re-running safe, shared by the create path and the reconciled-db repair
    path.
    """
    cmd = _postgres_command(
        ctx,
        "psql",
        "-U",
        _ADMIN_ROLE,
        "-d",
        _DATABASE,
        "-v",
        "ON_ERROR_STOP=1",
        "-c",
        "CREATE EXTENSION IF NOT EXISTS vector;",
    )
    _print_command(cmd)
    result = _run_host_command(ctx, cmd, timeout=_PROBE_TIMEOUT_S)
    if result.returncode != 0:
        raise BootstrapError(f"`CREATE EXTENSION IF NOT EXISTS vector` on database {_DATABASE!r} failed (exit {result.returncode}). Requires the pgvector extension files to be installed machine-wide first (see the pgvector step above) and {_ADMIN_ROLE!r} to have CREATE privilege on the database.")


def _write_default_scram_block(ctx: BootstrapContext, pg_hba_path: Path) -> None:
    """Insert the default-scram block above the blanket trust block (R3) and
    reload. Insert-only (never rewrites existing lines) + reload via
    `pg_reload_conf()`.
    """
    print(f"inserting the default-scram block above the trust block in {pg_hba_path}")
    try:
        atomic_write_scram_hba(pg_hba_path)
    except AdapterError as exc:
        if "safety or layout changed" in str(exc):
            raise BootstrapError(
                "pg_hba.conf layout is unrecognized or unsafe; inspect the file, "
                "extend the recognizer or edit it manually, and re-run bootstrap. "
                "bootstrap will not auto-write an unrecognized authentication file"
            ) from exc
        raise BootstrapError(f"could not safely apply pg_hba.conf SCRAM policy: {exc}") from exc
    except OSError as exc:
        raise BootstrapError(f"could not safely apply pg_hba.conf SCRAM policy: {exc}") from exc
    reload_cmd = _postgres_command(ctx, "psql", "-U", _ADMIN_ROLE, "-d", "postgres", "-c", "SELECT pg_reload_conf();")
    _print_command(reload_cmd)
    result = _run_host_command(ctx, reload_cmd, timeout=_PROBE_TIMEOUT_S)
    if result.returncode != 0:
        raise BootstrapError(f"pg_reload_conf() failed (exit {result.returncode})")


# ── Local embeddings server (LM Server + nomic) ─────────────────────


def probe_lm_server(http_get: HttpGetter) -> LMServerState:
    try:
        raw = http_get(f"{_LM_SERVER_BASE_URL}/models", _PROBE_TIMEOUT_S)
        payload = json.loads(raw)
    except (urllib.error.URLError, TimeoutError, ValueError, OSError):
        return LMServerState.ABSENT
    model_ids = [str(m.get("id", "")) for m in payload.get("data", [])]
    if _REQUIRED_EMBEDDING_MODEL_ID in model_ids:
        return LMServerState.RUNNING_CORRECT_MODEL
    return LMServerState.RUNNING_NO_MATCHING_MODEL


def ensure_lm_server(ctx: BootstrapContext) -> dict[str, Any]:
    """No cloud, no API key, ever -- a local endpoint only. GUI-app setup +
    model pulls are guided (stop-and-ask), never auto-installed: bootstrap.py
    does not attempt to drive LM Studio's UI or assume a CLI is present.
    """
    state = probe_lm_server(ctx.http_get)
    if state is LMServerState.RUNNING_CORRECT_MODEL:
        return {"step_name": "lm_server", "status": "skipped", "state": state.value}
    if state is LMServerState.RUNNING_NO_MATCHING_MODEL:
        return {
            "step_name": "lm_server",
            "status": "needs_user_action",
            "state": state.value,
            "detail": (
                f"LM Server is running but {_REQUIRED_EMBEDDING_MODEL_ID!r} is not loaded. "
                f"Fetch it — the full HF URL is required, the owner/repo shorthand fails:\n"
                f"  lms get \"https://huggingface.co/gaianet/Nomic-embed-text-v1.5-Embedding-GGUF\" --yes"
            ),
        }
    return {
        "step_name": "lm_server",
        "status": "needs_user_action",
        "state": state.value,
        "detail": (
            f"no local embeddings server reachable at {_LM_SERVER_BASE_URL}. Install/launch LM Studio, "
            f"enable server mode, then fetch {_REQUIRED_EMBEDDING_MODEL_ID!r} — the full HF URL is required, "
            f"the owner/repo shorthand fails:\n"
            f"  lms get \"https://huggingface.co/gaianet/Nomic-embed-text-v1.5-Embedding-GGUF\" --yes"
        ),
    }


# ── venv + SEED install (fail-loud, no partial-install continuation) ─


def probe_venv(ctx: BootstrapContext) -> VenvState:
    state, _facts = _probe_dependency_closure(ctx)
    return state


def _probe_dependency_closure(
    ctx: BootstrapContext,
) -> tuple[VenvState, dict[str, bool]]:
    from bootstrap_adapter import ClosureState, probe_dependency_closure

    state, facts = probe_dependency_closure(ctx.target, ctx.run)
    if state is ClosureState.ABSENT:
        return VenvState.ABSENT, facts
    if state is ClosureState.INCOMPLETE:
        return VenvState.INCOMPLETE, facts
    if state is ClosureState.INTERPRETER_DANGLING:
        return VenvState.INTERPRETER_DANGLING, facts
    if state is ClosureState.PRESENT_CLOSED:
        return VenvState.PRESENT_CLOSED, facts
    raise BootstrapError(f"unrecognized dependency-closure state: {state!r}")


def _resolve_long_lived_python(ctx: BootstrapContext, requested: str | None = None) -> str:
    candidates: list[str] = []
    if requested:
        candidates.append(requested)
    discovered = shutil.which("python3.13")
    if discovered:
        candidates.append(discovered)
    candidates.extend(("/opt/homebrew/bin/python3.13", "/usr/local/bin/python3.13"))
    if sys.version_info[:2] == (3, 13):
        candidates.append(sys.executable)
    for candidate in dict.fromkeys(candidates):
        if _FORMULA_KEG_MARKER in candidate or not Path(candidate).is_file():
            continue
        try:
            result = ctx.run(
                [candidate, "--version"],
                capture_output=True,
                text=True,
                timeout=_PROBE_TIMEOUT_S,
            )
        except (OSError, subprocess.TimeoutExpired):
            continue
        if result.returncode == 0 and f"{result.stdout} {result.stderr}".strip().startswith(
            "Python 3.13",
        ):
            return candidate
    raise BootstrapError(
        "no executable long-lived Python 3.13 outside the Solet formula keg is available",
    )


def _apply_dependency_closure(ctx: BootstrapContext, *, base_python: str | None = None) -> None:
    interpreter = _resolve_long_lived_python(ctx, requested=base_python)
    venv_python = ctx.venv_dir / "bin" / "python3"
    venv_cmd = [interpreter, "-m", "venv"]
    if ctx.venv_dir.exists():
        venv_cmd.append("--upgrade")
    venv_cmd.append(str(ctx.venv_dir))
    _run_required(ctx, venv_cmd, "venv construction")
    backend_cmd = [
        str(venv_python),
        "-m",
        "pip",
        "install",
        "--upgrade",
        "pip",
        "setuptools",
        "wheel",
    ]
    _run_required(ctx, backend_cmd, "build-backend installation")
    for _distribution, relative in _REQUIRED_DISTRIBUTIONS:
        package_dir = ctx.target / relative
        if not package_dir.is_dir():
            raise BootstrapError(f"required seed package directory is missing: {relative}")
        install_cmd = [
            str(venv_python),
            "-m",
            "pip",
            "install",
            "--no-build-isolation",
            "-e",
            str(package_dir),
        ]
        _run_required(ctx, install_cmd, f"seed install for {relative}")


def _run_required(ctx: BootstrapContext, command: list[str], label: str) -> None:
    _print_command(command)
    try:
        result = ctx.run(
            command,
            capture_output=True,
            text=True,
            timeout=_INSTALL_TIMEOUT_S,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise BootstrapError(f"{label} could not execute") from exc
    if result.returncode != 0:
        raise BootstrapError(f"{label} failed (exit {result.returncode})")


def ensure_venv_and_seed(ctx: BootstrapContext) -> dict[str, Any]:
    state = probe_venv(ctx)
    if state is VenvState.PRESENT_CLOSED:
        return {"step_name": "venv_and_seed", "status": "skipped", "state": state.value}

    if not ctx.confirm(f"Will create or repair the venv at {ctx.venv_dir} and install the closed seed package set plus its private solet bridge CLI."):
        return {
            "step_name": "venv_and_seed",
            "status": "needs_user_action",
            "state": state.value,
            "detail": "user declined venv dependency-closure construction or repair",
        }

    _apply_dependency_closure(ctx)

    return {"step_name": "venv_and_seed", "status": "completed", "state": "dependency_closure_applied"}


# ── Handoff to Layer 1 ───────────────────────────────────────────────


def handoff(ctx: BootstrapContext) -> dict[str, Any]:
    venv_python = ctx.venv_dir / "bin" / "python3"
    cmd = [str(venv_python), "-m", "github_midwife_plugin.genesis"]
    _print_command(cmd)
    result = _run_host_command(ctx, cmd, timeout=_INSTALL_TIMEOUT_S)
    if result.returncode != 0:
        # Codex must-fix (2026-07-09): genesis.py's main() prints its
        # "FATAL: ..." diagnostic to STDERR, not stdout -- the prior
        # raise only ever included stdout's tail, so a genesis failure
        # surfaced here as a bare "exit <n>" with no FATAL text at all.
        # Every FATAL-able path in genesis.py traces back to
        # CredentialSeedError / AutostartError / LaunchctlObservationError
        # / ProfileInstallError / GenesisError's own step-machine errors
        # -- all secret-free by construction (the credential paths have
        # dedicated smoke pins proving it) -- so it is safe to surface
        # both tails here.
        tails = [t for t in (result.stderr[-500:], result.stdout[-500:]) if t]
        raise BootstrapError(f"Layer 1 handoff failed (exit {result.returncode}): {' | '.join(tails)}")
    # genesis.py's own stdout carries the vault-passphrase/autostart status
    # and the MCP-registration command (_mcp_register_suggestion) -- this is
    # captured above for the failure path's diagnostics, so it must also be
    # surfaced here on success or the driving agent never sees it.
    if result.stdout:
        print(result.stdout, end="" if result.stdout.endswith("\n") else "\n")
    return {"step_name": "handoff", "status": "completed"}


_StepRunner = Callable[[BootstrapContext], dict[str, Any]]
BOOTSTRAP_STEP_RUNNERS: tuple[tuple[str, _StepRunner], ...] = (
    ("homebrew", ensure_homebrew),
    ("postgres", ensure_postgres),
    ("pgvector", ensure_pgvector),
    ("role_and_db", ensure_role_and_db),
    ("lm_server", ensure_lm_server),
    ("venv_and_seed", ensure_venv_and_seed),
    ("handoff", handoff),
)

_TERMINAL_STATUSES = ("failed", "needs_user_action")


def run_steps(
    ctx: BootstrapContext,
    step_runners: Sequence[tuple[str, _StepRunner]] = BOOTSTRAP_STEP_RUNNERS,
) -> list[dict[str, Any]]:
    """Execute `step_runners` in order; stop at the first failure OR
    stop-and-ask. `step_runners` is injectable (mirrors the Layer 1
    `steps.py` composability shape) -- not currently used for a second
    sequence, but keeps the two layers' orchestration pattern consistent.
    """
    for _step_name, runner in step_runners:
        try:
            record = runner(ctx)
        except BootstrapError as exc:
            record = {"step_name": _step_name, "status": "failed", "error": str(exc)}
        ctx.steps.append(record)
        if str(record.get("status", "")) in _TERMINAL_STATUSES:
            break
    return ctx.steps


def _step_summary_line(step: dict[str, Any]) -> str:
    """One human-readable line per step record. A failed record carries its
    reason under 'error' (run_steps' BootstrapError wrap and every step's own
    failed shape), NOT 'detail' -- the pre-fix chain read detail/state only,
    so failures printed with no reason at all (cold-agent acceptance finding
    F-6, 2026-07-12: venv_and_seed '[failed]' with the pip resolution error
    silently swallowed).
    """
    reason = step.get("detail") or step.get("error") or step.get("state") or ""
    return f"[{step['status']}] {step['step_name']}: {reason}"


def main() -> int:
    if sys.argv[1:] == [_OPERATION_ADAPTER_FLAG]:
        return operation_adapter_main()
    if sys.argv[1:]:
        print(f"unsupported bootstrap arguments: {sys.argv[1:]!r}", file=sys.stderr)
        return 2
    target = Path(__file__).resolve().parent
    ctx = BootstrapContext(target=target, run=subprocess.run, confirm=confirm_interactive)
    steps = run_steps(ctx)
    for step in steps:
        print(_step_summary_line(step))
    last = steps[-1] if steps else {}
    if last.get("status") == "failed":
        return 1
    if last.get("status") == "needs_user_action":
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
