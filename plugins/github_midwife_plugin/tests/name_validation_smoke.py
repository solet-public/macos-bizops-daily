"""Name-validation regression smoke — the shared ``is_valid_solet_name``
gate and the FOUR name-derivation boundaries it now guards, closing the
unvalidated-name -> SQL / libpq-conninfo injection class (Codex BLOCKER +
Reviewer-A F1/F2/F3, 2026-07-19).

The seed already shipped ``NAME_PATTERN`` and applied it in ``steps.validate_name``,
but the Layer-0 (``bootstrap.py``) and Layer-1 (``credential_seed``,
``venv_provision``) name-derivation points interpolated the RAW ``SOLET_NAME``
/ newborn name into admin psql SQL, ``ALTER ROLE`` identifiers, and a libpq
conninfo string BEFORE validating. This smoke pins that every boundary now
fail-closes a name carrying a quote / semicolon / space / leading hyphen /
embedded newline.

Requires SOLET_NAME set even to import ``credential_seed`` /
``venv_provision`` (they resolve the role name == SOLET_NAME at import, and
``credential_seed`` imports ``macos_vault_plugin``). Everything is validated
offline against pure predicates and a fresh in-process module load -- no live
Postgres, no real Keychain.

Run directly: ``SOLET_NAME=<name> .venv/bin/python3
plugins/github_midwife_plugin/tests/name_validation_smoke.py``.
"""

from __future__ import annotations

# ruff: noqa: E402
import importlib.util
import os
import sys
from collections.abc import Callable
from pathlib import Path
from types import ModuleType
from unittest.mock import patch

_PLUGIN_ROOT = Path(__file__).resolve().parents[1]
_SRC = _PLUGIN_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from github_midwife_plugin import steps, venv_provision  # noqa: E402
from github_midwife_plugin.constants import NAME_PATTERN, is_valid_solet_name  # noqa: E402
from github_midwife_plugin.credential_seed import (  # noqa: E402
    CredentialSeedError,
)
from github_midwife_plugin.credential_seed import (
    _require_solet_name as _cs_require_name,
)

_REPO_ROOT = Path(__file__).resolve().parents[3]
_BOOTSTRAP_PATH = _REPO_ROOT / "bootstrap.py"

_CHECKS_RUN: list[str] = []

# Names the pattern ACCEPTS: a lowercase-letter start, then 1-62 chars from
# [a-z0-9_-] (total length 2-63).
_VALID: tuple[str, ...] = (
    "ab", "example", "newborn", "fern-fresh-forge", "a-b_c9", "x1",
    "a" + "b" * 62,  # 63 chars — the maximum
)
# Names the pattern REJECTS, one per injection / edge class.
_INVALID: tuple[str, ...] = (
    "",             # empty
    "a",            # too short (min 2)
    "Example",      # uppercase start
    "1ab",          # digit start
    "-ab",          # leading hyphen (a psql flag-injection shape)
    "_ab",          # underscore start (must start with a letter)
    "ab cd",        # space (libpq conninfo keyword-injection vector)
    'ab"cd',        # double quote (breaks a "<identifier>")
    "ab'cd",        # single quote (breaks a '<literal>')
    "ab;cd",        # semicolon (SQL statement break)
    "ab\ncd",       # embedded newline (the $-before-newline hole fullmatch closes)
    "a" + "b" * 63,  # 64 chars — too long
)

# The injection-shaped names each Layer-1 boundary must reject.
_INJECTION_NAMES: tuple[str, ...] = ('ev"il', "ev;il", "ev il", "-evil")


class SmokeFailureError(AssertionError):
    """Raised on any check failure; message is the failure detail."""


def _check(label: str, condition: bool, detail: str = "") -> None:
    _CHECKS_RUN.append(label)
    if not condition:
        raise SmokeFailureError(f"{label}: {detail}")


def _expect_raises(label: str, exc: type[Exception], thunk: Callable[[], object]) -> None:
    _CHECKS_RUN.append(label)
    try:
        thunk()
    except exc:
        return
    raise SmokeFailureError(f"{label}: expected {exc.__name__}, no matching exception raised")


# ── the shared validator (constants.is_valid_solet_name) ───────────


def _check_shared_validator() -> None:
    for name in _VALID:
        _check(f"is_valid accepts {name!r}", is_valid_solet_name(name), "should be valid")
    for name in _INVALID:
        _check(f"is_valid rejects {name!r}", not is_valid_solet_name(name), "should be invalid")


def _check_fullmatch_closes_the_newline_hole() -> None:
    """The exact bug the ``fullmatch`` upgrade closes: ``$`` matches just BEFORE a
    TRAILING newline, so ``NAME_PATTERN.match`` ACCEPTS ``"goodname\\n"`` while
    ``is_valid_solet_name`` (fullmatch) rejects it. Documents the pre-fix
    hole so a regression back to ``.match`` at any boundary goes red here.
    """
    _check(
        "NAME_PATTERN.match is fooled by a TRAILING newline (documents the pre-fix hole)",
        NAME_PATTERN.match("goodname\n") is not None,
        "match unexpectedly rejected the trailing-newline name — the documented hole changed",
    )
    _check(
        "is_valid_solet_name (fullmatch) rejects the trailing-newline name",
        not is_valid_solet_name("goodname\n"),
        "fullmatch let a trailing newline through",
    )


# ── boundary 1: steps.validate_name (Layer-1 genesis) ───────────────────


def _make_ctx(name: str) -> steps.GenesisContext:
    return steps.GenesisContext(
        name=name, profile_name="macos-free-solet",
        target=Path("/nonexistent"), kb_root=Path("/nonexistent"),
    )


def _check_steps_validate_name_boundary() -> None:
    ok = steps._run_validate_name(_make_ctx("goodname"))  # noqa: SLF001
    _check("steps.validate_name passes a valid name", ok["status"] == "completed", f"got {ok!r}")
    for bad in _INJECTION_NAMES:
        record = steps._run_validate_name(_make_ctx(bad))  # noqa: SLF001
        _check(f"steps.validate_name fails {bad!r}", record["status"] == "failed", f"got {record!r}")


# ── boundary 2: credential_seed._require_solet_name (F1/F2) ─────────


def _check_credential_seed_boundary() -> None:
    with patch.dict(os.environ, {"SOLET_NAME": "goodname"}):
        _check(
            "credential_seed._require_solet_name returns a valid name",
            _cs_require_name() == "goodname",
            "valid name not returned",
        )
    for bad in _INJECTION_NAMES:
        with patch.dict(os.environ, {"SOLET_NAME": bad}):
            _expect_raises(
                f"credential_seed._require_solet_name rejects {bad!r}",
                CredentialSeedError, _cs_require_name,
            )


# ── boundary 3: venv_provision._require_valid_newborn_name (F3) ──────────


def _check_venv_provision_boundary() -> None:
    venv_provision._require_valid_newborn_name("goodname")  # noqa: SLF001  # no raise
    _check("venv_provision accepts a valid newborn name", True)
    for bad in (*_INJECTION_NAMES, "ev il host=other password=p"):
        _expect_raises(
            f"venv_provision._require_valid_newborn_name rejects {bad!r}",
            venv_provision.VerbModeProvisionError,
            lambda b=bad: venv_provision._require_valid_newborn_name(b),  # noqa: SLF001
        )


# ── boundary 4: bootstrap._require_solet_name (Layer-0, at import) ──


_fresh_counter = [0]


def _load_bootstrap_fresh() -> ModuleType:
    """Load a FRESH copy of bootstrap.py (unique module name) so its import-time
    ``_DATABASE = _require_solet_name()`` resolves under the CURRENT patched
    environment. An invalid SOLET_NAME raises during exec (before any db call).
    """
    _fresh_counter[0] += 1
    module_name = f"_bootstrap_nv_{_fresh_counter[0]}"
    spec = importlib.util.spec_from_file_location(module_name, _BOOTSTRAP_PATH)
    if spec is None or spec.loader is None:
        raise SmokeFailureError(f"could not build an import spec for {_BOOTSTRAP_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module  # dataclasses needs it registered before exec
    spec.loader.exec_module(module)
    return module


def _check_bootstrap_boundary() -> None:
    # bootstrap resolves _DATABASE == _require_solet_name() at IMPORT, so an
    # invalid SOLET_NAME fails the module load itself — BEFORE any admin psql
    # / REVOKE / createuser call runs.
    for bad in ('ev"il', "ev;il", "-evil"):
        with patch.dict(os.environ, {"SOLET_NAME": bad}):
            _expect_raises(f"bootstrap load fails on {bad!r} before any db call", RuntimeError, _load_bootstrap_fresh)
    with patch.dict(os.environ, {"SOLET_NAME": "goodname"}):
        mod = _load_bootstrap_fresh()
    _check(
        "bootstrap _DATABASE consumes a validated name",
        mod._DATABASE == "goodname",  # noqa: SLF001
        f"got {mod._DATABASE!r}",  # noqa: SLF001
    )


def main() -> int:
    try:
        _check_shared_validator()
        _check_fullmatch_closes_the_newline_hole()
        _check_steps_validate_name_boundary()
        _check_credential_seed_boundary()
        _check_venv_provision_boundary()
        _check_bootstrap_boundary()
    except SmokeFailureError as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        print(f"  ({len(_CHECKS_RUN)} checks attempted before failure)", file=sys.stderr)
        return 1

    print(f"name_validation_smoke OK: {len(_CHECKS_RUN)} checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
