#!/usr/bin/env python3
"""M5.C render smoke — bundled template substitution + credential-marker safety.

Run:

    .venv/bin/python3 plugins/session_shipper_bootstrap_plugin/tests/session_shipper_render_smoke.py

Per KB ``client-deployment-plugin-pattern`` §smoke-testing the contract is:

1. Every bundled template renders cleanly against a complete context dict.
2. No rendered output contains any known credential marker
   (``sk-ant-``, ``sk-``, ``Bearer ``, ``AKIA``, ``ASIA``, ``-----BEGIN``,
   ``password=``, ``passwd:``, ``secret=``, ``api_key=``).
3. Re-rendering the same context yields byte-identical output
   (renderer is deterministic — no datetime / random / env reads).
4. Canary detection works: a fixture template that intentionally flows
   a ``${OAUTH_CLIENT_SECRET}`` placeholder with a ``sk-canary-…`` planted
   value MUST raise :class:`RenderError`. This proves the detector
   actually fires (not just "no real secrets happened to leak").
5. Renderer rejects missing context keys with a clear error rather than
   producing partial output with unrendered ``${VAR}`` placeholders.
6. No rendered output carries any unrendered ``${VAR}`` placeholder
   (defense-in-depth even though substitution would raise on unknowns).
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(
    0,
    str(REPO_ROOT / "plugins" / "session_shipper_bootstrap_plugin" / "src"),
)

from session_shipper_bootstrap_plugin.renderer import (  # noqa: E402
    REQUIRED_CONTEXT_KEYS,
    RenderError,
    credential_markers,
    find_unrendered_placeholders,
    list_templates,
    render_package,
    render_text,
)

_passed = 0
_failed: list[str] = []


def _check(condition: object, label: str) -> None:
    global _passed
    if condition:
        _passed += 1
        print(f"  PASS  {label}")
    else:
        _failed.append(label)
        print(f"  FAIL  {label}")


_FIXTURE_CONTEXT: dict[str, str] = {
    "SOLET_PUBLIC_URL": "https://example.test",
    "DEPLOYMENT_ID": "dep-test-render-001",
    "MACHINE_ID": "machine-render-fixture",
    "INSTALL_DIR": "/opt/session-shipper/dep-test-render-001",
    "LAUNCHD_PLIST_PATH": "/Library/LaunchAgents/local.session-shipper.dep-test-render-001.plist",
    "SYSTEMD_UNIT_PATH": "/etc/systemd/user/session-shipper-dep-test-render-001.service",
    "CLAUDE_CODE_HOOK_PATH": "/Users/op/.config/claude_code/hooks/dep-test-render-001.json",
    "CREDENTIALS_PATH": "/Users/op/.config/dep-test-render-001-credentials.json",
    # Operator-neutral install paths + label (no birther/org name — see
    # renderer.REQUIRED_CONTEXT_KEYS breadcrumb + the router's local.* family).
    "LAUNCHD_LABEL": "local.session-shipper.dep-test-render-001",
    "PYTHON_BIN": "/usr/bin/env python3",
}


def test_all_bundled_templates_render_clean() -> None:
    """Contract #1 + #2: every bundled template renders with no credential markers."""
    rendered = render_package(_FIXTURE_CONTEXT)
    expected = set(list_templates())
    _check(
        set(rendered.keys()) == expected,
        f"render_package returned every bundled template ({len(expected)} files)",
    )
    for name, body in rendered.items():
        for marker in credential_markers():
            _check(
                marker not in body,
                f"rendered {name!r} contains no {marker!r} credential marker",
            )


def test_render_is_deterministic() -> None:
    """Contract #3: same inputs → byte-identical output across multiple calls."""
    first = render_package(_FIXTURE_CONTEXT)
    second = render_package(_FIXTURE_CONTEXT)
    for name in first:
        _check(
            first[name] == second[name],
            f"render of {name!r} is deterministic across two calls",
        )


def test_canary_planted_in_context_triggers_render_error() -> None:
    """Contract #4: detector trips on a planted credential, proving it actually fires."""
    canary_template = (
        "# Test fixture — would-be credential leak\n"
        "client_secret = ${OAUTH_CLIENT_SECRET}\n"
    )
    canary_context = {"OAUTH_CLIENT_SECRET": "sk-canary-NEVERSHIPCREDS123"}
    raised: RenderError | None = None
    try:
        render_text(canary_template, canary_context)
    except RenderError as exc:
        raised = exc
    _check(
        raised is not None,
        "render_text raises RenderError when canary credential flows into output",
    )
    if raised is not None:
        _check(
            "sk-" in str(raised),
            f"RenderError message names the credential marker (got: {raised!s})",
        )


def test_canary_marker_for_real_anthropic_key() -> None:
    """Defense-in-depth: real-shape Anthropic key also trips the detector."""
    template = "API_KEY = ${OAUTH_CLIENT_SECRET}\n"
    # Fragmented (not written whole): fed to render_text() as a context dict
    # value, never read from raw file bytes — but the seal validator scans
    # shipped bytes for exactly this real-shape Anthropic key pattern, so it
    # must be assembled rather than appear as a literal.
    context = {"OAUTH_CLIENT_SECRET": "sk-ant-api03-" + "redactedREDACTEDredacted"}
    raised: RenderError | None = None
    try:
        render_text(template, context)
    except RenderError as exc:
        raised = exc
    _check(
        raised is not None,
        "render_text raises RenderError on a real-shape sk-ant- key",
    )


def test_missing_context_key_raises_render_error() -> None:
    """Contract #5: incomplete context fails cleanly, not silently."""
    incomplete = dict(_FIXTURE_CONTEXT)
    del incomplete["DEPLOYMENT_ID"]
    raised: RenderError | None = None
    try:
        render_package(incomplete)
    except RenderError as exc:
        raised = exc
    _check(
        raised is not None,
        "render_package raises RenderError on missing required context key",
    )
    if raised is not None:
        _check(
            "DEPLOYMENT_ID" in str(raised),
            f"RenderError names the missing key (got: {raised!s})",
        )


def test_required_context_keys_match_bundled_templates() -> None:
    """REQUIRED_CONTEXT_KEYS covers every placeholder bundled templates declare."""
    import re  # noqa: PLC0415
    bundled_keys: set[str] = set()
    for name in list_templates():
        text = (
            REPO_ROOT
            / "plugins"
            / "session_shipper_bootstrap_plugin"
            / "src"
            / "session_shipper_bootstrap_plugin"
            / "templates"
            / name
        ).read_text(encoding="utf-8")
        bundled_keys.update(re.findall(r"\$\{([A-Z][A-Z0-9_]*)\}", text))
    missing = bundled_keys - REQUIRED_CONTEXT_KEYS
    _check(
        not missing,
        f"REQUIRED_CONTEXT_KEYS covers every bundled placeholder (missing: {sorted(missing)})",
    )


def test_no_unrendered_placeholders_in_output() -> None:
    """Contract #6: defense-in-depth — no `${VAR}` stays in output."""
    rendered = render_package(_FIXTURE_CONTEXT)
    for name, body in rendered.items():
        unrendered = find_unrendered_placeholders(body)
        _check(
            not unrendered,
            f"{name!r}: no unrendered ${{...}} placeholders (got: {unrendered})",
        )


def main() -> int:
    print("=== session_shipper_render_smoke (M5.C deferral #1) ===")
    test_all_bundled_templates_render_clean()
    test_render_is_deterministic()
    test_canary_planted_in_context_triggers_render_error()
    test_canary_marker_for_real_anthropic_key()
    test_missing_context_key_raises_render_error()
    test_required_context_keys_match_bundled_templates()
    test_no_unrendered_placeholders_in_output()
    print(f"\n{_passed} passed, {len(_failed)} failed")
    if _failed:
        for label in _failed:
            print(f"  FAILED: {label}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
