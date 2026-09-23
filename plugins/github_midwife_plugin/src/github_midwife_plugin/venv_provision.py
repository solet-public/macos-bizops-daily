"""Verb-mode venv/seed installation + newborn credential self-seed.

Own-copy of the shape `bootstrap.py`'s `ensure_venv_and_seed` uses, adapted
for the "birth a SECOND solet from an already-running one" verb-mode
case (design doc dual-use). `create_venv_and_install_seed` builds the
newborn's `.venv` and editable-installs the platform + seed plugins;
`birth_solet`'s §7 `provision_venv` variant (the `mint_and_birth_local`
local-birth chain) calls it EXPLICITLY before genesis, since a seed folder
from `assemble_seed` ships source-only.

This module deliberately does NOT clone anything: acquisition-mode
clone-of-pinned-upstream was RETIRED 2026-07-18. The Seed Factory replaces
it -- `assemble_seed` reads a committed git ref and never a caller-supplied
clone URL, so the former arbitrary-code-as-LaunchAgent injection surface (a
verb-argument clone URL landing in the newborn's autostart plist) is gone by
construction, not merely guarded.

Per-solet credential isolation (operator override, 2026-07-12):
there is NO cross-process credential copy any more. Each solet has
its OWN non-superuser role (name = SOLET_NAME) and its OWN password;
no credential ever crosses a solet namespace. `seed_newborn_credential`
runs the newborn's OWN self-seed as a subprocess in the newborn's OWN venv
(SOLET_NAME=<newborn>), so `SystemKeychain` binds the newborn's
namespace by construction and the freshly-generated password never leaves
that subprocess. `verify_newborn_db_scram_gated` (pre-seed) and the
subprocess's post-seed isolation self-proof are the two verifications this
module contributes to the verb-mode flow.
"""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

from .constants import BUILD_BACKEND_PACKAGES, is_valid_solet_name
from .credential_seed import _ISOLATION_FLAG, _SEED_FLAG, _resolve_psql_binary

_INSTALL_TIMEOUT_S = 300
_SEED_TIMEOUT_S = 30
# A deliberately-WRONG password for the scram-coverage negative auth probe: if a
# wrong password is ACCEPTED, the connection fell through to `trust` (not
# scram-gated). Not a secret -- it must never equal a real password (real ones
# are `secrets.token_urlsafe`), and it is never stored anywhere.
_WRONG_PW_PROBE = "scram_coverage_probe_wrong_pw_not_a_real_secret"  # noqa: S105

Runner = Callable[..., subprocess.CompletedProcess[str]]


class VerbModeProvisionError(RuntimeError):
    """Raised when venv/seed installation or cross-process credential
    provisioning cannot complete safely.
    """


def _require_valid_newborn_name(name: str, *, role: str = "newborn") -> None:
    """Fail closed unless `name` matches NAME_PATTERN (F3, 2026-07-19).

    `newborn_name` is interpolated into psql connection arguments and passed as
    the subprocess `SOLET_NAME`; `sibling_db` reaches a `psql -d` argv in the
    isolation self-proof. Validating at these verb entrypoints fail-closes a name
    carrying a SPACE -- a libpq conninfo keyword-injection vector (libpq takes the
    LAST value for a repeated key, so a space could rewrite `host`/add
    `password`), the exact F3 hole that could subvert the scram-gate self-check --
    or any other metacharacter, complementing the discrete-argv connection form
    that removes the single-conninfo-string sink structurally.
    """
    if not is_valid_solet_name(name):
        raise VerbModeProvisionError(
            f"{role} name {name!r} is not a valid solet name -- it must be a "
            "lowercase letter followed by 1-62 chars from [a-z0-9_-]. Names with "
            "spaces (a libpq conninfo keyword-injection vector), quotes, or "
            "semicolons are refused before any psql connection."
        )


def probe_target_absent_or_empty(target: Path) -> bool:
    """True iff `target` does not exist, or exists as an empty directory.

    Used by `birth_solet` to fail loud on an absent/empty target: an
    empty directory is NOT a birthable clone (acquisition-mode
    clone-of-pinned-upstream was retired 2026-07-18; assemble a seed first).
    """
    if not target.exists():
        return True
    return target.is_dir() and not any(target.iterdir())


def create_venv_and_install_seed(target: Path, *, run: Runner) -> Path:
    """`python3 -m venv <target>/.venv` + build-backend prep + fail-loud
    editable install of `ananta` + `macos_vault_plugin` +
    `github_midwife_plugin` -- own-copy of bootstrap.py's
    `ensure_venv_and_seed` SHAPE (that file is stdlib-only and cannot be
    imported from here; this is a deliberate, minimal duplication, not an
    oversight).

    macos_vault_plugin is installed BETWEEN ananta and the seed plugin
    because the seed plugin's pyproject pins it (honest pin -- genesis
    imports its keychain at module level); without the local editable
    install first, pip tries to resolve the pin from PyPI and fails
    (cold-agent acceptance finding F-5, 2026-07-12).

    Build-backend prep (finding F8, 2026-07-11): a stock Python 3.13 venv
    ships pip but NOT setuptools (`ensurepip` dropped it in 3.12), so the
    `pip install --no-build-isolation -e` calls below fail with
    `BackendUnavailable: Cannot import 'setuptools.build_meta'` on a fresh
    venv. This upgrades pip/setuptools/wheel FIRST, unconditionally (pip
    makes it idempotent). `--no-build-isolation` is kept deliberately:
    genesis already requires network (this module's own `git clone`), and
    one build-backend pre-seed per venv is cheaper than build isolation's
    per-package ephemeral build envs across the whole seed install, while
    preserving invocation parity with the sibling birthers' venv-setup
    shape. (Not an offline optimization -- setuptools/wheel are fetched
    from PyPI either way.)
    """
    venv_dir = target / ".venv"
    venv_cmd = [sys.executable, "-m", "venv", str(venv_dir)]
    result = run(venv_cmd, capture_output=True, text=True, timeout=_INSTALL_TIMEOUT_S)
    if result.returncode != 0:
        raise VerbModeProvisionError(f"venv creation failed at {venv_dir} (exit {result.returncode})")

    venv_python = venv_dir / "bin" / "python3"
    backend_cmd = [str(venv_python), "-m", "pip", "install", "--upgrade", *BUILD_BACKEND_PACKAGES]
    result = run(backend_cmd, capture_output=True, text=True, timeout=_INSTALL_TIMEOUT_S)
    if result.returncode != 0:
        raise VerbModeProvisionError(
            f"build-backend install (pip/setuptools/wheel) failed in {venv_dir} "
            f"(exit {result.returncode}): {(result.stderr or '').strip()[:500]}"
        )

    # born-clone-gates fix (2026-08-10, Dax Part 38): `ananta/setup.py`
    # declares the commit-gate toolchain (ruff/pyright/radon) in
    # extras_require["gate"], deliberately NOT absence-tolerant -- a bare
    # `pip install -e <dir>` pulls only install_requires, so the gate
    # toolchain could never arrive at birth by construction. `ananta`
    # installs with `[gate]` specifically; the other two package dirs stay
    # bare -- do NOT widen this to `.[all]` or sweep in any other plugin's
    # extras (code_vetting_plugin's typecheck/coverage extras are
    # deliberately absence-tolerant by a DIFFERENT, opposite contract; see
    # ananta/setup.py's own comment forbidding exactly this "harmonisation").
    for package_dir, extras in (
        (target / "solet_setup_contracts", ""),
        (target / "ananta", "[gate]"),
        (target / "plugins" / "macos_vault_plugin", ""),
        (target / "plugins" / "github_midwife_plugin", ""),
    ):
        install_cmd = [
            str(venv_python), "-m", "pip", "install", "--no-build-isolation", "-e",
            f"{package_dir}{extras}",
        ]
        result = run(install_cmd, capture_output=True, text=True, timeout=_INSTALL_TIMEOUT_S)
        if result.returncode != 0:
            raise VerbModeProvisionError(
                f"seed install failed for {package_dir} (exit {result.returncode}): "
                f"{(result.stderr or '').strip()[:500]}"
            )
    return venv_dir


def verify_newborn_db_scram_gated(newborn_name: str, *, run: Runner) -> None:
    """Assumes-and-verifies (Architect acceptance criterion; per-role isolation
    2026-07-12). PRE-seed check.

    The newborn's OWN database, its pgvector extension, its own non-superuser
    role (name = `SOLET_NAME`), and the localhost scram gate are created by
    WIZARD STEP 1 -- an agent-run pre-launch step the driving Claude performs
    with guidance -- NOT by genesis code, which ASSUMES them present. Of these,
    a MISSING database makes the verb/boot fail and a MISSING pgvector extension
    crash-loops at readiness (both self-announcing), but a MISSING scram line is
    INVISIBLE: everything works, silently passwordless-accessible forever. So the
    verb keeps exactly this ONE pre-seed check -- a NEGATIVE auth probe: if the
    newborn's OWN role (`newborn_name`) reaches its OWN database with a WRONG
    password, the connection fell through to `trust` and scram is not gating it
    -- refuse LOUD, naming the R3 default-scram lines wizard step 1 must add.
    """
    _require_valid_newborn_name(newborn_name)
    env = os.environ.copy()
    env["PGPASSWORD"] = _WRONG_PW_PROBE
    # Discrete argv (`-h`/`-d`/`-U`), NOT a single `host=... dbname=... user=...`
    # libpq conninfo string (F3, 2026-07-19): a name with a space could inject a
    # connection keyword (e.g. its own `password=`/`host=`) into a conninfo, which
    # would subvert THIS scram-gate self-check. As separate argv tokens a space is
    # inert -- it can only ever be part of the dbname/role value.
    result = run(
        [_resolve_psql_binary(), "-h", "127.0.0.1", "-d", newborn_name, "-U", newborn_name, "-tAc", "SELECT 1"],
        capture_output=True, text=True, timeout=_SEED_TIMEOUT_S, env=env,
    )
    if result.returncode == 0:
        raise VerbModeProvisionError(
            f"database {newborn_name!r} accepts role {newborn_name!r} with a WRONG "
            "password -- it is passwordless-accessible (localhost scram is not "
            "gating it). Wizard step 1 must insert the R3 default-scram lines "
            "immediately ABOVE the blanket trust block and reload "
            "(`SELECT pg_reload_conf()`) -- the all-databases form gates every "
            "per-solet role at once:\n"
            "  host    all     all          127.0.0.1/32            scram-sha-256\n"
            "  host    all     all          ::1/128                 scram-sha-256\n"
            "  local   all     all                                  scram-sha-256"
        )


def seed_newborn_credential(
    newborn_name: str,
    newborn_venv: Path,
    sibling_db: str,
    *,
    run: Runner,
) -> None:
    """Verb-mode newborn self-seed + post-seed isolation self-proof.

    Per-role isolation (operator override, 2026-07-12): the newborn seeds its
    OWN role via the same `credential_seed.seed_db_password` path the CLI uses
    -- there is NO parent-provisions-child credential copy any more (no
    credential ever crosses a solet namespace). This runs
    `credential_seed --seed --isolation-sibling-db <sibling_db>` in a subprocess
    bound to the NEWBORN's own venv with `SOLET_NAME=<newborn_name>`, so
    SystemKeychain resolves the newborn's namespace by construction and the
    freshly-generated password never leaves that subprocess. The subprocess:
      1. generates + stores (both keys) + `ALTER ROLE "<newborn>"` its OWN role.
      2. post-seed, proves that role CANNOT reach `sibling_db` (the parent's db)
         -- a real-password connect must be refused with a CONNECT-privilege
         denial (R4 revoke), not an auth failure.

    `sibling_db` is the PARENT solet's database (== the parent's own
    `SOLET_NAME`). The subprocess's stderr tail IS surfaced on failure:
    `credential_seed`'s `--seed` path is secret-free by construction (it never
    prints a password), and its FATAL diagnostic is the actionable error (e.g.
    "role does not exist -- wizard step 1", or an isolation breach).
    """
    _require_valid_newborn_name(newborn_name)
    _require_valid_newborn_name(sibling_db, role="sibling database")
    env = os.environ.copy()
    env["SOLET_NAME"] = newborn_name
    newborn_python = newborn_venv / "bin" / "python3"
    result = run(
        [str(newborn_python), "-m", "github_midwife_plugin.credential_seed",
         _SEED_FLAG, _ISOLATION_FLAG, sibling_db],
        capture_output=True, text=True, timeout=_SEED_TIMEOUT_S, env=env,
    )
    if result.returncode != 0:
        raise VerbModeProvisionError(
            f"newborn credential self-seed subprocess exited {result.returncode}: "
            f"{(result.stderr or '').strip()[:500]}"
        )


__all__ = [
    "VerbModeProvisionError",
    "create_venv_and_install_seed",
    "probe_target_absent_or_empty",
    "seed_newborn_credential",
    "verify_newborn_db_scram_gated",
]
