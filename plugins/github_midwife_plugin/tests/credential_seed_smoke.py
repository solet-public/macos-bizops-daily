"""Slice C smoke — per-solet scram DB-password seed: generate/store/
retrieve round-trip, idempotent skip, dual-key backfill, rotate-both, the
two-state guard + role-absent fail-loud, the post-seed isolation self-proof
classifier, the "never printed" guarantee, and the 2026-07-09 Codex must-fix
pins (store-first ordering, no secret-bearing stderr in exceptions).

Per-solet role isolation (operator override, 2026-07-12): each solet
seeds its OWN role (name == SOLET_NAME); the shared-`ananta` model and its
parent-provisions-child credential copy are retired, so this smoke no longer
exercises `provision_db_password` / `--provision-from-stdin` / the pg_authid
"already seeded by another solet" refusal (all deleted). The tri-state
ownership guard is now a TWO-state guard (skip / rotate / fresh) plus a
role-absent fail-loud, and the newborn proves isolation from a sibling db.

Requires SOLET_NAME to be set even to IMPORT
`github_midwife_plugin.credential_seed` (it imports `macos_vault_plugin`, whose
package `__init__.py` resolves vault-scoped constants eagerly, and it resolves
its own role name == SOLET_NAME at import) -- same class of precondition as
`hmac_bearer_token_smoke.py` in the gate register. `subprocess.run` and
`secrets.token_urlsafe` are mocked throughout: no real Postgres, no real
Keychain, and the generated password is a known sentinel so the "never printed"
check has something concrete to search for.

Run directly: ``SOLET_NAME=<name> .venv/bin/python3
plugins/github_midwife_plugin/tests/credential_seed_smoke.py``.
"""

from __future__ import annotations

# ruff: noqa: E402
import getpass
import io
import os
import subprocess
import sys
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

_PLUGIN_ROOT = Path(__file__).resolve().parents[1]
_SRC = _PLUGIN_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

import github_midwife_plugin.credential_seed as _cs_module  # noqa: E402
from fake_keychain import FakeKeychain  # noqa: E402
from github_midwife_plugin.credential_seed import (  # noqa: E402
    _ISOLATION_FLAG,
    _SEED_FLAG,
    CredentialSeedError,
    _assert_role_cannot_reach_db,
    _default_alter_role_password,
    _default_role_authenticates,
    _default_role_exists,
    _default_sibling_connect_probe,
    _parse_seed_argv,
    _resolve_psql_binary,
    seed_db_password,
)

_STATE_PLUGIN = "postgres_state_management_plugin"
_CREDENTIAL = "db_password"
# The second owner-plugin key the dual-key seed writes with the SAME value
# (finding F11, 2026-07-11): pgvector reads its db password from its OWN vault
# namespace (`<h>.pgvector_service_plugin.password`), and enforce_namespace
# stops it reading the postgres key -- so the one role password lives under two
# keys, by definition the same value.
_PGVECTOR_PLUGIN = "pgvector_service_plugin"
_PGVECTOR_CREDENTIAL = "password"
_SENTINEL_PW = "SENTINEL_PW_VALUE_do_not_leak_98765"
_RESOLVED_PSQL = "/opt/homebrew/opt/postgresql@17/bin/psql"

_CHECKS_RUN: list[str] = []


class SmokeFailureError(AssertionError):
    """Raised on any check failure; message is the failure detail."""


def _check(label: str, condition: bool, detail: str = "") -> None:
    _CHECKS_RUN.append(label)
    if not condition:
        raise SmokeFailureError(f"{label}: {detail}")


def _fake_completed(
    returncode: int, stderr: str = "", stdout: str = "",
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


# ── Two-state guard: skip / rotate / fresh / role-absent-fail-loud ──────


def _check_fresh_seed_generates_stores_verifies() -> None:
    kc = FakeKeychain()
    alter_calls: list[str] = []
    with patch(
        "github_midwife_plugin.credential_seed.secrets.token_urlsafe",
        return_value=_SENTINEL_PW,
    ):
        seed_db_password(
            keychain=kc,
            alter_role_password=alter_calls.append,
            # Not reached for the pre-ALTER probe (no prior entry to check);
            # reached for the POST-ALTER probe (store-first order) -- True
            # simulates a successful ALTER that the role now authenticates with.
            role_authenticates=lambda _pw: True,
            # Two-state guard: own key absent AND the role EXISTS (wizard step 1
            # created it) -> fresh-seed-is-ours-to-do.
            role_exists=lambda: True,
        )
    _check(
        "fresh-seed calls ALTER ROLE with the generated password",
        alter_calls == [_SENTINEL_PW],
        f"got {alter_calls!r}",
    )
    stored = kc.retrieve_credential(_STATE_PLUGIN, _CREDENTIAL)
    _check(
        "fresh-seed stores the generated password",
        stored == _SENTINEL_PW.encode("utf-8"),
        f"got {stored!r}",
    )
    _check(
        "fresh-seed dual-writes the pgvector key with the SAME value (F11)",
        kc.retrieve_credential(_PGVECTOR_PLUGIN, _PGVECTOR_CREDENTIAL) == _SENTINEL_PW.encode("utf-8"),
        f"got {kc.retrieve_credential(_PGVECTOR_PLUGIN, _PGVECTOR_CREDENTIAL)!r}",
    )


def _check_role_absent_fails_loud() -> None:
    """Two-state guard, role-absent case (§5.6): own key absent AND the role
    does NOT exist (wizard step 1's createuser was not run) -> fail loud naming
    the role and wizard step 1, never ALTER, never store. Replaces the retired
    pg_authid "seeded by another solet" refusal.
    """
    kc = FakeKeychain()
    alter_calls: list[str] = []
    with patch(
        "github_midwife_plugin.credential_seed.secrets.token_urlsafe",
        return_value=_SENTINEL_PW,
    ):
        try:
            seed_db_password(
                keychain=kc,
                alter_role_password=alter_calls.append,
                role_authenticates=lambda _pw: False,
                role_exists=lambda: False,
            )
        except CredentialSeedError as exc:
            _check(
                "role-absent fail-loud names the role and wizard step 1",
                "does not exist" in str(exc) and "wizard step 1" in str(exc),
                str(exc),
            )
        else:
            raise SmokeFailureError("role-absent-fail-loud: did not raise")
    _check("role-absent never calls ALTER ROLE", alter_calls == [], f"got {alter_calls!r}")
    _check(
        "role-absent never stores anything in the Keychain",
        kc.retrieve_credential(_STATE_PLUGIN, _CREDENTIAL) is None,
        f"got {kc.snapshot()!r}",
    )


def _check_idempotent_skip_when_both_keys_present_and_authenticate() -> None:
    """True no-op: BOTH owner-plugin keys already present and the role
    authenticates -> no ALTER, and the keychain is left completely untouched
    (nothing to backfill)."""
    kc = FakeKeychain()
    kc.store_credential(_STATE_PLUGIN, _CREDENTIAL, b"already-valid-pw")
    kc.store_credential(_PGVECTOR_PLUGIN, _PGVECTOR_CREDENTIAL, b"already-valid-pw")
    alter_calls: list[str] = []
    store_calls_before = kc.snapshot()
    seed_db_password(
        keychain=kc,
        alter_role_password=alter_calls.append,
        role_authenticates=lambda pw: pw == "already-valid-pw",
    )
    _check("both-keys-present skip does not call ALTER ROLE", alter_calls == [], f"got {alter_calls!r}")
    _check(
        "both-keys-present skip leaves the keychain completely untouched (true no-op)",
        kc.snapshot() == store_calls_before,
        "keychain state changed on a no-op skip",
    )


def _check_backfill_pgvector_key_when_postgres_valid_but_pgvector_absent() -> None:
    """Dual-key backfill branch (F11, Architect 2026-07-11): the postgres key is
    valid and the role authenticates, but the pgvector key is ABSENT (a resumed
    partial birth, or a solet seeded before the dual-key change). The seed
    must NOT ALTER ROLE and must NOT touch the postgres key -- but it MUST
    backfill the pgvector key from the SAME value, or pgvector boots without its
    password and crash-loops. Deterministic completion of a definitional
    invariant, not lazy-init.
    """
    kc = FakeKeychain()
    kc.store_credential(_STATE_PLUGIN, _CREDENTIAL, b"already-valid-pw")
    alter_calls: list[str] = []
    seed_db_password(
        keychain=kc,
        alter_role_password=alter_calls.append,
        role_authenticates=lambda pw: pw == "already-valid-pw",
    )
    _check("backfill path never calls ALTER ROLE", alter_calls == [], f"got {alter_calls!r}")
    _check(
        "backfill leaves the valid postgres key untouched",
        kc.retrieve_credential(_STATE_PLUGIN, _CREDENTIAL) == b"already-valid-pw",
        f"got {kc.retrieve_credential(_STATE_PLUGIN, _CREDENTIAL)!r}",
    )
    _check(
        "backfill writes the absent pgvector key from the same (postgres) value",
        kc.retrieve_credential(_PGVECTOR_PLUGIN, _PGVECTOR_CREDENTIAL) == b"already-valid-pw",
        f"got {kc.retrieve_credential(_PGVECTOR_PLUGIN, _PGVECTOR_CREDENTIAL)!r}",
    )


def _check_rotate_both_when_stored_pw_is_stale() -> None:
    kc = FakeKeychain()
    kc.store_credential(_STATE_PLUGIN, _CREDENTIAL, b"stale-pw-role-was-rotated-externally")
    alter_calls: list[str] = []
    with patch(
        "github_midwife_plugin.credential_seed.secrets.token_urlsafe",
        return_value=_SENTINEL_PW,
    ):
        seed_db_password(
            keychain=kc,
            alter_role_password=alter_calls.append,
            role_authenticates=lambda pw: pw != "stale-pw-role-was-rotated-externally",
        )
    _check(
        "rotate-both calls ALTER ROLE with a freshly generated password",
        alter_calls == [_SENTINEL_PW],
        f"got {alter_calls!r}",
    )
    stored = kc.retrieve_credential(_STATE_PLUGIN, _CREDENTIAL)
    _check(
        "rotate-both overwrites the stale credential with the new one",
        stored == _SENTINEL_PW.encode("utf-8"),
        f"got {stored!r}",
    )
    _check(
        "rotate-both rotates BOTH keys to the new value (pgvector key too, F11)",
        kc.retrieve_credential(_PGVECTOR_PLUGIN, _PGVECTOR_CREDENTIAL) == _SENTINEL_PW.encode("utf-8"),
        f"got {kc.retrieve_credential(_PGVECTOR_PLUGIN, _PGVECTOR_CREDENTIAL)!r}",
    )


def _check_inconsistent_keychain_state_raises() -> None:
    class LyingKeychain(FakeKeychain):
        def exists_credential(self, plugin_name: str, credential: str) -> bool:
            return True  # lies: nothing was ever stored

    kc = LyingKeychain()
    try:
        seed_db_password(
            keychain=kc,
            alter_role_password=lambda _pw: None,
            role_authenticates=lambda _pw: True,
        )
    except CredentialSeedError as exc:
        _check("inconsistent-exists-vs-retrieve raises", "inconsistent" in str(exc).lower(), str(exc))
    else:
        raise SmokeFailureError("inconsistent-keychain-state: did not raise")


def _check_roundtrip_mismatch_raises() -> None:
    class CorruptingKeychain(FakeKeychain):
        def retrieve_credential(self, plugin_name: str, credential: str) -> bytes | None:
            real = super().retrieve_credential(plugin_name, credential)
            if real is None:
                return None
            return real + b"-corrupted"

    kc = CorruptingKeychain()
    with patch(
        "github_midwife_plugin.credential_seed.secrets.token_urlsafe",
        return_value=_SENTINEL_PW,
    ):
        try:
            seed_db_password(
                keychain=kc,
                alter_role_password=lambda _pw: None,
                role_authenticates=lambda _pw: False,
                role_exists=lambda: True,
            )
        except CredentialSeedError as exc:
            _check("round-trip-mismatch raises", "round-trip" in str(exc).lower(), str(exc))
        else:
            raise SmokeFailureError("round-trip-mismatch: did not raise")


def _check_solet_name_required() -> None:
    kc = FakeKeychain()
    saved = os.environ.pop("SOLET_NAME", None)
    try:
        try:
            seed_db_password(
                keychain=kc, alter_role_password=lambda _pw: None, role_authenticates=lambda _pw: True,
            )
        except CredentialSeedError as exc:
            _check("SOLET_NAME-required raises", "SOLET_NAME" in str(exc), str(exc))
        else:
            raise SmokeFailureError("solet-name-required: did not raise")
    finally:
        if saved is not None:
            os.environ["SOLET_NAME"] = saved


def _check_nothing_printed() -> None:
    kc = FakeKeychain()
    captured_out, captured_err = io.StringIO(), io.StringIO()
    with patch(
        "github_midwife_plugin.credential_seed.secrets.token_urlsafe",
        return_value=_SENTINEL_PW,
    ), redirect_stdout(captured_out), redirect_stderr(captured_err):
        seed_db_password(
            keychain=kc,
            alter_role_password=lambda _pw: None,
            role_authenticates=lambda _pw: True,
            role_exists=lambda: True,
        )
    combined = captured_out.getvalue() + captured_err.getvalue()
    _check(
        "the generated password never appears on stdout/stderr",
        _SENTINEL_PW not in combined,
        f"leaked: {combined!r}",
    )


# ── psql helpers: ALTER ROLE, role-authenticates, role-exists ───────────


def _check_postgresql17_resolver_is_strict_and_keg_aware() -> None:
    """Red mutations: missing brew fallback, unusable prefix, and non-executable keg.

    The fixture deliberately models the genesis launch PATH where neither
    Homebrew nor keg-only psql is visible. The resolver must find a validated
    standard Homebrew executable before accepting only the approved formula
    prefix; it must reject malformed/missing binaries rather than guessing a
    prefix or falling back to PATH.
    """
    def _keg_only_run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        if command == ["/opt/homebrew/bin/brew", "--version"]:
            return _fake_completed(0, stdout="Homebrew 4.4.0\n")
        if command == ["/opt/homebrew/bin/brew", "--prefix", "postgresql@17"]:
            return _fake_completed(0, stdout="/opt/homebrew/opt/postgresql@17\n")
        raise SmokeFailureError(f"unexpected command: {command!r}")

    with patch("github_midwife_plugin.credential_seed.os.access", return_value=True), patch(
        "github_midwife_plugin.credential_seed.Path.is_file", return_value=True,
    ), patch(
        "github_midwife_plugin.credential_seed.shutil.which", return_value=None,
    ), patch(
        "subprocess.run", side_effect=_keg_only_run,
    ) as mock_run:
        resolved = _resolve_psql_binary()
    _check(
        "bare-brew-and-psql-keg-only resolver reaches the PostgreSQL 17 Homebrew prefix",
        resolved == _RESOLVED_PSQL,
        resolved,
    )
    _check(
        "resolver validates the standard Homebrew fallback before querying its PostgreSQL 17 prefix",
        [call.args[0] for call in mock_run.call_args_list]
        == [
            ["/opt/homebrew/bin/brew", "--version"],
            ["/opt/homebrew/bin/brew", "--prefix", "postgresql@17"],
        ],
        repr([call.args[0] for call in mock_run.call_args_list]),
    )

    with patch("github_midwife_plugin.credential_seed._resolve_brew_executable", return_value="brew"), patch(
        "subprocess.run", return_value=_fake_completed(0, stdout="relative-prefix\n"),
    ):
        try:
            _resolve_psql_binary()
        except CredentialSeedError as exc:
            _check(
                "unresolvable-homebrew-prefix-refusal is named and actionable",
                "PostgreSQL 17 psql" in str(exc) and "Repair" in str(exc),
                str(exc),
            )
        else:
            raise SmokeFailureError("unresolvable-homebrew-prefix-refusal: did not raise")

    with patch("github_midwife_plugin.credential_seed._resolve_brew_executable", return_value="brew"), patch(
        "github_midwife_plugin.credential_seed.Path.is_file", return_value=True,
    ), patch(
        "github_midwife_plugin.credential_seed.os.access", return_value=False,
    ), patch(
        "subprocess.run", return_value=_fake_completed(0, stdout="/opt/homebrew/opt/postgresql@17\n"),
    ):
        try:
            _resolve_psql_binary()
        except CredentialSeedError as exc:
            _check(
                "non-executable-keg-psql-refusal is named and actionable",
                "executable `bin/psql`" in str(exc) and "Repair" in str(exc),
                str(exc),
            )
        else:
            raise SmokeFailureError("non-executable-keg-psql-refusal: did not raise")


def _check_default_alter_role_password_passes_pw_via_stdin_not_argv() -> None:
    with patch("github_midwife_plugin.credential_seed._resolve_psql_binary", return_value=_RESOLVED_PSQL), patch(
        "subprocess.run", return_value=_fake_completed(0),
    ) as mock_run:
        _default_alter_role_password(_SENTINEL_PW)
    _, kwargs = mock_run.call_args
    cmd = mock_run.call_args.args[0]
    _check(
        "ALTER ROLE argv never contains the plaintext password",
        all(_SENTINEL_PW not in str(arg) for arg in cmd),
        f"leaked in argv: {cmd!r}",
    )
    _check(
        "ALTER ROLE password travels via stdin (input=)",
        _SENTINEL_PW in kwargs.get("input", ""),
        "password not found in the piped SQL text",
    )
    _check(
        "role-password psql runs under the resolved getpass admin role (-U <getuser>)",
        "-U" in cmd and getpass.getuser() in cmd,
        f"cmd={cmd!r} getuser={getpass.getuser()!r}",
    )
    _check("ALTER ROLE uses the resolved keg psql argv0", cmd[0] == _RESOLVED_PSQL, repr(cmd))


def _check_default_alter_role_password_raises_on_failure() -> None:
    with patch("github_midwife_plugin.credential_seed._resolve_psql_binary", return_value=_RESOLVED_PSQL), patch(
        "subprocess.run", return_value=_fake_completed(1, stderr="permission denied"),
    ):
        try:
            _default_alter_role_password(_SENTINEL_PW)
        except CredentialSeedError as exc:
            _check("ALTER ROLE failure raises and names the exit code", "exit 1" in str(exc), str(exc))
            # Must-fix 1: stderr content ("permission denied" here) is
            # deliberately NOT echoed into the exception.
            _check(
                "ALTER ROLE failure does NOT echo raw psql stderr",
                "permission denied" not in str(exc),
                str(exc),
            )
        else:
            raise SmokeFailureError("alter-role-failure: did not raise")


def _check_default_role_authenticates_passes_pw_via_env_not_argv() -> None:
    with patch("github_midwife_plugin.credential_seed._resolve_psql_binary", return_value=_RESOLVED_PSQL), patch(
        "subprocess.run", return_value=_fake_completed(0),
    ) as mock_run:
        result = _default_role_authenticates(_SENTINEL_PW)
    cmd = mock_run.call_args.args[0]
    _, kwargs = mock_run.call_args
    _check("role-authenticates returns True on exit 0", result is True)
    _check(
        "role-authenticates argv never contains the plaintext password",
        all(_SENTINEL_PW not in str(arg) for arg in cmd),
        f"leaked in argv: {cmd!r}",
    )
    _check(
        "role-authenticates password travels via PGPASSWORD env",
        kwargs.get("env", {}).get("PGPASSWORD") == _SENTINEL_PW,
        "PGPASSWORD not set correctly",
    )
    _check("role-authenticates uses the resolved keg psql argv0", cmd[0] == _RESOLVED_PSQL, repr(cmd))

    with patch("github_midwife_plugin.credential_seed._resolve_psql_binary", return_value=_RESOLVED_PSQL), patch(
        "subprocess.run", return_value=_fake_completed(2),
    ):
        result_fail = _default_role_authenticates(_SENTINEL_PW)
    _check("role-authenticates returns False on non-zero exit", result_fail is False)


def _check_default_role_exists_parses_psql_output() -> None:
    """The role-existence probe replaces the retired pg_authid ownership probe:
    it queries `pg_roles` for this solet's own role and returns a plain
    bool (row present -> True, absent -> False)."""
    with patch("github_midwife_plugin.credential_seed._resolve_psql_binary", return_value=_RESOLVED_PSQL), patch(
        "subprocess.run", return_value=_fake_completed(0, stdout="1\n"),
    ):
        _check("pg_roles row '1' -> role exists True", _default_role_exists() is True)
    with patch("github_midwife_plugin.credential_seed._resolve_psql_binary", return_value=_RESOLVED_PSQL), patch(
        "subprocess.run", return_value=_fake_completed(0, stdout="\n"),
    ):
        _check("pg_roles empty output -> role exists False", _default_role_exists() is False)
    with patch("github_midwife_plugin.credential_seed._resolve_psql_binary", return_value=_RESOLVED_PSQL), patch(
        "subprocess.run", return_value=_fake_completed(0, stdout="1\n"),
    ) as mock_run:
        _default_role_exists()
    cmd = mock_run.call_args.args[0]
    _check(
        "the role-existence probe runs as the resolved getpass admin role",
        "-U" in cmd and getpass.getuser() in cmd,
        f"cmd={cmd!r} getuser={getpass.getuser()!r}",
    )
    _check("the probe queries pg_roles", "pg_roles" in " ".join(cmd), f"cmd={cmd!r}")
    _check("role-existence uses the resolved keg psql argv0", cmd[0] == _RESOLVED_PSQL, repr(cmd))


def _check_default_role_exists_execution_failure_raises() -> None:
    """A probe execution failure (nonzero exit, OSError, timeout) must RAISE,
    never be read as "role absent" -- that would route into a fresh seed whose
    ALTER then hits the same raw error the probe front-runs.
    """
    with patch("github_midwife_plugin.credential_seed._resolve_psql_binary", return_value=_RESOLVED_PSQL), patch(
        "subprocess.run", return_value=_fake_completed(2, stderr="psql: connection failed"),
    ):
        try:
            _default_role_exists()
        except CredentialSeedError as exc:
            _check("role-existence probe nonzero exit raises", "psql exit 2" in str(exc), str(exc))
        else:
            raise SmokeFailureError("role-exists-nonzero: did not raise")

    with patch("github_midwife_plugin.credential_seed._resolve_psql_binary", return_value=_RESOLVED_PSQL), patch(
        "subprocess.run", side_effect=OSError("psql binary missing"),
    ):
        try:
            _default_role_exists()
        except CredentialSeedError as exc:
            _check("role-existence probe OSError raises", "could not be executed" in str(exc), str(exc))
        else:
            raise SmokeFailureError("role-exists-oserror: did not raise")

    with patch("github_midwife_plugin.credential_seed._resolve_psql_binary", return_value=_RESOLVED_PSQL), patch(
        "subprocess.run", side_effect=subprocess.TimeoutExpired(cmd=[_RESOLVED_PSQL], timeout=15),
    ):
        try:
            _default_role_exists()
        except CredentialSeedError as exc:
            _check("role-existence probe timeout raises", "could not be executed" in str(exc), str(exc))
        else:
            raise SmokeFailureError("role-exists-timeout: did not raise")


def _check_resolved_role_absent_preserves_wizard_step_1_refusal() -> None:
    """A resolved client with no catalog row is role-absent, not resolution-failed.

    This is the third red mutation: after the keg-only resolution succeeds, an
    empty ``pg_roles`` response must retain the existing named wizard-step-1
    repair and must not write either credential or ALTER the role.
    """
    kc = FakeKeychain()
    with patch("github_midwife_plugin.credential_seed._resolve_psql_binary", return_value=_RESOLVED_PSQL), patch(
        "subprocess.run", return_value=_fake_completed(0, stdout="\n"),
    ):
        try:
            seed_db_password(keychain=kc, role_authenticates=lambda _pw: False)
        except CredentialSeedError as exc:
            _check(
                "resolved-role-absent-wizard-step-1 keeps the named repair",
                "does not exist" in str(exc) and "wizard step 1" in str(exc),
                str(exc),
            )
        else:
            raise SmokeFailureError("resolved-role-absent-wizard-step-1: did not raise")
    _check(
        "resolved-role-absent-wizard-step-1 writes no credentials",
        kc.snapshot() == {},
        repr(kc.snapshot()),
    )


def _check_alter_role_stderr_never_leaks_password() -> None:
    """Codex must-fix 1 (2026-07-09): psql failure stderr can echo the SQL text
    (which embeds the password literal) back verbatim -- the fixed code must
    never do that.
    """
    poisoned_stderr = f"ERROR: syntax error at or near \"{_SENTINEL_PW}\""
    with patch("github_midwife_plugin.credential_seed._resolve_psql_binary", return_value=_RESOLVED_PSQL), patch(
        "subprocess.run", return_value=_fake_completed(1, stderr=poisoned_stderr),
    ):
        try:
            _default_alter_role_password(_SENTINEL_PW)
        except CredentialSeedError as exc:
            _check(
                "ALTER ROLE failure exception never echoes psql stderr (which can carry the password)",
                _SENTINEL_PW not in str(exc),
                f"leaked: {exc!r}",
            )
        else:
            raise SmokeFailureError("alter-role-stderr-leak: did not raise")


# ── store-first ordering + self-healing rerun ──────────────────────────


def _check_store_failure_leaves_role_untouched() -> None:
    """Codex must-fix 2a: a store_credential failure must never reach ALTER
    ROLE (store-first order) -- proves the role is left fully untouched.
    """
    class FailingStoreKeychain(FakeKeychain):
        def store_credential(self, plugin_name: str, credential: str, value: bytes) -> None:
            raise RuntimeError("simulated Keychain store failure")

    kc = FailingStoreKeychain()
    alter_calls: list[str] = []
    with patch(
        "github_midwife_plugin.credential_seed.secrets.token_urlsafe",
        return_value=_SENTINEL_PW,
    ):
        try:
            seed_db_password(
                keychain=kc,
                alter_role_password=alter_calls.append,
                role_authenticates=lambda _pw: True,
                role_exists=lambda: True,
            )
        except RuntimeError:
            pass  # the simulated store failure propagating out is expected
        else:
            raise SmokeFailureError(
                "store-failure-leaves-role-untouched: seed_db_password did not propagate the store failure"
            )
    _check(
        "a store_credential failure never touches the live role (store-first order)",
        alter_calls == [],
        f"got {alter_calls!r} -- ALTER ROLE was called despite the store failing first",
    )


def _check_alter_failure_after_store_then_rerun_repairs() -> None:
    """Codex must-fix 2b: an ALTER failure AFTER a verified store leaves the
    Keychain holding a password the role does not have YET -- the same "stale
    stored pw" state a rerun's pre-check already repairs. Proves a rerun
    converges to a consistent, authenticating credential.
    """
    kc = FakeKeychain()

    def _failing_alter(_pw: str) -> None:
        raise CredentialSeedError("simulated ALTER ROLE failure")

    with patch(
        "github_midwife_plugin.credential_seed.secrets.token_urlsafe",
        return_value=_SENTINEL_PW,
    ):
        try:
            seed_db_password(
                keychain=kc,
                alter_role_password=_failing_alter,
                role_authenticates=lambda _pw: False,
                role_exists=lambda: True,
            )
        except CredentialSeedError:
            pass  # the simulated ALTER failure propagating out is expected
        else:
            raise SmokeFailureError("did not raise on the simulated ALTER failure")

    stored_after_first_attempt = kc.retrieve_credential(_STATE_PLUGIN, _CREDENTIAL)
    _check(
        "the failed-ALTER attempt still left the new pw stored (store-first, self-healing state)",
        stored_after_first_attempt == _SENTINEL_PW.encode("utf-8"),
        f"got {stored_after_first_attempt!r}",
    )

    second_pw = "SECOND_ATTEMPT_PW_repairs_67890"
    alter_calls: list[str] = []
    with patch(
        "github_midwife_plugin.credential_seed.secrets.token_urlsafe",
        return_value=second_pw,
    ):
        seed_db_password(
            keychain=kc,
            alter_role_password=alter_calls.append,
            # Simulates the role's REAL (unchanged-by-the-failed-attempt)
            # password: only the freshly-generated `second_pw` authenticates.
            role_authenticates=lambda pw: pw == second_pw,
        )

    _check(
        "the rerun calls ALTER ROLE again with a fresh password",
        alter_calls == [second_pw],
        f"got {alter_calls!r}",
    )
    final_stored = kc.retrieve_credential(_STATE_PLUGIN, _CREDENTIAL)
    _check(
        "the rerun converges the Keychain to a consistent, authenticating password",
        final_stored == second_pw.encode("utf-8"),
        f"got {final_stored!r}",
    )


# ── isolation self-proof classifier (§5.2 / R6.6b) ─────────────────────


def _check_isolation_permission_denied_confirms() -> None:
    """A CONNECT-privilege denial (`permission denied for database`) is the
    ONLY outcome that confirms isolation -- returns without raising."""
    def _denied(_db: str, _pw: str) -> subprocess.CompletedProcess[str]:
        return _fake_completed(
            2, stderr='connection to server failed: FATAL:  permission denied for database "example"',
        )
    _assert_role_cannot_reach_db("example", "real-pw", connect=_denied)
    _check("permission-denied-for-database -> isolation confirmed (no raise)", True)


def _check_isolation_auth_failure_is_inconclusive_not_isolated() -> None:
    """Dawn's explicit ask: a password-authentication failure must NOT read as
    isolation -- it is a DISTINCT, inconclusive outcome (the scram gate or the
    seed is broken), so a scram regression cannot masquerade as isolation.
    """
    def _auth_failed(_db: str, _pw: str) -> subprocess.CompletedProcess[str]:
        return _fake_completed(2, stderr='FATAL:  password authentication failed for user "otherrole"')
    try:
        _assert_role_cannot_reach_db("example", "real-pw", connect=_auth_failed)
    except CredentialSeedError as exc:
        _check(
            "auth-failure isolation probe is INCONCLUSIVE and distinct from a CONNECT denial",
            "inconclusive" in str(exc).lower(),
            str(exc),
        )
    else:
        raise SmokeFailureError("isolation-auth-failure: did not raise")


def _check_isolation_breach_when_connect_succeeds() -> None:
    def _succeeds(_db: str, _pw: str) -> subprocess.CompletedProcess[str]:
        return _fake_completed(0, stdout="1\n")
    try:
        _assert_role_cannot_reach_db("example", "real-pw", connect=_succeeds)
    except CredentialSeedError as exc:
        _check("a successful sibling connect raises ISOLATION BREACH", "breach" in str(exc).lower(), str(exc))
    else:
        raise SmokeFailureError("isolation-breach: did not raise")


def _check_default_sibling_connect_probe_passes_pw_via_env_not_argv() -> None:
    with patch("github_midwife_plugin.credential_seed._resolve_psql_binary", return_value=_RESOLVED_PSQL), patch(
        "subprocess.run", return_value=_fake_completed(2, stderr="permission denied for database"),
    ) as mock_run:
        _default_sibling_connect_probe("parent_db_xyz", _SENTINEL_PW)
    cmd = mock_run.call_args.args[0]
    _, kwargs = mock_run.call_args
    _check(
        "sibling-connect argv never contains the plaintext password",
        all(_SENTINEL_PW not in str(a) for a in cmd),
        f"leaked in argv: {cmd!r}",
    )
    _check(
        "sibling-connect password travels via PGPASSWORD env",
        kwargs.get("env", {}).get("PGPASSWORD") == _SENTINEL_PW,
        "PGPASSWORD not set correctly",
    )
    _check("sibling-connect targets the sibling database", "parent_db_xyz" in cmd, f"cmd={cmd!r}")
    _check("sibling-connect uses the resolved keg psql argv0", cmd[0] == _RESOLVED_PSQL, repr(cmd))


# ── CLI argv parsing ────────────────────────────────────────────────────


def _check_parse_seed_argv() -> None:
    _check("--seed alone parses to (True, None)", _parse_seed_argv([_SEED_FLAG]) == (True, None))
    _check(
        "--seed --isolation-sibling-db <db> parses to (True, db)",
        _parse_seed_argv([_SEED_FLAG, _ISOLATION_FLAG, "parentdb"]) == (True, "parentdb"),
    )
    _check("empty argv -> usage error (False, None)", _parse_seed_argv([]) == (False, None))
    _check("unknown flag -> usage error", _parse_seed_argv(["--nope"]) == (False, None))
    _check(
        "--isolation-sibling-db with no db -> usage error",
        _parse_seed_argv([_SEED_FLAG, _ISOLATION_FLAG]) == (False, None),
    )


# ── import-time discipline + admin-role identity ────────────────────────


def _check_bare_package_import_survives_missing_solet_name() -> None:
    """RIDER (Codex, non-blocking): `credential_seed` must stay OUT of
    `github_midwife_plugin/__init__.py` -- a bare `import github_midwife_plugin`
    must succeed with no SOLET_NAME set; only importing the
    `credential_seed` submodule specifically should fast-fail. Runs in a
    subprocess (this process already imported credential_seed at module load
    with SOLET_NAME set -- sys.modules caching would mask it in-process).
    """
    script = (
        "import os\n"
        "os.environ.pop('SOLET_NAME', None)\n"
        "import github_midwife_plugin\n"
        "print('bare-import-ok')\n"
        "try:\n"
        "    import github_midwife_plugin.credential_seed\n"
        "except RuntimeError:\n"
        "    print('credential-seed-import-failed-as-expected')\n"
        "else:\n"
        "    print('credential-seed-import-unexpectedly-succeeded')\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, timeout=15,
    )
    _check(
        "bare `import github_midwife_plugin` succeeds without SOLET_NAME",
        "bare-import-ok" in result.stdout,
        f"stdout={result.stdout!r} stderr={result.stderr!r}",
    )
    _check(
        "`import github_midwife_plugin.credential_seed` is the one thing that fast-fails without SOLET_NAME",
        "credential-seed-import-failed-as-expected" in result.stdout,
        f"stdout={result.stdout!r} stderr={result.stderr!r}",
    )


def _check_admin_role_dynamic_and_identical_across_layers() -> None:
    """RED-FIRST (operator-identity parameterization, 2026-07-11): the Layer-1
    admin role (credential_seed._ADMIN_ROLE, used by ALTER ROLE + the
    role-existence probe) must resolve to getpass.getuser() at import, NOT a
    hardcoded 'dw' -- AND must resolve IDENTICALLY to the Layer-0 admin role
    (bootstrap.py), or Layer 0 creates the role/db as user X while Layer 1
    alters as user Y -> fail. In a fresh subprocess (so this process's
    sys.modules cache can't mask it) getpass.getuser is patched to a sentinel
    BEFORE either module is imported. Machine-independent -- never asserts a
    literal username.
    """
    bootstrap_path = Path(__file__).resolve().parents[3] / "bootstrap.py"
    sentinel = "SENTINEL_ADMIN_ROLE_not_a_real_user"
    script = (
        "import os\n"
        "os.environ.setdefault('SOLET_NAME', 'example')\n"
        "import getpass\n"
        f"getpass.getuser = lambda: {sentinel!r}\n"
        "import importlib.util, sys\n"
        "import github_midwife_plugin.credential_seed as cs\n"
        f"spec = importlib.util.spec_from_file_location('_bs_under_test', {str(bootstrap_path)!r})\n"
        "bs = importlib.util.module_from_spec(spec)\n"
        "sys.modules['_bs_under_test'] = bs\n"
        "spec.loader.exec_module(bs)\n"
        "print('L1=' + cs._ADMIN_ROLE)\n"
        "print('L0=' + bs._ADMIN_ROLE)\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, timeout=30,
    )
    _check(
        "Layer-1 credential_seed._ADMIN_ROLE is dynamically sourced from getpass.getuser() (not hardcoded)",
        f"L1={sentinel}" in result.stdout,
        f"stdout={result.stdout!r} stderr={result.stderr!r}",
    )
    _check(
        "Layer-0 bootstrap._ADMIN_ROLE is dynamically sourced from getpass.getuser() (not hardcoded)",
        f"L0={sentinel}" in result.stdout,
        f"stdout={result.stdout!r} stderr={result.stderr!r}",
    )
    _check(
        "Layer-0 and Layer-1 resolve the IDENTICAL admin role (both getpass.getuser())",
        f"L1={sentinel}" in result.stdout and f"L0={sentinel}" in result.stdout,
        f"stdout={result.stdout!r}",
    )
    # House-rule regression guard (this process, real getpass): the imported
    # module's admin role equals getpass.getuser() and is non-empty.
    real_user = getpass.getuser()
    _check(
        "the resolved admin role equals getpass.getuser() and is non-empty",
        bool(real_user) and _cs_module._ADMIN_ROLE == real_user,  # noqa: SLF001
        f"got {_cs_module._ADMIN_ROLE!r} vs getpass.getuser()={real_user!r}",  # noqa: SLF001
    )


def main() -> int:
    try:
        _check_admin_role_dynamic_and_identical_across_layers()
        _check_fresh_seed_generates_stores_verifies()
        _check_role_absent_fails_loud()
        _check_idempotent_skip_when_both_keys_present_and_authenticate()
        _check_backfill_pgvector_key_when_postgres_valid_but_pgvector_absent()
        _check_rotate_both_when_stored_pw_is_stale()
        _check_inconsistent_keychain_state_raises()
        _check_roundtrip_mismatch_raises()
        _check_solet_name_required()
        _check_nothing_printed()
        _check_postgresql17_resolver_is_strict_and_keg_aware()
        _check_default_alter_role_password_passes_pw_via_stdin_not_argv()
        _check_default_alter_role_password_raises_on_failure()
        _check_default_role_authenticates_passes_pw_via_env_not_argv()
        _check_default_role_exists_parses_psql_output()
        _check_default_role_exists_execution_failure_raises()
        _check_resolved_role_absent_preserves_wizard_step_1_refusal()
        _check_alter_role_stderr_never_leaks_password()
        _check_store_failure_leaves_role_untouched()
        _check_alter_failure_after_store_then_rerun_repairs()
        _check_isolation_permission_denied_confirms()
        _check_isolation_auth_failure_is_inconclusive_not_isolated()
        _check_isolation_breach_when_connect_succeeds()
        _check_default_sibling_connect_probe_passes_pw_via_env_not_argv()
        _check_parse_seed_argv()
        _check_bare_package_import_survives_missing_solet_name()
    except SmokeFailureError as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        print(f"  ({len(_CHECKS_RUN)} checks attempted before failure)", file=sys.stderr)
        return 1

    print(f"credential_seed_smoke OK: {len(_CHECKS_RUN)} checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
