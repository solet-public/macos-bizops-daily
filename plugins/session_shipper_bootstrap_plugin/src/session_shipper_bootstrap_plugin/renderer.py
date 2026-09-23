"""Shipper-template renderer.

Substitutes ``${VAR}`` placeholders in the bundled templates with
operator-provided context values, then runs a shape-based credential-
marker scan over every rendered output. Any marker hit raises
:class:`RenderError`; the install bundle is NEVER returned to the
caller with a leaked credential.

Per KB ``client-deployment-plugin-pattern`` (article 17):

* Markers are shape-based — known prefixes/patterns only. No
  length-based regex (would false-positive on legitimate opaque ids).
* The canary token ``sk-canary-NEVERSHIPCREDS123`` (KB §smoke contract)
  matches the ``sk-`` marker, so feeding it into ANY context value
  flows through to detection. The render smoke uses this fact to
  verify detection works rather than relying on the absence of real
  secrets.
* The render package is a pure transform: same context + same templates
  → byte-identical output (no datetime, no random ids, no environment
  reads).

The templates live in the sibling ``templates/`` directory; the renderer
discovers them at import time and caches their text. Adding a template
means dropping a ``.template`` file in the directory — no renderer
edits required.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from importlib import resources
from string import Template
from typing import Final

_TEMPLATES_PACKAGE = "session_shipper_bootstrap_plugin.templates"

# Shape-based credential markers. Order is informational. Any output
# substring matching one of these in any rendered file triggers a
# RenderError. Length-based regex (e.g. [A-Za-z0-9_-]{32,}) is
# explicitly avoided because it false-positives on legitimate opaque
# tokens (deployment_ids, urls, content hashes, machine identifiers).
_CREDENTIAL_MARKERS: Final[tuple[str, ...]] = (
    "sk-ant-",     # Anthropic API key prefix
    "sk-",         # OpenAI / generic provider prefix (also covers sk-canary-)
    "Bearer ",     # bearer-token Authorization header value
    "AKIA",        # AWS access key id (long-lived)
    "ASIA",        # AWS access key id (temporary)
    "-----BEGIN",  # PEM key / certificate header
    "password=",   # connection-string password
    "passwd:",     # generic password yaml
    "secret=",     # generic secret querystring
    "api_key=",    # generic api key querystring
)

# Required context keys for the BUNDLED templates. Caller-side renderers
# (e.g. the future apply_manifest installer) supply these; missing keys
# raise RenderError before any template substitution runs. Future
# credential-embedded templates (post-pairing config writers) would add
# OAUTH_CLIENT_ID / OAUTH_CLIENT_SECRET here — the credential-marker
# scan catches misuse, so adding them is safe.
#
# LAUNCHD_LABEL must be OPERATOR-NEUTRAL — no `com.<org>` birther prefix.
# The shipper installs onto the operator's own machine; stamping the
# birther's reverse-DNS namespace into the label risks colliding with an
# org-owned plist and leaks the birther identity into a client deployment.
# Mirror the router's neutral `local.*` family (see
# `macos_self_deployment_plugin` service_install.launchd_label). The
# bundled test fixtures use `local.session-shipper.<deployment_id>`.
REQUIRED_CONTEXT_KEYS: Final[frozenset[str]] = frozenset({
    "SOLET_PUBLIC_URL",
    "DEPLOYMENT_ID",
    "MACHINE_ID",
    "INSTALL_DIR",
    "LAUNCHD_PLIST_PATH",
    "SYSTEMD_UNIT_PATH",
    "CLAUDE_CODE_HOOK_PATH",
    "CREDENTIALS_PATH",
    "LAUNCHD_LABEL",
    "PYTHON_BIN",
})


class RenderError(RuntimeError):
    """Renderer refused to emit a template output."""


def list_templates() -> list[str]:
    """All ``*.template`` filenames bundled under the templates package."""
    return sorted(
        entry.name
        for entry in resources.files(_TEMPLATES_PACKAGE).iterdir()
        if entry.name.endswith(".template")
    )


def read_template(name: str) -> str:
    """Read one template by name. Raises FileNotFoundError if absent."""
    return resources.files(_TEMPLATES_PACKAGE).joinpath(name).read_text(
        encoding="utf-8",
    )


def render_one(template_text: str, context: Mapping[str, str]) -> str:
    """Substitute ``${VAR}`` placeholders then scan for credential markers.

    Raises :class:`RenderError` on missing context keys (caller asked
    for a placeholder we have no value for) or on credential-marker
    hits in the rendered output (a template tried to flow a credential).
    """
    try:
        rendered = Template(template_text).substitute(context)
    except KeyError as exc:
        raise RenderError(
            f"missing context key {exc.args[0]!r} for template substitution",
        ) from None
    leaked = _find_credential_marker(rendered)
    if leaked is not None:
        raise RenderError(
            f"refusing to render: credential marker {leaked!r} appeared in "
            f"template output (would leak a secret to disk)",
        )
    return rendered


def render_package(context: Mapping[str, str]) -> dict[str, str]:
    """Render every bundled template against ``context``.

    Returns ``{template_name: rendered_text}``. Raises :class:`RenderError`
    on the FIRST credential-marker hit (no partial bundles — either
    every output is clean, or the caller gets a fail-closed error).
    """
    _require_full_context(context)
    return {name: render_one(read_template(name), context) for name in list_templates()}


def _require_full_context(context: Mapping[str, str]) -> None:
    missing = REQUIRED_CONTEXT_KEYS - context.keys()
    if missing:
        raise RenderError(
            f"context missing required keys: {sorted(missing)}",
        )


def _find_credential_marker(text: str) -> str | None:
    for marker in _CREDENTIAL_MARKERS:
        if marker in text:
            return marker
    return None


# Public test-only helper: render a single ad-hoc template string. The
# orphan / canary smokes use this to confirm the detector triggers on a
# fixture template that intentionally flows a credential variable.
def render_text(template_text: str, context: Mapping[str, str]) -> str:
    """Render a single template string. Same scan + raise contract as
    :func:`render_one`, but does not require a bundled ``.template`` file
    (intended for smoke fixtures + ad-hoc renderer invocations)."""
    return render_one(template_text, context)


# Regex export — used by smokes that want to test marker overlap.
def credential_markers() -> tuple[str, ...]:
    """The full credential-marker tuple."""
    return _CREDENTIAL_MARKERS


# Sanity check at import time: confirm the canary token format the KB
# pattern article specifies actually trips at least one marker. If a
# future edit drops the ``sk-`` marker, this raises at module-load and
# prevents a silent loss of canary detection.
_CANARY_SENTINEL = "sk-canary-NEVERSHIPCREDS123"
if _find_credential_marker(_CANARY_SENTINEL) is None:
    raise AssertionError(
        "renderer canary sentinel no longer matches any credential marker "
        "— canary-detection coverage has regressed",
    )


# Re-exported regex helper for smokes that want to assert what a marker
# substring looks like in the canonical form.
_CONTEXT_PLACEHOLDER_RE = re.compile(r"\$\{[A-Z][A-Z0-9_]*\}")


def find_unrendered_placeholders(text: str) -> list[str]:
    """Return any ``${...}`` placeholder substrings still present in ``text``.

    The renderer's substitution would normally raise on unknown keys, so
    a non-empty list here points at a template authoring mistake (the
    placeholder name isn't in REQUIRED_CONTEXT_KEYS and the caller never
    populated it). Smokes use this as a defense-in-depth assertion.
    """
    return _CONTEXT_PLACEHOLDER_RE.findall(text)


__all__ = [
    "REQUIRED_CONTEXT_KEYS",
    "RenderError",
    "credential_markers",
    "find_unrendered_placeholders",
    "list_templates",
    "read_template",
    "render_one",
    "render_package",
    "render_text",
]
