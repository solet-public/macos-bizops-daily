"""Runtime parser for ``plugin.yaml``'s ``config:`` block.

Until 2026-05-30, ``plugin.yaml``'s declared ``config:`` field defaults were
read **only** at solet birth time (`initialization/src/solets/
plugin_discovery.py`). The platform runtime never parsed the yaml; per-plugin
defaults effectively lived in two unrelated places — the yaml (for operator
inspection) and hardcoded Python ``config_provider.get(key, X)`` literals
(for actual runtime fallback). A third source — the on-disk override file
at ``profile/config/plugins/<name>.json`` — could shadow either silently.
Editing yaml had zero runtime effect; only the override file controlled
behavior.

This module is the v1 fix: a tiny runtime loader that turns
``plugin.yaml``'s ``config:`` block into a ``{field: default}`` dict, used
as the lowest-priority merge layer in ``ConfigManager.get_plugin_config``.
Override file (and env / CLI) overrides win on top. After this loader is
wired, the yaml is authoritative for declared defaults.

Design:

- **Pure parser.** No I/O beyond reading the named plugin.yaml. No state.
  The caller (typically ``PluginInitializer``) supplies the plugin's root
  directory.
- **Skip secret fields.** ``secret: true`` fields flow through the vault
  service, not the plugin-config flow. Surfacing them here would invite
  accidental persistence of credentials in the merged config dict.
- **Skip no-default fields.** A field declared without ``default:`` is
  caller's concern (operator override or ``required: true`` failure at
  v2's init-time validator). The loader does not invent defaults.
- **Trust yaml's native typing.** PyYAML deserialises ``true/false`` to
  ``bool``, integers to ``int``, etc. The downstream consumer does the
  coercion if needed (the existing ``_as_int`` / ``_as_bool`` helpers).

See ``workbench/2026-05-30_plugin_config_defaults_unification.md`` §8 for
the full design + locked decisions.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import yaml

_PLUGIN_YAML_NAME = "plugin.yaml"

logger = logging.getLogger(__name__)


def load_plugin_yaml_defaults(plugin_root: Path | None) -> dict[str, Any]:
    """Return ``{field: default}`` parsed from ``<plugin_root>/plugin.yaml``.

    Args:
        plugin_root: Filesystem root of the plugin (the directory that
            contains ``plugin.yaml``). When ``None`` or when no
            ``plugin.yaml`` is found, the loader returns ``{}`` — the
            merge layer becomes a no-op rather than failing the boot.

    Returns:
        Mapping of field-name → default value. Empty when:

        - ``plugin_root`` is ``None``.
        - ``<plugin_root>/plugin.yaml`` doesn't exist.
        - The yaml has no ``config:`` block.
        - All declared fields are ``secret: true`` or lack ``default:``.

    Raises:
        ValueError: when ``plugin.yaml`` exists but its top-level shape or
            ``config:`` block is malformed (per platform's fail-fast
            policy). YAML parse errors propagate as ``yaml.YAMLError`` —
            also fail-fast; a malformed plugin.yaml is operator intent
            we should not paper over.
    """
    if plugin_root is None:
        return {}
    yaml_path = plugin_root / _PLUGIN_YAML_NAME
    if not yaml_path.is_file():
        return {}

    document = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
    if document is None:
        return {}
    if not isinstance(document, dict):
        raise ValueError(
            f"{yaml_path}: top-level YAML must be a mapping; got "
            f"{type(document).__name__}"
        )

    config_block = document.get("config")
    if config_block is None:
        return {}
    if not isinstance(config_block, dict):
        raise ValueError(
            f"{yaml_path}: 'config' must be a mapping of field-name to "
            f"field-spec; got {type(config_block).__name__}"
        )

    return _extract_defaults_from_config_block(config_block, yaml_path)


def _extract_defaults_from_config_block(
    config_block: dict[Any, Any], yaml_path: Path
) -> dict[str, Any]:
    """Walk a ``config:`` block, returning ``{field: default}`` for usable entries.

    Skips ``secret: true`` fields (vault flow, not config-default flow)
    and fields missing a ``default:`` entry. Logs warnings on malformed
    per-field specs but continues — one broken field does not poison the
    rest of the loader.

    Parameter is ``dict[Any, Any]`` because PyYAML returns untyped mapping
    contents at the boundary; the per-entry ``isinstance`` checks below
    are the load-bearing validation.
    """
    defaults: dict[str, Any] = {}
    for field_name, field_spec in config_block.items():
        if not isinstance(field_name, str) or not field_name:
            logger.warning(
                "Skipping non-string field name in %s config block: %r",
                yaml_path, field_name,
            )
            continue
        if not isinstance(field_spec, dict):
            logger.warning(
                "Skipping malformed field spec for %r in %s: not a mapping",
                field_name, yaml_path,
            )
            continue
        if field_spec.get("secret"):
            continue
        if "default" not in field_spec:
            continue
        defaults[field_name] = field_spec["default"]
    return defaults
