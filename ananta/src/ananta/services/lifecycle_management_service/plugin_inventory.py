"""Source-of-truth enumeration for ``list_available_plugins``.

Codex review of the 2026-05-30 platform/plugin-split design (Finding 6 in
``workbench/2026-05-30_plugin_lifecycle_codex_review.md``) called out that a
``plugin.yaml``-only scan misses 11 plugins that ship valid pyproject-only
entry-point metadata (``s3_blob_storage_plugin``, ``ssml_plugin``,
``pedalboard_effects_plugin``, etc.). The authoritative source is the
``ananta.plugins`` entry-point group — exactly what
:class:`~ananta.core.plugins.plugin_discovery.PluginDiscovery` walks at
startup.

This module enumerates:

1. **Installed plugins** — every entry point registered under the
   ``ananta.plugins`` group. These can be loaded by name into the live
   roster without first running ``install_plugin_from_path``.
2. **Source-tree candidates** — directories under the repo's ``plugins/``
   root that ship a ``plugin.yaml`` OR ``pyproject.toml`` but are NOT
   currently registered as an entry point. These are candidates for
   ``install_plugin_from_path``.

Each entry's ``source`` field discriminates the two cases. Enrichment fields
(``description``, ``version``, ``implements``) prefer source-tree metadata
when both are available — the on-disk ``plugin.yaml`` is the human-authored
shape; ``importlib.metadata`` falls back on packaging defaults that may not
match operator intent. ``has_metadata`` flips ``False`` only when neither a
source ``plugin.yaml``/``pyproject.toml`` nor a distribution is reachable.
"""

from __future__ import annotations

import logging
import tomllib
from collections.abc import Iterable
from dataclasses import dataclass, field
from importlib import metadata as importlib_metadata
from pathlib import Path
from typing import Any

import yaml

_SOURCE_INSTALLED = "installed_entry_point"
_SOURCE_AVAILABLE_UNINSTALLED = "available_uninstalled"

_ENTRY_POINT_GROUP = "ananta.plugins"
_PLUGIN_YAML_NAME = "plugin.yaml"
_PYPROJECT_NAME = "pyproject.toml"

# Walk from .../ananta/src/ananta/services/lifecycle_management_service/plugin_inventory.py
# to the repo root, where the ``plugins/`` sibling-of-``ananta/`` lives.
_REPO_PLUGINS_ROOT = Path(__file__).resolve().parents[5] / "plugins"

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class AvailablePlugin:
    """One row in the ``list_available_plugins`` response.

    ``source`` is one of:
        * ``"installed_entry_point"``   — discoverable via
          ``importlib.metadata.entry_points(group="ananta.plugins")``.
          ``apply_manifest`` can load this plugin by name directly.
        * ``"available_uninstalled"``   — present on disk under
          ``plugins/<name>/`` with valid metadata but not installed; the
          operator must call ``install_plugin_from_path`` before
          ``apply_manifest`` can use it.

    ``has_metadata`` is ``True`` whenever any of ``plugin.yaml``,
    ``pyproject.toml``, or a distribution record yielded usable values
    for ``description``, ``version``, or ``implements``.
    """

    name: str
    source: str
    has_metadata: bool
    version: str | None
    description: str | None
    implements: tuple[str, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict[str, Any]:
        """Render as the response-row dict that the verb returns."""
        return {
            "name": self.name,
            "source": self.source,
            "has_metadata": self.has_metadata,
            "version": self.version,
            "description": self.description,
            "implements": list(self.implements),
        }


def resolve_plugins_root() -> Path:
    """Return the repo's ``plugins/`` directory (sibling-of-``ananta/``).

    Pinned via ``__file__``-relative walk so the function works regardless
    of cwd. Production deployments that strip the source tree out of the
    image will see this directory as absent; callers must tolerate that —
    :func:`enumerate_available_plugins` returns only the entry-point-set
    in that case, which is the correct semantic.
    """
    return _REPO_PLUGINS_ROOT


def enumerate_available_plugins(
    plugins_root: Path | None = None,
) -> list[AvailablePlugin]:
    """Enumerate every plugin loadable into ``apply_manifest``'s next manifest.

    Args:
        plugins_root: Directory to scan for source-tree candidates. Pass
            ``None`` to use :func:`resolve_plugins_root` (the canonical
            repo location). When the directory is absent, source-tree
            enumeration silently returns the empty set — production-image
            deployments without checked-out source see only installed
            entry points, which is correct.

    Returns:
        Plugins sorted alphabetically by name. ``installed_entry_point``
        and ``available_uninstalled`` entries are interleaved in one list;
        the ``source`` field discriminates. No deduplication is needed —
        the name is unique by construction.
    """
    root = plugins_root if plugins_root is not None else resolve_plugins_root()
    installed_entry_points = _scan_entry_points()
    source_tree_dirs = _scan_source_tree(root)

    all_names = sorted(set(installed_entry_points) | set(source_tree_dirs))
    return [
        _build_plugin_row(
            name=name,
            entry_point=installed_entry_points.get(name),
            source_dir=source_tree_dirs.get(name),
        )
        for name in all_names
    ]


def _scan_entry_points() -> dict[str, importlib_metadata.EntryPoint]:
    """Return every installed entry point in the ``ananta.plugins`` group."""
    eps = importlib_metadata.entry_points().select(group=_ENTRY_POINT_GROUP)
    return {ep.name: ep for ep in eps}


def _scan_source_tree(plugins_root: Path) -> dict[str, Path]:
    """Return source-tree plugin directories keyed by directory name.

    A directory is a candidate if it contains either ``plugin.yaml`` or
    ``pyproject.toml`` — Codex Finding 6 makes pyproject-only plugins
    first-class. Returns empty when ``plugins_root`` is absent (e.g.
    production image without source).
    """
    if not plugins_root.is_dir():
        return {}
    result: dict[str, Path] = {}
    for entry in plugins_root.iterdir():
        if not entry.is_dir():
            continue
        if (entry / _PLUGIN_YAML_NAME).is_file() or (entry / _PYPROJECT_NAME).is_file():
            result[entry.name] = entry
    return result


def _build_plugin_row(
    *,
    name: str,
    entry_point: importlib_metadata.EntryPoint | None,
    source_dir: Path | None,
) -> AvailablePlugin:
    """Materialise one :class:`AvailablePlugin` row from its sources."""
    source = (
        _SOURCE_INSTALLED if entry_point is not None else _SOURCE_AVAILABLE_UNINSTALLED
    )
    version, description, implements = _read_metadata(entry_point, source_dir)
    has_metadata = (
        version is not None
        or description is not None
        or bool(implements)
    )
    return AvailablePlugin(
        name=name,
        source=source,
        has_metadata=has_metadata,
        version=version,
        description=description,
        implements=implements,
    )


def _read_metadata(
    entry_point: importlib_metadata.EntryPoint | None,
    source_dir: Path | None,
) -> tuple[str | None, str | None, tuple[str, ...]]:
    """Merge source-tree + distribution metadata; source-tree wins.

    Returns ``(version, description, implements)``. Source-tree
    ``plugin.yaml`` carries the human-authored shape and is preferred for
    ``description`` and ``implements``. ``version`` falls back to
    ``pyproject.toml`` then to the installed distribution.
    """
    src_yaml = _read_plugin_yaml(source_dir)
    src_pyproject_version = _read_pyproject_version(source_dir)
    dist_version, dist_summary = _read_distribution_metadata(entry_point)

    description = src_yaml.description or dist_summary
    implements = src_yaml.implements
    version = src_pyproject_version or dist_version
    return version, description, implements


@dataclass(frozen=True, slots=True)
class _PluginYamlMetadata:
    """Lightweight container for the fields we care about in plugin.yaml."""

    description: str | None = None
    implements: tuple[str, ...] = field(default_factory=tuple)


def _read_plugin_yaml(source_dir: Path | None) -> _PluginYamlMetadata:
    """Return parsed ``plugin.yaml`` fields, or empty values on absence/error."""
    if source_dir is None:
        return _PluginYamlMetadata()
    plugin_yaml = source_dir / _PLUGIN_YAML_NAME
    if not plugin_yaml.is_file():
        return _PluginYamlMetadata()
    try:
        doc = yaml.safe_load(plugin_yaml.read_text())
    except yaml.YAMLError as exc:
        logger.warning(f"Skipping malformed {plugin_yaml}: {exc}")
        return _PluginYamlMetadata()
    if not isinstance(doc, dict):
        return _PluginYamlMetadata()
    return _PluginYamlMetadata(
        description=_extract_str(doc, "description"),
        implements=_extract_implements(doc.get("implements")),
    )


def _read_pyproject_version(source_dir: Path | None) -> str | None:
    """Return the ``[project] version`` field from pyproject.toml, or None."""
    if source_dir is None:
        return None
    pyproject = source_dir / _PYPROJECT_NAME
    if not pyproject.is_file():
        return None
    try:
        with pyproject.open("rb") as fh:
            data = tomllib.load(fh)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        logger.warning(f"Skipping malformed {pyproject}: {exc}")
        return None
    return _extract_str(data.get("project") or {}, "version")


def _read_distribution_metadata(
    entry_point: importlib_metadata.EntryPoint | None,
) -> tuple[str | None, str | None]:
    """Return ``(version, summary)`` from the entry point's distribution."""
    if entry_point is None or entry_point.dist is None:
        return None, None
    dist = entry_point.dist
    summary = dist.metadata["Summary"] if "Summary" in dist.metadata else None
    return dist.version, summary


def _extract_str(doc: dict[str, Any], key: str) -> str | None:
    """Return a non-empty string at ``doc[key]``, or ``None``."""
    value = doc.get(key)
    return value if isinstance(value, str) and value else None


def _extract_implements(raw: Any) -> tuple[str, ...]:
    """Parse plugin.yaml's ``implements`` list into a tuple of interface names.

    Accepts the documented shape ``[{interface: <name>}, ...]`` and falls
    back to the empty tuple for anything malformed. The ``implements``
    field is a hint, not a contract — the authoritative declaration is
    ``plugin.service_interfaces`` at runtime — so silent fallback is the
    right behaviour here.
    """
    if not isinstance(raw, list):
        return ()
    interfaces: list[str] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        name = entry.get("interface")
        if isinstance(name, str) and name:
            interfaces.append(name)
    return tuple(interfaces)


def as_response_rows(plugins: Iterable[AvailablePlugin]) -> list[dict[str, Any]]:
    """Render :class:`AvailablePlugin` rows as response dicts."""
    return [plugin.to_dict() for plugin in plugins]
