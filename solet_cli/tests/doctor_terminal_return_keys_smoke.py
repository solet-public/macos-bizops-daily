"""Offline coverage for the tmux and iTerm2 Return-key doctor advisories."""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from solet_manager.doctor_terminal_return_keys import (  # noqa: E402
    _DYNAMIC_PROFILE,
    _PROFILE_NAME,
    _TMUX_LINES,
    collect_terminal_return_key_advisories,
)

_CHECKS = 0


class _Record:
    name = "terminal-census"
    target = "/nonexistent/terminal-census"


def _check(condition: bool, label: str) -> None:
    global _CHECKS
    _CHECKS += 1
    if not condition:
        raise AssertionError(f"red: {label}")


def _advisories(home: Path) -> list[dict[str, object]]:
    rows = collect_terminal_return_key_advisories(_Record(), home=home)
    _check(len(rows) == 2, f"expected two terminal advisories, got {len(rows)}")
    return [dict(row) for row in rows if isinstance(row, dict)]


def _by_id(rows: list[dict[str, object]], check_id: str) -> dict[str, object]:
    found = [row for row in rows if row["check_id"] == check_id]
    _check(len(found) == 1, f"missing advisory {check_id}")
    return found[0]


def main() -> int:
    with tempfile.TemporaryDirectory() as temporary:
        home = Path(temporary)
        missing = _advisories(home)
        tmux = _by_id(missing, "doctor::tmux_return_key_modifiers_v1")
        iterm = _by_id(missing, "doctor::iterm_option_return_profile_v1")
        _check(tmux["status"] == "warn", "missing tmux config must warn")
        _check(tmux["reason_code"] == "tmux_return_key_config_absent", "tmux absence named")
        _check(iterm["status"] == "warn", "missing iTerm profile must warn")
        _check(iterm["reason_code"] == "iterm_option_return_profile_absent", "iTerm absence named")

        (home / ".tmux.conf").write_text(_TMUX_LINES[0] + "\n", encoding="utf-8")
        profile_path = home / _DYNAMIC_PROFILE
        profile_path.parent.mkdir(parents=True)
        profile_path.write_text(
            json.dumps({"Profiles": [{"Name": _PROFILE_NAME, "Option Key Sends": 0}]}),
            encoding="utf-8",
        )
        partial = _advisories(home)
        tmux = _by_id(partial, "doctor::tmux_return_key_modifiers_v1")
        iterm = _by_id(partial, "doctor::iterm_option_return_profile_v1")
        _check(tmux["reason_code"] == "tmux_return_key_config_incomplete", "tmux partial named")
        _check(iterm["reason_code"] == "iterm_option_return_profile_incorrect", "wrong Option mode named")

        (home / ".tmux.conf").write_text("\n".join(_TMUX_LINES) + "\n", encoding="utf-8")
        profile_path.write_text(
            json.dumps({"Profiles": [{"Name": _PROFILE_NAME, "Option Key Sends": 2}]}),
            encoding="utf-8",
        )
        green = _advisories(home)
        _check(
            all(row["status"] == "verified" and row["blocking"] is False for row in green),
            "both terminal halves verify as non-blocking advisories",
        )
    print(f"doctor_terminal_return_keys_smoke OK: {_CHECKS} checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
