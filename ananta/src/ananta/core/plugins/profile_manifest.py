"""Profile manifest loader for runtime plugin filtering.

A solet's birth-time profile (cloud / local / custom) is captured
in ``<APP_HOME>/config/manifest.yaml``. At startup, the plugin manager
reads this file and only loads entry points whose name appears in its
``plugins`` list — everything else is skipped even if installed.

Manifest absent = no gating (load every installed entry point). This is
the sensible default for dev boxes and pre-A5 solets that pre-date
profile templates; new solets born under A5+ always write a manifest.
"""

from __future__ import annotations

import logging
from pathlib import Path

import yaml

logger = logging.getLogger(__name__)

_MANIFEST_RELATIVE_PATH = Path("config") / "manifest.yaml"


def load_manifest_plugin_set(app_home: str | Path) -> set[str] | None:
    """Return the set of plugin names declared in ``<APP_HOME>/config/manifest.yaml``.

    Returns ``None`` when the manifest is absent — the caller should
    interpret this as "no gating, load every installed entry point."

    Raises ``ValueError`` when the manifest exists but is malformed. The
    fast-fail policy applies: a present-but-broken manifest is operator
    intent that we should not paper over.
    """
    manifest_path = Path(app_home) / _MANIFEST_RELATIVE_PATH
    if not manifest_path.is_file():
        return None

    raw = yaml.safe_load(manifest_path.read_text())
    if not isinstance(raw, dict):
        raise ValueError(f"{manifest_path}: top-level YAML must be a mapping")

    plugins = raw.get("plugins")
    if not isinstance(plugins, list) or not plugins:
        raise ValueError(f"{manifest_path}: 'plugins' must be a non-empty list")
    for entry in plugins:
        if not isinstance(entry, str) or not entry:
            raise ValueError(
                f"{manifest_path}: every plugin entry must be a non-empty string"
            )

    return set(plugins)
