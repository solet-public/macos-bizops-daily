"""Slice C — first-run scram DB-password seed (Layer 1, in-venv).

Adapts `macos_vault_plugin/tools/seed_state_db_password.py`'s pattern
(construct SystemKeychain directly, let `store_credential` do the RFC-2397
encode, verify via round-trip read) to genesis's situation: there is no
pre-existing raw password to derive from — this GENERATES one.

Per build spec §6.1 / design doc §6.5 rider, this constructs
`macos_vault_plugin.keychain.SystemKeychain` DIRECTLY rather than calling
`service_interface::vault_service::store` on a booted runtime — normal
state-plugin startup is itself waiting on this exact credential, so a
post-boot process call would deadlock the bootstrap phase. Only Layer 1
(in-venv, pre-first-boot) can do this; Layer 0 stays stdlib and never
handles a credential value at all.

Per-solet role isolation (operator override, 2026-07-12; Architect
amended ruling `workbench/2026-07-12_per_homunculus_db_role_isolation_ruling.md`):
each solet has its OWN non-superuser Postgres role named after it
(db = schema = role = SOLET_NAME), owning its own database. There is
NO shared cluster role and NO credential ever crosses a solet's
namespace — the shared-`ananta` model and the parent-provisions-child
credential copy it required are both retired. This module ALWAYS seeds the
CURRENT process's own role (`SOLET_NAME`), generating a fresh password
and `ALTER ROLE`-ing that role. A verb-mode newborn runs this exact path in
ITS OWN venv subprocess (`--seed`, SOLET_NAME=<newborn>), same as the
fresh-machine CLI path — never seeded from a parent.

Two keys, one value (Architect F11 ruling, 2026-07-11): this solet's
own role password is stored under TWO vault keys, because
`enforce_namespace` only lets a plugin read keys whose middle `<plugin>`
segment equals its own name, and BOTH owner plugins connect as this
solet's role with the SAME password:
  * `<solet>.postgres_state_management_plugin.db_password`
  * `<solet>.pgvector_service_plugin.password`
They are BY DEFINITION the same value; divergence is a bug. Every mutation
path here writes BOTH keys store-first, and the two-state guard backfills
the pgvector key if a valid postgres key predates it (F11) — shipping the
pgvector connection params without its password key would boot a newborn
whose pgvector crash-loops at readiness.

Sequence (build spec §6.1's steps, STORE-FIRST order per Codex's
2026-07-09 must-fix review — see "Store-first" note below):
  1. SOLET_NAME must be set (checked by SystemKeychain construction
     and re-checked explicitly here for a clearer error).
  2. If a `db_password` entry already exists: probe whether this
     solet's role currently authenticates with it. If yes -> SKIP
     (idempotent no-op; backfill the pgvector key if missing). If no ->
     fall through and ROTATE BOTH by construction (regenerate, ALTER
     ROLE, store --force) -- never one without the other, so the Keychain
     entry and the live role password can never silently diverge.
  3. If no `db_password` entry exists: assume-and-verify the role itself
     exists (wizard step 1's `createuser` ran). Role present -> fresh
     seed. Role ABSENT -> fail loud naming wizard step 1, never a raw
     psql "role does not exist" from the later ALTER.
  4. Generate a fresh password in-process (`secrets.token_urlsafe`) --
     never printed to stdout/logs/the transcript.
  5. `SystemKeychain().store_credential(...)` FIRST for BOTH owner-plugin
     keys (postgres + pgvector), each round-trip verified via
     `retrieve_credential` -- the substrate does the RFC-2397 encode; this
     module never hand-crafts a `data:` URI (the exact mismatch class that
     crash-looped a boot per the build spec).
  6. `ALTER ROLE "<name>" PASSWORD '<pw>'` via the trust-superuser admin
     role, only after the store above verified. The SQL text (which embeds
     the password literal -- ALTER ROLE has no bind-parameter form) is
     passed over the subprocess's STDIN, never as a `-c` argv string, so it
     never appears in `ps` output.
  7. Post-ALTER role-auth probe: confirm the role actually authenticates
     with the new password (against its OWN db) before returning.

Store-first (Codex must-fix, 2026-07-09): the original draft ran ALTER
ROLE before store_credential. A store failure after a successful ALTER
would strand the live role's password -- its ONLY copy dies with the
process, and the role is locked out until an operator manually resets
it via `psql -U <local-admin-role>`. Store-first is self-healing instead: a
store failure leaves nothing changed on the role (fully recoverable, no
side effect to undo); an ALTER failure AFTER a verified store leaves the
Keychain holding a password the role doesn't have YET -- exactly the
"stale stored pw" state step 2's rerun path already repairs (the next
run's auth probe fails against it and rotates again). Overwriting the
Keychain entry never destroys a WORKING credential: the fresh-seed path
has nothing stored yet, and the rotate path only reaches here after the
stored pw already failed the auth probe in step 2.

Isolation self-proof (Architect §5.2/R6.6b, 2026-07-12): after a verb-mode
newborn seeds its own role, the `--seed --isolation-sibling-db <db>` CLI
proves the newborn's role, authenticating with its REAL password, CANNOT
reach a sibling database -- it must be refused with a CONNECT-privilege
denial (`permission denied for database`), which R4's
`REVOKE CONNECT ... FROM PUBLIC` enforces. A password-auth failure there is
NOT proof of isolation (it means the scram gate or the seed is broken); the
classifier distinguishes the two so a scram regression can't masquerade as
isolation. This probe reads ONLY the newborn's own Keychain namespace,
runs in the newborn's own venv, and is verb-mode only (a fresh-machine
first solet has no sibling to be isolated from).

Key-shape verified against the live reader (2026-07-09): the state
plugin's boot-time credential read
(`postgres_state_management_plugin.plugin`) calls
`vault_service.retrieve(key=f"{solet}.postgres_state_management_plugin.db_password")`,
which `macos_vault_plugin`'s `_parse_scoped_key` splits into exactly
`(plugin_name="postgres_state_management_plugin", credential="db_password")`
before calling `SystemKeychain.retrieve_credential(*pair)` -- the same two
arguments this module passes to `store_credential`. No key-shape mismatch.

Importing `macos_vault_plugin.keychain` requires `SOLET_NAME` to be
set (the package's `__init__.py` resolves vault-scoped constants eagerly
at import time) -- this module cannot be imported without it either, which
is consistent with step 1 of the sequence above and with resolving the
role name (== SOLET_NAME) at import.
"""

from __future__ import annotations

import getpass
import os
import secrets
import shutil
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

from macos_vault_plugin.keychain import PerCredentialKeychain, SystemKeychain

from .constants import is_valid_solet_name


class CredentialSeedError(RuntimeError):
    """Raised when the scram DB-password seed cannot complete safely."""


def _require_solet_name() -> str:
    """Resolve + VALIDATE this solet's name at the sole derivation boundary.

    The returned name is used verbatim as `_ROLE_NAME` in the `ALTER ROLE
    "<name>"` SQL (F1) and the `rolname='<name>'` existence probe (F2), both run
    as the trust-superuser admin role. Validating here against
    `NAME_PATTERN` (via `is_valid_solet_name`) fail-closes a name carrying a
    quote/semicolon/space/leading-hyphen BEFORE it can reach any SQL site --
    closing the injection class Codex flagged in `bootstrap.py`, in this Layer-1
    module where it runs as the admin role. The pattern forbids `"`/`'` outright,
    so the double-quoted identifier and single-quoted literal sites can no longer
    be broken out of.
    """
    solet = os.environ.get("SOLET_NAME", "").strip()
    if not solet:
        raise CredentialSeedError(
            "SOLET_NAME env var is required to resolve the per-solet "
            "Keychain service name and Postgres role identity."
        )
    if not is_valid_solet_name(solet):
        raise CredentialSeedError(
            f"SOLET_NAME {solet!r} is not a valid solet name -- it "
            "must be a lowercase letter followed by 1-62 chars from [a-z0-9_-]. "
            "This name is interpolated into admin `ALTER ROLE`/catalog SQL as the "
            "role identity; names with quotes, semicolons, spaces, or a leading "
            "hyphen are refused before any Postgres call."
        )
    return solet


_STATE_PLUGIN = "postgres_state_management_plugin"
_CREDENTIAL = "db_password"
# The pgvector service connects to the SAME per-solet role with the SAME
# password, but `enforce_namespace` only lets a plugin read vault keys whose
# middle `<plugin>` segment equals its own name -- so the one role credential
# is stored under TWO keys, one per owner plugin's namespace. The two keys are
# BY DEFINITION the same value (one role password, two consumer namespaces);
# divergence is a bug, and every mutation path here writes both. pgvector's
# live reader is `<solet>.pgvector_service_plugin.password`
# (`pgvector_service_plugin.plugin._resolve_db_password`).
_PGVECTOR_PLUGIN = "pgvector_service_plugin"
_PGVECTOR_CREDENTIAL = "password"
# This solet's own Postgres role. db = schema = role = SOLET_NAME
# (operator per-solet-isolation ruling, 2026-07-12). Resolved at import
# (like bootstrap.py's `_DATABASE`) so every role-touching site sees one
# identity and the module refuses to import without a name. `_require_solet_name`
# now VALIDATES the name against NAME_PATTERN at resolution, so the double-quoted
# `ALTER ROLE` identifier and the single-quoted `rolname='...'` literal below can
# never be broken out of (F1/F2, 2026-07-19); hyphenated names remain fine.
_ROLE_NAME = _require_solet_name()
# Cluster-global admin database. `ALTER ROLE` and the role-existence probe are
# cluster-wide catalog operations, so they target `postgres` rather than any
# solet db -- pointing them at a per-solet db would add a needless
# ordering edge (the db must exist first). The role-auth probe, by contrast,
# targets this solet's OWN db (its purpose is "role authenticates AND can
# reach my db"), resolved from SOLET_NAME at call time.
_ADMIN_DB = "postgres"
# The Postgres admin/superuser role (Layer 1 side). MUST resolve identically
# to bootstrap.py's Layer 0 _ADMIN_ROLE -- both use getpass.getuser(), the OS
# login user Homebrew Postgres initdb's as the superuser (KB 20/03). A
# hardcoded operator identity here would break the newborn's in-venv credential
# seed on any non-operator machine, and diverge from the role Layer 0 created.
_ADMIN_ROLE = getpass.getuser()
_PW_TOKEN_BYTES = 32
_PSQL_TIMEOUT_S = 15
_POSTGRES_FORMULA = "postgresql@17"
_HOMEBREW_CANDIDATES = (
    "/opt/homebrew/bin/brew",
    "/usr/local/bin/brew",
)

# CLI flags (verb-mode newborn self-seed subprocess). `--seed` runs the full
# self-seed against this process's own SOLET_NAME role; the optional
# `--isolation-sibling-db <db>` adds the post-seed isolation self-proof.
_SEED_FLAG = "--seed"
_ISOLATION_FLAG = "--isolation-sibling-db"


def _resolve_brew_executable() -> str:
    """Resolve a working Homebrew executable when the launch PATH is incomplete."""
    candidates = (shutil.which("brew"), *_HOMEBREW_CANDIDATES)
    for candidate in dict.fromkeys(item for item in candidates if item is not None):
        try:
            completed = subprocess.run(  # noqa: S603
                [candidate, "--version"],
                capture_output=True,
                text=True,
                timeout=_PSQL_TIMEOUT_S,
            )
        except (OSError, subprocess.TimeoutExpired):
            continue
        if completed.returncode == 0:
            return candidate
    raise CredentialSeedError(
        "PostgreSQL 17 psql could not be resolved: Homebrew executable could "
        "not be found. Repair the approved PostgreSQL 17 setup, then re-run genesis."
    )


def _resolve_psql_binary() -> str:
    """Resolve the declared PostgreSQL 17 client without a PATH fallback.

    The setup adapter verifies the Homebrew-managed ``postgresql@17`` formula
    by resolving its authoritative ``brew --prefix`` and invoking binaries
    under that prefix.  Genesis runs after that verified prerequisite, but its
    subprocess environment need not include the keg-only ``bin`` directory.
    Resolve the same formula here rather than guessing a Homebrew prefix or
    silently falling back to a possibly unrelated PATH binary.
    """
    brew = _resolve_brew_executable()
    try:
        prefix_result = subprocess.run(  # noqa: S603
            [brew, "--prefix", _POSTGRES_FORMULA],
            capture_output=True,
            text=True,
            timeout=_PSQL_TIMEOUT_S,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise CredentialSeedError(
            "PostgreSQL 17 psql could not be resolved: `brew --prefix "
            "postgresql@17` could not run. Repair the approved PostgreSQL 17 "
            "setup, then re-run genesis."
        ) from exc
    if prefix_result.returncode != 0:
        raise CredentialSeedError(
            "PostgreSQL 17 psql could not be resolved: `brew --prefix "
            "postgresql@17` failed. Repair the approved PostgreSQL 17 setup, "
            "then re-run genesis."
        )
    prefix = Path(prefix_result.stdout.strip())
    psql = prefix / "bin" / "psql"
    if not prefix.is_absolute() or not psql.is_file() or not os.access(psql, os.X_OK):
        raise CredentialSeedError(
            "PostgreSQL 17 psql could not be resolved: the configured "
            "Homebrew prefix does not contain an executable `bin/psql`. Repair "
            "the approved PostgreSQL 17 setup, then re-run genesis."
        )
    return str(psql)


def _default_alter_role_password(pw: str) -> None:
    """`ALTER ROLE "<name>" PASSWORD '<pw>'` via the trust-superuser admin role.

    Targets `-d postgres` (`_ADMIN_DB`), NOT any per-solet db: `ALTER
    ROLE` is a cluster-wide catalog operation, so pointing it at this
    solet's own db would add a needless ordering edge (that db must be
    created first). Do NOT "fix" this back to the solet db.

    The password is embedded in the SQL text (ALTER ROLE has no
    bind-parameter form), so the SQL is piped over stdin rather than
    passed as a `-c` argv string -- argv is visible to `ps`, stdin is not.
    """
    escaped = pw.replace("'", "''")
    sql = f'ALTER ROLE "{_ROLE_NAME}" PASSWORD \'{escaped}\';\n'
    result = subprocess.run(  # noqa: S603
        [_resolve_psql_binary(), "-U", _ADMIN_ROLE, "-d", _ADMIN_DB, "-v", "ON_ERROR_STOP=1", "-q"],
        input=sql, text=True, capture_output=True, timeout=_PSQL_TIMEOUT_S,
    )
    if result.returncode != 0:
        # Codex must-fix (2026-07-09): psql failure stderr can echo the SQL
        # text back verbatim (e.g. on a syntax error) -- which embeds the
        # password literal. NEVER include raw psql stderr in this message;
        # a generic exit-code diagnostic is enough to point an operator at
        # the Postgres server log, without risking a secret landing in an
        # exception string that a caller might log or print.
        raise CredentialSeedError(
            f"ALTER ROLE {_ROLE_NAME!r} failed (psql exit {result.returncode}); "
            "see the Postgres server log for detail (stderr deliberately "
            "omitted here -- it can echo the failed SQL, which embeds the "
            "password literal)."
        )


def _default_role_authenticates(pw: str) -> bool:
    """Probe whether this solet's role currently authenticates with `pw`
    AND can reach its OWN db.

    Targets `-d <SOLET_NAME>` (the per-solet db, per the
    operator's "database per solet, named after it" ruling), NOT a
    cluster db: the probe's purpose is "the role authenticates and can
    connect to the db this solet will actually use", so it runs
    after the db has been provisioned. The db name is the RAW solet
    name (identical to the role and schema names -- one identity, no
    second derivation); psql receives it as a `-d`/`-U` argv value (data,
    not SQL text), so hyphenated names need no quoting here.

    The password travels via the `PGPASSWORD` environment variable, not
    argv -- `ps` does not show a process's environment by default.
    """
    env = os.environ.copy()
    env["PGPASSWORD"] = pw
    result = subprocess.run(  # noqa: S603
        [_resolve_psql_binary(), "-U", _ROLE_NAME, "-d", _require_solet_name(),
         "-v", "ON_ERROR_STOP=1", "-q", "-c", "SELECT 1;"],
        capture_output=True, text=True, timeout=_PSQL_TIMEOUT_S, env=env,
    )
    return result.returncode == 0


def _default_role_exists() -> bool:
    """Assume-and-verify probe (Architect R5, 2026-07-12): does this
    solet's own role exist? Wizard step 1's `createuser "<name>"`
    creates it BEFORE this in-venv seed ever runs, so an absent role means
    the wizard step was not performed -- fail loud NAMING it, rather than
    letting the later `ALTER ROLE` die with a raw psql "role does not
    exist". Deterministic: one `pg_roles` catalog probe as the
    trust-superuser admin role.

    FAILS on execution error (OSError / timeout / nonzero exit): the caller
    only reaches this probe on the fresh-seed path, and a transient probe
    failure must not be read as "role absent" and route into a fresh seed
    that would then hit the same raw ALTER error this probe exists to
    front-run.
    """
    try:
        result = subprocess.run(  # noqa: S603
            [_resolve_psql_binary(), "-U", _ADMIN_ROLE, "-d", _ADMIN_DB, "-tAc",
             f"SELECT 1 FROM pg_roles WHERE rolname='{_ROLE_NAME}'"],
            capture_output=True, text=True, timeout=_PSQL_TIMEOUT_S,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise CredentialSeedError(
            f"role-existence probe for {_ROLE_NAME!r} could not be executed: {exc}"
        ) from exc
    if result.returncode != 0:
        raise CredentialSeedError(
            f"role-existence probe for {_ROLE_NAME!r} failed (psql exit "
            f"{result.returncode}); see the Postgres server log for detail."
        )
    return result.stdout.strip() == "1"


def _store_and_verify(
    kc: PerCredentialKeychain, plugin: str, credential: str, value: bytes
) -> None:
    """Store one credential and round-trip verify it before returning.

    The substrate does the RFC-2397 encode; a round-trip read confirms the
    stored value matches byte-for-byte, so callers never `ALTER ROLE` (or
    return) against an unverified credential -- the exact mismatch class that
    crash-looped a boot per the build spec.
    """
    kc.store_credential(plugin, credential, value)
    verify = kc.retrieve_credential(plugin, credential)
    if verify != value:
        raise CredentialSeedError(
            f"{plugin}/{credential}: round-trip verification failed after "
            "store_credential -- refusing to proceed with an unverified credential."
        )


def _backfill_pgvector_key(kc: PerCredentialKeychain, value: bytes) -> None:
    """Write the pgvector db-password key from an already-valid postgres-key
    value, IFF the pgvector key is absent.

    Deterministic COMPLETION of the two-keys-one-value invariant (pgvector
    reads its db password from its own vault namespace), NOT lazy
    initialization: a resumed partial birth, or a solet seeded before the
    dual-key change, can hold a valid postgres key while the pgvector key is
    still absent -- and the dual-seed would otherwise silently no-op in exactly
    the state where the missing key must be created. `value` is the postgres
    key's own bytes (the two keys are the same value by definition).
    """
    if not kc.exists_credential(_PGVECTOR_PLUGIN, _PGVECTOR_CREDENTIAL):
        _store_and_verify(kc, _PGVECTOR_PLUGIN, _PGVECTOR_CREDENTIAL, value)


def _should_skip_seeding(
    kc: PerCredentialKeychain,
    probe_fn: Callable[[str], bool],
    role_exists_fn: Callable[[], bool],
) -> bool:
    """Two-state ownership guard (Architect R5, 2026-07-12; dual-key backfill
    2026-07-11). Returns `True` iff `seed_db_password` should be an idempotent
    no-op (own-namespace entry already authenticates -- and the pgvector key is
    backfilled first if it is missing). Returns `False` to proceed to
    generate+store+ALTER (own key stale -> rotate, or own key absent + role
    present -> fresh seed). Raises `CredentialSeedError` when the own key is
    absent AND the role itself does not exist (wizard step 1 not run) --
    extracted from `seed_db_password` to keep its cyclomatic complexity A/B.

    Under per-solet role isolation there is no foreign owner to protect:
    a set-but-unknown password on our OWN role is unambiguously ours to rotate,
    and there is no "prior solet seeded this role" refusal (each role is
    private to one solet).
    """
    if kc.exists_credential(_STATE_PLUGIN, _CREDENTIAL):
        stored = kc.retrieve_credential(_STATE_PLUGIN, _CREDENTIAL)
        if stored is None:
            raise CredentialSeedError(
                f"{_STATE_PLUGIN}/{_CREDENTIAL}: exists_credential reported True "
                "but retrieve_credential returned None -- inconsistent Keychain state."
            )
        # Stored pw is stale relative to the live role -- rotate BOTH keys
        # below, never leaving the Keychain entries and the role password
        # diverged.
        if not probe_fn(stored.decode("utf-8")):
            return False
        # Role already authenticates with the stored pw -> idempotent skip,
        # but first complete the two-keys invariant if the pgvector key is
        # absent (dual-key backfill branch, Architect 2026-07-11).
        _backfill_pgvector_key(kc, stored)
        return True

    # Own-namespace entry absent. Assume-and-verify the role exists (wizard
    # step 1 ran); fail loud naming it if not, rather than a raw psql error
    # deep in the later ALTER.
    if not role_exists_fn():
        raise CredentialSeedError(
            f"Postgres role {_ROLE_NAME!r} does not exist -- wizard step 1 was "
            "not performed. Create this solet's role/database first (see "
            "plugins/github_midwife_plugin/knowledge_base/01_hydration_runbook.md "
            f"'wizard step 1': `createuser -U <admin> \"{_ROLE_NAME}\"` + "
            f"`createdb -U <admin> -O \"{_ROLE_NAME}\" \"{_ROLE_NAME}\"` + pgvector "
            "extension + REVOKE + scram hba), then re-run."
        )
    # Role present, own key absent -- this solet is seeding it fresh.
    return False


def seed_db_password(
    *,
    keychain: PerCredentialKeychain | None = None,
    alter_role_password: Callable[[str], None] | None = None,
    role_authenticates: Callable[[str], bool] | None = None,
    role_exists: Callable[[], bool] | None = None,
) -> None:
    """Generate and seed the scram `db_password` for THIS solet's own
    role, idempotently.

    `keychain`/`alter_role_password`/`role_authenticates`/`role_exists` are
    injectable (default to the real `SystemKeychain` + `psql` subprocess
    calls) so this is testable offline against a `FakeKeychain` and stubbed
    psql outcomes -- no live Postgres or real Keychain touched in a smoke.

    Raises `CredentialSeedError` (before any mutation) when the own-namespace
    entry is absent AND the role does not exist (wizard step 1 not run).
    """
    _require_solet_name()

    kc = keychain if keychain is not None else SystemKeychain()
    alter_fn = alter_role_password if alter_role_password is not None else _default_alter_role_password
    probe_fn = role_authenticates if role_authenticates is not None else _default_role_authenticates
    exists_fn = role_exists if role_exists is not None else _default_role_exists

    if _should_skip_seeding(kc, probe_fn, exists_fn):
        return

    pw = secrets.token_urlsafe(_PW_TOKEN_BYTES)
    pw_bytes = pw.encode("utf-8")

    # Store-first, BOTH keys (see module docstring "Store-first" note): verify
    # both Keychain writes BEFORE touching the live role, so a store failure
    # here has changed nothing about the role's actual password. A second-key
    # store failure before ALTER is fully recoverable (the role is untouched),
    # so the two keys and the live role can never silently diverge.
    _store_and_verify(kc, _STATE_PLUGIN, _CREDENTIAL, pw_bytes)
    _store_and_verify(kc, _PGVECTOR_PLUGIN, _PGVECTOR_CREDENTIAL, pw_bytes)

    alter_fn(pw)

    if not probe_fn(pw):
        raise CredentialSeedError(
            f"{_STATE_PLUGIN}/{_CREDENTIAL}: ALTER ROLE reported success but the "
            "role does not authenticate with the new password -- refusing to "
            "proceed with an inconsistent credential."
        )


def _default_sibling_connect_probe(sibling_db: str, pw: str) -> subprocess.CompletedProcess[str]:
    """Attempt to connect as this solet's role to `sibling_db` with its
    REAL password. Returns the `CompletedProcess` for the classifier to read.

    The password travels via `PGPASSWORD` (env, not argv). This is a real
    connection attempt against a real sibling database; the classifier
    distinguishes the CONNECT-privilege denial (proper isolation) from an
    auth failure or a success.
    """
    env = os.environ.copy()
    env["PGPASSWORD"] = pw
    return subprocess.run(  # noqa: S603
        [_resolve_psql_binary(), "-U", _ROLE_NAME, "-d", sibling_db, "-v", "ON_ERROR_STOP=1",
         "-tAc", "SELECT 1"],
        capture_output=True, text=True, timeout=_PSQL_TIMEOUT_S, env=env,
    )


SiblingConnectProbe = Callable[[str, str], subprocess.CompletedProcess[str]]


def _assert_role_cannot_reach_db(
    sibling_db: str,
    pw: str,
    *,
    connect: SiblingConnectProbe | None = None,
) -> None:
    """Isolation self-proof (Architect §5.2/R6.6b, 2026-07-12): assert this
    solet's role, authenticating with its REAL password, is REFUSED at
    `sibling_db` with a CONNECT-privilege denial.

    Distinguishes three outcomes so a scram regression cannot masquerade as
    isolation:
      * connection SUCCEEDS -> isolation BREACH (R4 revoke not in effect).
      * `permission denied for database` -> correctly isolated (return).
      * `password authentication failed` -> INCONCLUSIVE: the auth gate or the
        seed is broken, so this probe cannot confirm the CONNECT boundary.
      * any other failure -> unexpected; refuse to claim isolation.
    """
    probe = connect if connect is not None else _default_sibling_connect_probe
    result = probe(sibling_db, pw)
    if result.returncode == 0:
        raise CredentialSeedError(
            f"ISOLATION BREACH: role {_ROLE_NAME!r} CONNECTED to sibling database "
            f"{sibling_db!r} with its own password -- `REVOKE CONNECT, TEMP ON "
            f"DATABASE \"{sibling_db}\" FROM PUBLIC` (R4) is not in effect."
        )
    stderr = (result.stderr or "").lower()
    if "permission denied for database" in stderr:
        return
    if "password authentication failed" in stderr:
        raise CredentialSeedError(
            f"isolation probe INCONCLUSIVE for {sibling_db!r}: role {_ROLE_NAME!r} "
            "hit a password-authentication failure, NOT a CONNECT-privilege denial "
            "-- the scram gate or the seed is broken, so isolation cannot be "
            "confirmed (a scram regression must not read as isolation)."
        )
    raise CredentialSeedError(
        f"isolation probe for {sibling_db!r} got an unexpected psql failure "
        f"(exit {result.returncode}); expected `permission denied for database`."
    )


def _run_seed(sibling_db: str | None) -> int:
    """CLI worker (verb-mode newborn self-seed subprocess). Seeds THIS
    process's own role, then -- if `sibling_db` is given -- proves the role
    cannot reach that sibling db (post-seed isolation self-proof). Never
    prints the password; a failure message never echoes a credential value.
    """
    try:
        seed_db_password()
        if sibling_db is not None:
            kc = SystemKeychain()
            pw = kc.retrieve_credential(_STATE_PLUGIN, _CREDENTIAL)
            if pw is None:
                raise CredentialSeedError(
                    f"{_STATE_PLUGIN}/{_CREDENTIAL} absent immediately after seed -- "
                    "cannot run the isolation self-proof."
                )
            _assert_role_cannot_reach_db(sibling_db, pw.decode("utf-8"))
    except CredentialSeedError as exc:
        print(f"FATAL: {exc}", file=sys.stderr)
        return 1
    return 0


def _parse_seed_argv(argv: list[str]) -> tuple[bool, str | None]:
    """Parse `[--seed [--isolation-sibling-db <db>]]`. Returns
    `(is_seed, sibling_db)`; `is_seed` False signals a usage error.
    """
    if argv == [_SEED_FLAG]:
        return True, None
    if len(argv) == 3 and argv[0] == _SEED_FLAG and argv[1] == _ISOLATION_FLAG and argv[2]:
        return True, argv[2]
    return False, None


if __name__ == "__main__":
    is_seed, sibling = _parse_seed_argv(sys.argv[1:])
    if not is_seed:
        print(
            f"usage: python -m github_midwife_plugin.credential_seed {_SEED_FLAG} "
            f"[{_ISOLATION_FLAG} <sibling-db>]",
            file=sys.stderr,
        )
        sys.exit(2)
    sys.exit(_run_seed(sibling))
