"""Finding F10 smoke — first-run vault passphrase file seed.

Pins the security-load-bearing properties of `vault_passphrase_seed`:
write-if-absent (NEVER regenerate — the passphrase is the sole key to whatever
the vault has already encrypted), mode 0600, and the hygiene bar that the
generated value never leaks to stdout/stderr or the return. Fully offline: no
vault, no Keychain, no network — just a tmp filesystem.

Run directly: ``.venv/bin/python3
plugins/github_midwife_plugin/tests/vault_passphrase_seed_smoke.py``.
"""

from __future__ import annotations

# ruff: noqa: E402
import io
import stat
import sys
import tempfile
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

_PLUGIN_ROOT = Path(__file__).resolve().parents[1]
_SRC = _PLUGIN_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from github_midwife_plugin.vault_passphrase_seed import (
    seed_vault_passphrase,
    vault_passphrase_path,
)

_CHECKS_RUN: list[str] = []
_SENTINEL_A = "VAULT_SEED_SENTINEL_A_do_not_leak_11111"
_SENTINEL_B = "VAULT_SEED_SENTINEL_B_do_not_leak_22222"
_SENTINEL_C = "VAULT_SEED_SENTINEL_C_do_not_leak_33333"


class SmokeFailureError(AssertionError):
    """Raised on any check failure; message is the failure detail."""


def _check(label: str, condition: bool, detail: str = "") -> None:
    _CHECKS_RUN.append(label)
    if not condition:
        raise SmokeFailureError(f"{label}: {detail}")


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def _check_path_shape(root: Path) -> None:
    target = root / "clone"
    _check(
        "vault_passphrase_path resolves to the seed_loader-expected nested file",
        vault_passphrase_path(target)
        == target / "profile" / "config" / "plugins" / "macos_vault_plugin" / "passphrase",
        str(vault_passphrase_path(target)),
    )


def _check_creates_when_absent(root: Path) -> None:
    target = root / "clone"
    with patch("github_midwife_plugin.vault_passphrase_seed.token_urlsafe", return_value=_SENTINEL_A):
        created = seed_vault_passphrase(target)
    path = vault_passphrase_path(target)
    _check("seed_vault_passphrase returns True when it creates the file", created is True, str(created))
    _check("the passphrase file was created", path.is_file(), str(path))
    _check("the passphrase file is mode 0600 (owner-only)", _mode(path) == 0o600, oct(_mode(path)))
    _check("the passphrase file holds the generated value", path.read_text() == _SENTINEL_A, "content mismatch")


def _check_idempotent_never_regenerates(root: Path) -> None:
    """The load-bearing safety property: a second call NEVER overwrites — even
    with a different generated value on offer, the original file survives
    byte-for-byte (overwriting would orphan whatever the vault already
    encrypted under the first passphrase).
    """
    target = root / "clone"
    with patch("github_midwife_plugin.vault_passphrase_seed.token_urlsafe", return_value=_SENTINEL_A):
        first = seed_vault_passphrase(target)
    with patch("github_midwife_plugin.vault_passphrase_seed.token_urlsafe", return_value=_SENTINEL_B):
        second = seed_vault_passphrase(target)
    path = vault_passphrase_path(target)
    _check("first call created the file (returns True)", first is True, str(first))
    _check("second call is a no-op (returns False)", second is False, str(second))
    _check(
        "the file still holds the ORIGINAL passphrase (never regenerated / overwritten)",
        path.read_text() == _SENTINEL_A,
        "second call clobbered the passphrase — would orphan already-encrypted vault secrets",
    )
    _check("mode is still 0600 after the no-op", _mode(path) == 0o600, oct(_mode(path)))


def _check_never_clobbers_operator_set_file(root: Path) -> None:
    """An operator-chosen passphrase (written before genesis via the README
    getpass ladder) is preserved untouched — seed is write-if-absent.
    """
    target = root / "clone"
    path = vault_passphrase_path(target)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("OPERATOR_CHOSEN_PASSPHRASE")
    with patch("github_midwife_plugin.vault_passphrase_seed.token_urlsafe", return_value=_SENTINEL_A):
        created = seed_vault_passphrase(target)
    _check("seed no-ops on a pre-existing operator file (returns False)", created is False, str(created))
    _check(
        "the operator's own passphrase is preserved untouched",
        path.read_text() == "OPERATOR_CHOSEN_PASSPHRASE",
        "seed clobbered an operator-set passphrase",
    )


def _check_never_leaks(root: Path) -> None:
    target = root / "clone"
    captured_out, captured_err = io.StringIO(), io.StringIO()
    with patch("github_midwife_plugin.vault_passphrase_seed.token_urlsafe", return_value=_SENTINEL_C), \
         redirect_stdout(captured_out), redirect_stderr(captured_err):
        created = seed_vault_passphrase(target)
    combined = captured_out.getvalue() + captured_err.getvalue()
    _check("the generated passphrase never appears on stdout/stderr", _SENTINEL_C not in combined, f"leaked: {combined!r}")
    _check("the return value carries no secret (a bare bool)", isinstance(created, bool) and _SENTINEL_C not in str(created), str(created))


def main() -> int:
    try:
        with tempfile.TemporaryDirectory() as tmp:
            _check_path_shape(Path(tmp))
        with tempfile.TemporaryDirectory() as tmp:
            _check_creates_when_absent(Path(tmp))
        with tempfile.TemporaryDirectory() as tmp:
            _check_idempotent_never_regenerates(Path(tmp))
        with tempfile.TemporaryDirectory() as tmp:
            _check_never_clobbers_operator_set_file(Path(tmp))
        with tempfile.TemporaryDirectory() as tmp:
            _check_never_leaks(Path(tmp))
    except SmokeFailureError as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        print(f"  ({len(_CHECKS_RUN)} checks attempted before failure)", file=sys.stderr)
        return 1

    print(f"vault_passphrase_seed_smoke OK: {len(_CHECKS_RUN)} checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
