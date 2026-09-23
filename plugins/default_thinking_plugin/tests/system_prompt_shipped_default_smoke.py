"""Smoke: default_thinking_plugin's shipped-default system prompt.

Until now, ``profile/config/prompts/thinking_system_prompt.md`` was the
ONLY source for the reasoning system prompt -- but ``profile/config/`` is
deliberately excluded from every seed (genesis boundary), so every
solet born from the github-seed pipeline silently ran with an EMPTY
planning prompt (confirmed live on the v9/v10 from-scratch verification
runs). The fix ships a plugin-owned default at
``knowledge_base/prompts/thinking_system_prompt.md`` (package-relative,
present in every seed that includes this plugin) and falls back to it
only when no operator override exists.

Coverage:

1. The shipped default file exists on disk and is non-empty.
2. No override present -> ``_load_system_prompt`` loads the shipped default.
3. An override file present -> it wins over the shipped default.
4. Neither present (shipped-path resolution monkeypatched away) -> returns
   "" (unchanged fail-soft behavior for a genuinely corrupted install).

Run directly: ``.venv/bin/python3 plugins/default_thinking_plugin/tests/system_prompt_shipped_default_smoke.py``.
"""

from __future__ import annotations

# ruff: noqa: E402
import logging
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

_PLUGIN_ROOT = Path(__file__).resolve().parents[1]
_SRC = _PLUGIN_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from default_thinking_plugin import plugin as _plugin_module  # noqa: E402
from default_thinking_plugin.plugin import (  # noqa: E402
    DefaultThinkingPlugin,
    _resolve_shipped_system_prompt_path,
)

_CHECKS_RUN: list[str] = []


class SmokeFailureError(AssertionError):
    """Raised on any check failure; message is the failure detail."""


def _check(label: str, condition: bool, detail: str) -> None:
    _CHECKS_RUN.append(label)
    if not condition:
        raise SmokeFailureError(f"{label}: {detail}")


class _FakeConfigProvider:
    def __init__(self, system_prompt_path: str) -> None:
        self._path = system_prompt_path

    def get(self, key: str) -> str:
        assert key == "system_prompt_path"
        return self._path


def _fake_self() -> SimpleNamespace:
    return SimpleNamespace(logger=logging.getLogger("system_prompt_shipped_default_smoke"))


def _load(app_home: str, override_rel: str) -> str:
    config_provider = _FakeConfigProvider(override_rel)
    return DefaultThinkingPlugin._load_system_prompt(_fake_self(), app_home, config_provider)  # type: ignore[arg-type]


def _check_shipped_default_exists_on_disk() -> None:
    shipped_path = _resolve_shipped_system_prompt_path()
    _check("shipped default file exists", shipped_path.is_file(), f"missing at {shipped_path}")
    content = shipped_path.read_text(encoding="utf-8")
    _check("shipped default is non-empty", bool(content.strip()), "shipped default is empty")
    _check(
        "shipped default is the real planning prompt, not a placeholder",
        "execution planner" in content,
        f"unexpected content: {content[:80]!r}",
    )


def _check_falls_back_to_shipped_default_when_no_override() -> None:
    with tempfile.TemporaryDirectory() as app_home:
        # config/prompts/thinking_system_prompt.md deliberately absent.
        prompt = _load(app_home, "config/prompts/thinking_system_prompt.md")
        shipped_content = _resolve_shipped_system_prompt_path().read_text(encoding="utf-8").rstrip("\n")
        _check(
            "no override -> loads shipped default verbatim",
            prompt == shipped_content,
            f"got {len(prompt)} chars, expected {len(shipped_content)} chars",
        )


def _check_override_wins_when_present() -> None:
    with tempfile.TemporaryDirectory() as app_home:
        override_rel = "config/prompts/thinking_system_prompt.md"
        override_path = Path(app_home) / override_rel
        override_path.parent.mkdir(parents=True)
        override_path.write_text("CUSTOM OPERATOR PROMPT", encoding="utf-8")

        prompt = _load(app_home, override_rel)
        _check("override present -> wins over shipped default", prompt == "CUSTOM OPERATOR PROMPT", f"got {prompt!r}")


def _check_neither_present_returns_empty() -> None:
    original_resolver = _plugin_module._resolve_shipped_system_prompt_path
    with tempfile.TemporaryDirectory() as scratch:
        nonexistent = Path(scratch) / "does_not_exist.md"
        _plugin_module._resolve_shipped_system_prompt_path = lambda: nonexistent  # type: ignore[assignment]
        try:
            with tempfile.TemporaryDirectory() as app_home:
                prompt = _load(app_home, "config/prompts/thinking_system_prompt.md")
                _check("neither override nor shipped default -> empty string", prompt == "", f"got {prompt!r}")
        finally:
            _plugin_module._resolve_shipped_system_prompt_path = original_resolver


def main() -> int:
    try:
        _check_shipped_default_exists_on_disk()
        _check_falls_back_to_shipped_default_when_no_override()
        _check_override_wins_when_present()
        _check_neither_present_returns_empty()
    except SmokeFailureError as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        print(f"  ({len(_CHECKS_RUN)} checks attempted before failure)", file=sys.stderr)
        return 1

    print(f"system_prompt_shipped_default_smoke OK: {len(_CHECKS_RUN)} checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
