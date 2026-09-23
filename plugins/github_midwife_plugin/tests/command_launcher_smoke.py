"""No-MCP-first launcher smoke — the per-solet PATH command birth step.

Drives `install_command_launcher_at_birth()` against tmpfs clone + bin dirs
(no real `~/.local/bin`, no venv). Asserts the full contract:

* happy path installs `<bin_dir>/<name>` as a symlink to the clone's own
  `solet` console script,
* an existing same-name Codex MCP table is a fail-loud refusal,
* a stale symlink (pointing elsewhere) is repointed,
* a NON-symlink file at the launcher path is a fail-loud refusal (never
  clobber an operator file),
* a missing console script is a fail-loud refusal (venv must be provisioned
  first),
* an invalid solet name is refused (defense in depth: `bin_dir / name`
  must never escape bin_dir).

Run directly: ``.venv/bin/python3
plugins/github_midwife_plugin/tests/command_launcher_smoke.py``.
"""

from __future__ import annotations

import sys
import tempfile
import tomllib
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import github_midwife_plugin.command_launcher as command_launcher  # noqa: E402
from github_midwife_plugin.command_launcher import (  # noqa: E402
    CONSOLE_SCRIPT_NAME,
    CommandLauncherError,
    _mcp_block,
    install_command_launcher_at_birth,
)

_CHECKS_RUN: list[str] = []


class SmokeFailureError(AssertionError):
    """Raised on any check failure; message is the failure detail."""


def _check(label: str, condition: bool, detail: str = "") -> None:
    _CHECKS_RUN.append(label)
    if not condition:
        raise SmokeFailureError(f"{label}: {detail}")


def _make_clone(root: Path, name: str = "clone") -> Path:
    clone = root / name
    (clone / ".venv" / "bin").mkdir(parents=True)
    (clone / ".venv" / "bin" / CONSOLE_SCRIPT_NAME).write_text("#!/bin/sh\n")
    return clone


def _check_fresh_install(root: Path) -> tuple[Path, Path, Path, Path]:
    clone = _make_clone(root)
    bin_dir = root / "bin"
    config_path = root / "codex" / "config.toml"
    target = clone / ".venv" / "bin" / CONSOLE_SCRIPT_NAME

    installed = install_command_launcher_at_birth(
        name="testhum", clone_root=clone, bin_dir=bin_dir, codex_config_path=config_path,
    )
    launcher = bin_dir / "testhum"
    _check(
        "fresh install creates the symlink and reports installed",
        installed.status == "installed"
        and launcher.is_symlink()
        and launcher.readlink() == target,
        f"{installed} link={launcher}",
    )
    parsed = tomllib.loads(config_path.read_text(encoding="utf-8"))
    server = parsed.get("mcp_servers", {}).get("testhum", {})
    _check(
        server.get("command") == str(clone / ".venv" / "bin" / "python3")
        and server.get("args") == ["-m", "agent_messaging_plugin.mcp_bridge"]
        and server.get("env_vars") == ["CODEX_THREAD_ID"]
        and server.get("env", {}).get("SOLET_NAME") == "testhum"
        and server.get("env", {}).get("AGENT_IDENTITY") == "codex"
        and server.get("env", {}).get("AGENT_SESSION_LABEL") == "Codex-Ambient",
        "fresh install appends the newborn's own Codex MCP bridge configuration",
        repr(server),
    )

    return clone, bin_dir, config_path, target


def _check_self_generated_config_is_idempotent(
    clone: Path,
    bin_dir: Path,
    config_path: Path,
    target: Path,
) -> None:
    generated = config_path.read_text(encoding="utf-8")
    duplicate = install_command_launcher_at_birth(
        name="testhum", clone_root=clone, bin_dir=bin_dir, codex_config_path=config_path,
    )
    _check(
        "an exact self-generated Codex MCP table is idempotent rather than refused",
        duplicate.status == "already_installed"
        and duplicate.mcp_status == "already_installed"
        and "already installed" in duplicate.mcp_reason
        and (bin_dir / "testhum").readlink() == target
        and config_path.read_text(encoding="utf-8") == generated,
        repr(duplicate),
    )


def _check_equivalent_mcp_table_format_is_idempotent(root: Path, clone: Path) -> None:
    config_path = root / "equivalent-config.toml"
    generated = _mcp_block("testhum", clone)
    equivalent = generated.replace(
        'command = "' + str(clone / ".venv" / "bin" / "python3") + '"\n'
        'args = ["-m", "agent_messaging_plugin.mcp_bridge"]\n'
        'env_vars = ["CODEX_THREAD_ID"]\n',
        'env_vars = ["CODEX_THREAD_ID"]\n'
        'args = ["-m", "agent_messaging_plugin.mcp_bridge"]\n'
        'command = "' + str(clone / ".venv" / "bin" / "python3") + '"\n',
    )
    _check("the equivalent MCP fixture differs textually", equivalent != generated)
    config_path.write_text(equivalent, encoding="utf-8")
    result = install_command_launcher_at_birth(
        name="testhum", clone_root=clone, bin_dir=root / "equivalent-bin", codex_config_path=config_path,
    )
    _check(
        "a semantically equivalent differently formatted MCP table is already installed",
        result.mcp_status == "already_installed" and config_path.read_text(encoding="utf-8") == equivalent,
        repr(result),
    )


def _check_quote_path_is_valid_toml(root: Path) -> None:
    clone = _make_clone(root, name='quote"clone')
    config_path = root / "quote-config.toml"
    result = install_command_launcher_at_birth(
        name="quotehum", clone_root=clone, bin_dir=root / "quote-bin", codex_config_path=config_path,
    )
    parsed = tomllib.loads(config_path.read_text(encoding="utf-8"))
    _check(
        "a quote-containing clone path is escaped into valid Codex TOML",
        result.mcp_status == "installed"
        and parsed["mcp_servers"]["quotehum"]["command"]
        == str(clone / ".venv" / "bin" / "python3"),
        repr(result),
    )


def _check_atomic_config_failures_preserve_existing_bytes(root: Path) -> None:
    clone = _make_clone(root, name="atomic_clone")
    original = '[existing]\nvalue = "preserve-me"\n'

    def assert_failure_preserves(
        label: str,
        config_path: Path,
        patch_target: object,
        patch_attribute: str,
        replacement: object,
    ) -> None:
        config_path.write_text(original, encoding="utf-8")
        with patch.object(patch_target, patch_attribute, replacement):
            try:
                install_command_launcher_at_birth(
                    name="atomichum", clone_root=clone, bin_dir=root / f"{label}-bin",
                    codex_config_path=config_path,
                )
                raise SmokeFailureError(f"{label} did not raise")
            except CommandLauncherError:
                _check(
                    f"{label} leaves the existing config byte-identical",
                    config_path.read_text(encoding="utf-8") == original,
                    repr(config_path.read_text(encoding="utf-8")),
                )

    partial_config = root / "partial-config.toml"
    original_os_write = command_launcher.os.write
    first_write = True

    def write_partial_then_fail(descriptor: int, content: bytes) -> int:
        nonlocal first_write
        if first_write:
            first_write = False
            return original_os_write(descriptor, content[:17])
        raise OSError("injected ENOSPC after partial temporary write")

    assert_failure_preserves(
        "partial write failure", partial_config, command_launcher.os, "write", write_partial_then_fail
    )

    fsync_config = root / "fsync-config.toml"

    def fsync_fail(_descriptor: int) -> None:
        raise OSError("injected fsync failure")

    assert_failure_preserves("fsync failure", fsync_config, command_launcher.os, "fsync", fsync_fail)

    parse_config = root / "parse-config.toml"
    original_read_text = Path.read_text

    def read_corrupt_temporary(path: Path, *args: object, **kwargs: object) -> str:
        if path.parent == parse_config.parent and path.name.startswith(f".{parse_config.name}."):
            return "not valid = [toml"
        return original_read_text(path, *args, **kwargs)

    assert_failure_preserves("temporary parse failure", parse_config, Path, "read_text", read_corrupt_temporary)

    replace_config = root / "replace-config.toml"

    def replace_fail(_source: object, _target: object) -> None:
        raise OSError("injected replace failure")

    assert_failure_preserves("replace failure", replace_config, command_launcher.os, "replace", replace_fail)


def _check_atomic_config_success_preserves_mode_and_non_bmp(root: Path) -> None:
    clone = _make_clone(root, name="rocket-😀")
    config_path = root / "mode-config.toml"
    config_path.write_text('[existing]\nvalue = "preserve-me"\n', encoding="utf-8")
    config_path.chmod(0o640)
    result = install_command_launcher_at_birth(
        name="atomichum", clone_root=clone, bin_dir=root / "atomic-success-bin", codex_config_path=config_path,
    )
    parsed = tomllib.loads(config_path.read_text(encoding="utf-8"))
    _check(
        "atomic replacement preserves unrelated config, target mode, and parses a non-BMP path",
        result.mcp_status == "installed"
        and parsed["existing"]["value"] == "preserve-me"
        and parsed["mcp_servers"]["atomichum"]["command"] == str(clone / ".venv" / "bin" / "python3")
        and config_path.stat().st_mode & 0o777 == 0o640,
        repr(result),
    )


def _check_symlinked_config_is_refused(root: Path) -> None:
    clone = _make_clone(root, name="symlink_clone")
    target = root / "operator-config.toml"
    target.write_text('[existing]\nvalue = "preserve-me"\n', encoding="utf-8")
    config_path = root / "symlink-config.toml"
    config_path.symlink_to(target)
    try:
        install_command_launcher_at_birth(
            name="symlinkhum", clone_root=clone, bin_dir=root / "symlink-bin", codex_config_path=config_path,
        )
        raise SmokeFailureError("symlinked config did not raise")
    except CommandLauncherError as exc:
        _check(
            "a symlinked config is refused without replacing the operator target",
            "symlinked" in str(exc)
            and config_path.is_symlink()
            and target.read_text(encoding="utf-8") == '[existing]\nvalue = "preserve-me"\n',
            str(exc),
        )


def _check_stale_symlink_repoint(root: Path, clone: Path, bin_dir: Path, target: Path) -> None:
    other_clone = _make_clone(root, name="other_clone")
    launcher = bin_dir / "testhum"
    launcher.unlink()
    launcher.symlink_to(other_clone / ".venv" / "bin" / CONSOLE_SCRIPT_NAME)
    repointed = install_command_launcher_at_birth(
        name="testhum", clone_root=clone, bin_dir=bin_dir,
        codex_config_path=root / "codex-repoint" / "config.toml",
    )
    _check(
        "a stale symlink (another clone's script) is repointed to this clone",
        repointed.status == "repointed" and launcher.readlink() == target,
        f"{repointed} -> {launcher.readlink()}",
    )


def _check_failure_modes(root: Path) -> None:
    clone = _make_clone(root, name="failure_clone")
    bin_dir = root / "failure_bin"
    bin_dir.mkdir()

    (bin_dir / "occupied").write_text("an operator's real file\n")
    try:
        install_command_launcher_at_birth(
            name="occupied", clone_root=clone, bin_dir=bin_dir,
            codex_config_path=root / "occupied-config.toml",
        )
        raise SmokeFailureError("non-symlink collision did not raise")
    except CommandLauncherError as exc:
        _check(
            "a NON-symlink at the launcher path is a fail-loud refusal",
            "refusing to clobber" in str(exc),
            str(exc),
        )
    _check(
        "the operator's file survives the refusal untouched",
        (bin_dir / "occupied").read_text() == "an operator's real file\n",
    )

    bare_clone = root / "bare_clone"
    (bare_clone / ".venv" / "bin").mkdir(parents=True)
    try:
        install_command_launcher_at_birth(
            name="testhum", clone_root=bare_clone, bin_dir=bin_dir,
            codex_config_path=root / "missing-config.toml",
        )
        raise SmokeFailureError("missing console script did not raise")
    except CommandLauncherError as exc:
        _check(
            "a missing console script is a fail-loud refusal",
            "console script missing" in str(exc),
            str(exc),
        )

    try:
        install_command_launcher_at_birth(
            name="../escape", clone_root=clone, bin_dir=bin_dir,
            codex_config_path=root / "escape-config.toml",
        )
        raise SmokeFailureError("invalid name did not raise")
    except CommandLauncherError as exc:
        _check(
            "an invalid solet name is refused before touching bin_dir",
            "invalid solet name" in str(exc),
            str(exc),
        )
    _check(
        "the refused name created nothing outside bin_dir",
        not (root / "escape").exists(),
    )

    different_config = root / "different-config.toml"
    different_config.write_text(
        '[mcp_servers.testhum]\ncommand = "/operator/python"\n', encoding="utf-8",
    )
    try:
        install_command_launcher_at_birth(
            name="testhum", clone_root=clone, bin_dir=root / "different-bin",
            codex_config_path=different_config,
        )
        raise SmokeFailureError("different MCP table did not raise")
    except CommandLauncherError as exc:
        _check(
            "a different same-name Codex MCP table is refused with differing keys only",
            "refusing to overwrite" in str(exc) and "keys: " in str(exc) and "command" in str(exc),
            str(exc),
        )


def main() -> int:
    try:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            clone, bin_dir, config_path, target = _check_fresh_install(root)
            _check_self_generated_config_is_idempotent(clone, bin_dir, config_path, target)
            _check_equivalent_mcp_table_format_is_idempotent(root, clone)
            _check_quote_path_is_valid_toml(root)
            _check_atomic_config_failures_preserve_existing_bytes(root)
            _check_atomic_config_success_preserves_mode_and_non_bmp(root)
            _check_symlinked_config_is_refused(root)
            _check_stale_symlink_repoint(root, clone, bin_dir, target)
            _check_failure_modes(root)
    except SmokeFailureError as exc:
        print(f"command_launcher_smoke FAILED: {exc}", file=sys.stderr)
        print(f"  ({len(_CHECKS_RUN)} checks attempted before failure)", file=sys.stderr)
        return 1
    print(f"command_launcher_smoke OK: {len(_CHECKS_RUN)} checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
