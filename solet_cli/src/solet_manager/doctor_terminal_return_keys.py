"""Report whether the managed terminal newline configuration is present.

The tmux spawn path enforces these server options for every managed worker.
This advisory checks the persistent companion artifacts the setup wizard
installs, so an operator can repair a host before opening an interactive seat.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import cast

from .doctor_advisory_result import advisory_unknown, advisory_verified, advisory_warn
from .models import InstanceRecord, JsonValue

_TMUX_LINES = (
    "set -s extended-keys on",
    'set -as terminal-features "xterm*:extkeys"',
)
_PROFILE_NAME = "Solet Claude Code Return keys"
_DYNAMIC_PROFILE = Path(
    "Library/Application Support/iTerm2/DynamicProfiles/solet-claude-return-keys.json"
)


def collect_terminal_return_key_advisories(
    record: InstanceRecord,
    *,
    home: Path | None = None,
) -> list[JsonValue]:
    """Return non-blocking checks for tmux modifier transport and iTerm2 input."""

    del record
    resolved_home = Path.home() if home is None else home
    return [
        _tmux_advisory(resolved_home / ".tmux.conf"),
        _iterm_advisory(resolved_home / _DYNAMIC_PROFILE),
    ]


def _tmux_advisory(path: Path) -> dict[str, JsonValue]:
    check_id = "doctor::tmux_return_key_modifiers_v1"
    expected: dict[str, JsonValue] = {
        "required_lines": cast(list[JsonValue], list(_TMUX_LINES))
    }
    observed: dict[str, JsonValue] = {"path_exists": path.exists(), "required_lines": []}
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return advisory_warn(
            check_id,
            "The managed tmux Return-key settings are absent.",
            expected,
            observed,
            str(path),
            "tmux_return_key_config_absent",
            "Run setup to add the managed tmux block, then start a new tmux server.",
        )
    except OSError as exc:
        return advisory_unknown(
            check_id,
            "The managed tmux Return-key settings could not be read.",
            expected,
            observed,
            str(path),
            "tmux_return_key_config_unreadable",
            f"Could not read {path.name}: {type(exc).__name__}.",
        )
    present = [line for line in _TMUX_LINES if line in text]
    observed["required_lines"] = cast(list[JsonValue], present)
    if len(present) == len(_TMUX_LINES):
        return advisory_verified(
            check_id,
            "tmux preserves modified Return keys for managed Claude Code seats.",
            expected,
            observed,
            str(path),
        )
    return advisory_warn(
        check_id,
        "The managed tmux Return-key block is incomplete.",
        expected,
        observed,
        str(path),
        "tmux_return_key_config_incomplete",
        "Run setup to restore both tmux lines, then start a new tmux server.",
    )


def _iterm_advisory(path: Path) -> dict[str, JsonValue]:
    check_id = "doctor::iterm_option_return_profile_v1"
    expected: dict[str, JsonValue] = {
        "profile_name": _PROFILE_NAME,
        "option_key_sends": 2,
    }
    observed: dict[str, JsonValue] = {"path_exists": path.exists(), "option_key_sends": None}
    raw, read_error = _read_iterm_profile(path)
    if read_error == "absent":
        return advisory_warn(
            check_id,
            "The managed iTerm2 Option+Return profile is absent.",
            expected,
            observed,
            str(path),
            "iterm_option_return_profile_absent",
            "Run setup, restart iTerm2, and select the Solet Claude Code Return keys profile.",
        )
    if read_error is not None:
        return advisory_unknown(
            check_id,
            "The managed iTerm2 Option+Return profile could not be read.",
            expected,
            observed,
            str(path),
            "iterm_option_return_profile_unreadable",
            f"Could not read the dynamic profile: {read_error}.",
        )
    profile = _managed_profile(raw)
    if profile is None:
        return advisory_warn(
            check_id,
            "The managed iTerm2 Option+Return profile is missing its required profile entry.",
            expected,
            observed,
            str(path),
            "iterm_option_return_profile_missing",
            "Run setup again, restart iTerm2, and select the restored profile.",
        )
    return _iterm_profile_value_advisory(path, expected, observed, profile)


def _read_iterm_profile(path: Path) -> tuple[object | None, str | None]:
    try:
        return json.loads(path.read_text(encoding="utf-8")), None
    except FileNotFoundError:
        return None, "absent"
    except (OSError, json.JSONDecodeError) as exc:
        return None, type(exc).__name__


def _managed_profile(raw: object | None) -> dict[object, object] | None:
    if not isinstance(raw, dict):
        return None
    profiles = raw.get("Profiles")
    if not isinstance(profiles, list):
        return None
    for item in profiles:
        if isinstance(item, dict) and item.get("Name") == _PROFILE_NAME:
            return item
    return None


def _iterm_profile_value_advisory(
    path: Path,
    expected: dict[str, JsonValue],
    observed: dict[str, JsonValue],
    profile: dict[object, object],
) -> dict[str, JsonValue]:
    check_id = "doctor::iterm_option_return_profile_v1"
    option_key_sends = profile.get("Option Key Sends")
    observed["option_key_sends"] = cast(JsonValue, option_key_sends) if isinstance(
        option_key_sends, (str, int, float, bool)
    ) or option_key_sends is None else None
    if option_key_sends == 2:
        return advisory_verified(
            check_id,
            "iTerm2's managed profile sends Option+Return as Escape then Return.",
            expected,
            observed,
            str(path),
        )
    return advisory_warn(
        check_id,
        "The managed iTerm2 profile does not send Option+Return as Escape then Return.",
        expected,
        observed,
        str(path),
        "iterm_option_return_profile_incorrect",
        "Run setup again, restart iTerm2, and select the repaired profile.",
    )
