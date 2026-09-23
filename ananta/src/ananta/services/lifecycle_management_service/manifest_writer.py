"""Atomic write + CAS + diff helpers for ``apply_manifest``.

``apply_manifest`` writes a new plugin manifest and service bindings to disk
before delegating restart to the bound deployment plugin. The writes must be:

1. **Atomic per file** — readers (startup_sequence Steps 3 and 4) must
   never observe a half-written manifest. ``write_*`` uses
   ``Path.replace`` (POSIX ``rename(2)``) after writing to a sibling
   ``.tmp`` file in the same directory.
2. **CAS-checked** — concurrent ``apply_manifest`` invocations are
   serialised via a content-hash etag. The caller passes the etag they
   read before mutating; the writer refuses if the on-disk etag has
   shifted in the interim.
3. **Ordered** — ``service_bindings.json`` is written FIRST, then
   ``manifest.yaml``. Reason: ``startup_sequence._init_plugin_manager``
   reads the manifest at Step 3 and ``_load_service_bindings`` reads
   bindings at Step 4. A partial-write window where manifest is new but
   bindings are old causes Step 4's required-service-bound StartupError
   (fail-fast on inconsistent state) rather than silent-boot into the
   old binding set. The reverse order would prefer silent inconsistency,
   which violates the platform's fail-fast policy.

The module is platform-tier infrastructure: the helpers are pure I/O
functions, no orchestrator coupling, no MCP envelopes. The service-layer
caller adapts results into the ``action_status`` dict shape.

v1 prototype scope (per Coordinator dispatch 2026-05-30 Q2 answer C):
the manifest carries ``plugins`` + ``service_bindings`` only. The
``plugin_config_overrides`` key documented in Architect's design (§4.2)
is deferred to a later cycle — the v1 demo loop doesn't exercise it.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

_MANIFEST_RELATIVE_PATH = Path("config") / "manifest.yaml"
_BINDINGS_RELATIVE_PATH = Path("config") / "service_bindings.json"
_TEMP_SUFFIX = ".tmp"


@dataclass(frozen=True, slots=True)
class CurrentManifestState:
    """Snapshot of the on-disk manifest + bindings at the moment of read.

    ``plugins`` and ``service_bindings`` carry the parsed contents;
    ``manifest_bytes`` and ``bindings_bytes`` carry the raw bytes so the
    L2 probe-failure rollback path (Architect's local blue/green design
    Coordinator review Finding 3) can atomically restore the prior
    on-disk state without re-rendering. ``etag`` is the sha256 over the
    (manifest_bytes, bindings_bytes) pair, used by
    :func:`write_new_manifest`'s CAS gate.
    """

    plugins: tuple[str, ...]
    service_bindings: dict[str, str]
    manifest_bytes: bytes
    bindings_bytes: bytes
    etag: str


@dataclass(frozen=True, slots=True)
class ManifestDiff:
    """Set-level diff between two manifest states."""

    added_plugins: tuple[str, ...]
    removed_plugins: tuple[str, ...]
    rebound_services: tuple[str, ...]


class ManifestPreconditionFailedError(Exception):
    """Raised when ``write_new_manifest`` detects an etag mismatch."""

    def __init__(self, expected_etag: str, current_etag: str) -> None:
        self.expected_etag = expected_etag
        self.current_etag = current_etag
        super().__init__(
            "apply_manifest precondition failed: expected etag "
            f"{expected_etag!r}, current is {current_etag!r}; "
            "re-read current state and re-submit."
        )


class ManifestPartialWriteError(Exception):
    """An atomic write failed AFTER the CAS check passed (partial-write window).

    Carries ``pre_write_state`` — the snapshot ``write_new_manifest``
    actually CAS-checked immediately before writing — so the caller
    restores exactly the bytes the CAS validated (GTE-06 T4): restoring
    an earlier (entry-time) snapshot can resurrect bytes OLDER than the
    CAS anchor under concurrency.
    """

    def __init__(
        self, *, pre_write_state: CurrentManifestState, original: OSError
    ) -> None:
        self.pre_write_state = pre_write_state
        self.original = original
        super().__init__(
            f"manifest write failed mid-flight after the CAS check passed: {original}"
        )


@dataclass(frozen=True, slots=True)
class ManifestWriteOutcome:
    """Successful ``write_new_manifest`` result.

    ``pre_write_state`` is the CAS-time snapshot (the state the write was
    actually validated against, captured atomically with the CAS read).
    Any rollback of this write — notably the GTE-06 L2 probe-failure
    rollback — MUST restore THESE bytes, not an earlier read (T4).
    """

    bindings_path: Path
    manifest_path: Path
    new_etag: str
    pre_write_state: CurrentManifestState


def read_current_manifest_state(app_home: Path) -> CurrentManifestState:
    """Read the current on-disk manifest + service bindings + their etag.

    Either file may be absent on a freshly-birthed solet that has
    never run ``apply_manifest`` — in that case the corresponding
    collection is empty and the etag still hashes the absent-file
    sentinel (``b""``). Subsequent ``apply_manifest`` calls observing the
    same empty-state etag will succeed; calls passing a stale etag from
    a prior ``apply_manifest`` will fail CAS.
    """
    manifest_path = app_home / _MANIFEST_RELATIVE_PATH
    bindings_path = app_home / _BINDINGS_RELATIVE_PATH

    manifest_bytes = manifest_path.read_bytes() if manifest_path.is_file() else b""
    bindings_bytes = bindings_path.read_bytes() if bindings_path.is_file() else b""

    plugins = _extract_plugins_from_manifest_bytes(manifest_bytes)
    bindings = _extract_bindings_from_bytes(bindings_bytes)
    etag = _compute_etag(manifest_bytes, bindings_bytes)

    return CurrentManifestState(
        plugins=plugins,
        service_bindings=bindings,
        manifest_bytes=manifest_bytes,
        bindings_bytes=bindings_bytes,
        etag=etag,
    )


def restore_previous_manifest(
    app_home: Path,
    *,
    manifest_bytes: bytes,
    bindings_bytes: bytes,
) -> tuple[Path, Path]:
    """Atomically restore the prior on-disk manifest + bindings.

    Used by ``apply_manifest`` when the L2 probe rejects a manifest:
    the new bytes were already committed to disk before the probe
    fired, so we need to put back exactly what was there before.

    ``manifest_bytes`` / ``bindings_bytes`` come from the
    :class:`CurrentManifestState` snapshot the caller captured before
    ``write_new_manifest`` ran. Both writes are temp+rename so the
    on-disk state never observes a half-restored shape.

    Empty bytes indicate the file did not exist in the prior state
    (freshly-birthed solet); in that case the corresponding file
    is removed rather than written. Per Architect's local blue/green
    design Coordinator review Finding 3.
    """
    manifest_path = app_home / _MANIFEST_RELATIVE_PATH
    bindings_path = app_home / _BINDINGS_RELATIVE_PATH

    if bindings_bytes:
        _atomic_write_bytes(bindings_path, bindings_bytes)
    elif bindings_path.is_file():
        bindings_path.unlink()

    if manifest_bytes:
        _atomic_write_bytes(manifest_path, manifest_bytes)
    elif manifest_path.is_file():
        manifest_path.unlink()

    return bindings_path, manifest_path


def diff_manifest(
    current: CurrentManifestState,
    new_manifest: Mapping[str, Any],
) -> ManifestDiff:
    """Compute the set-level diff between current state and the proposed manifest.

    The diff is informational — used to populate ``apply_manifest``'s
    response so the operator can see what will change before the
    restart. Pure function; no I/O.
    """
    new_plugins = _extract_new_plugins(new_manifest)
    new_bindings = _extract_new_bindings(new_manifest)

    current_plugins = set(current.plugins)
    added = tuple(sorted(new_plugins - current_plugins))
    removed = tuple(sorted(current_plugins - new_plugins))

    rebound = tuple(
        sorted(
            service
            for service, provider in new_bindings.items()
            if service in current.service_bindings
            and current.service_bindings[service] != provider
        )
    )

    return ManifestDiff(
        added_plugins=added,
        removed_plugins=removed,
        rebound_services=rebound,
    )


def write_new_manifest(
    app_home: Path,
    new_manifest: Mapping[str, Any],
    *,
    expected_etag: str,
    profile_name: str = "local",
) -> ManifestWriteOutcome:
    """Atomically write the new manifest + bindings; refuse on CAS mismatch.

    Returns a :class:`ManifestWriteOutcome` carrying the written paths,
    the new etag (sha256 over the just-written file bytes — subsequent
    ``apply_manifest`` invocations can pass it as their
    ``expected_etag``), and the CAS-time ``pre_write_state`` snapshot
    that any rollback of this write must restore (GTE-06 T4).

    Args:
        app_home: Solet's ``APP_HOME`` (``<profile_dir>``).
        new_manifest: Validated manifest dict carrying ``plugins`` and
            ``service_bindings`` keys.
        expected_etag: Etag the caller observed when they last read the
            current state. The write refuses with
            :class:`ManifestPreconditionFailedError` if the on-disk etag has
            shifted in the interim. Pass the etag from
            :func:`read_current_manifest_state`.
        profile_name: Goes into the manifest's ``profile_name`` field.
            v1 default is ``"local"`` because the local prototype is the
            first consumer; cloud will pass its own profile.

    Raises:
        ManifestPreconditionFailedError: when the CAS check fails.
        ManifestPartialWriteError: when an atomic write fails after the
            CAS check passed — carries the CAS-time snapshot so the
            caller restores the exact bytes the CAS validated.
    """
    pre_write_state = read_current_manifest_state(app_home)
    if pre_write_state.etag != expected_etag:
        raise ManifestPreconditionFailedError(
            expected_etag=expected_etag,
            current_etag=pre_write_state.etag,
        )

    new_plugins = _extract_new_plugins(new_manifest)
    new_bindings = _extract_new_bindings(new_manifest)

    bindings_path = app_home / _BINDINGS_RELATIVE_PATH
    manifest_path = app_home / _MANIFEST_RELATIVE_PATH
    bindings_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.parent.mkdir(parents=True, exist_ok=True)

    # WRITE ORDER: bindings first, manifest second (per advisor + module
    # docstring — fail-fast on inconsistent state is preferable to
    # silent-boot into old bindings).
    bindings_bytes = _render_bindings_bytes(new_bindings)
    manifest_bytes = _render_manifest_bytes(
        profile_name=profile_name,
        plugins=tuple(sorted(new_plugins)),
    )

    try:
        _atomic_write_bytes(bindings_path, bindings_bytes)
        _atomic_write_bytes(manifest_path, manifest_bytes)
    except OSError as exc:
        raise ManifestPartialWriteError(
            pre_write_state=pre_write_state, original=exc
        ) from exc

    new_etag = _compute_etag(manifest_bytes, bindings_bytes)
    return ManifestWriteOutcome(
        bindings_path=bindings_path,
        manifest_path=manifest_path,
        new_etag=new_etag,
        pre_write_state=pre_write_state,
    )


def _atomic_write_bytes(target: Path, payload: bytes) -> None:
    """Write ``payload`` to ``target`` via temp+rename on the same FS."""
    tmp = target.with_suffix(target.suffix + _TEMP_SUFFIX)
    tmp.write_bytes(payload)
    tmp.replace(target)


def _compute_etag(manifest_bytes: bytes, bindings_bytes: bytes) -> str:
    """Hash the (manifest, bindings) pair into a content-derived etag."""
    h = hashlib.sha256()
    h.update(b"manifest:")
    h.update(manifest_bytes)
    h.update(b"\nbindings:")
    h.update(bindings_bytes)
    return h.hexdigest()


def _extract_plugins_from_manifest_bytes(payload: bytes) -> tuple[str, ...]:
    """Parse the on-disk manifest's plugins list, tolerating absence."""
    if not payload:
        return ()
    raw = yaml.safe_load(payload)
    if not isinstance(raw, dict):
        return ()
    plugins = raw.get("plugins")
    if not isinstance(plugins, list):
        return ()
    return tuple(p for p in plugins if isinstance(p, str) and p)


def _extract_bindings_from_bytes(payload: bytes) -> dict[str, str]:
    """Parse the on-disk service_bindings.json into a flat dict."""
    if not payload:
        return {}
    raw = json.loads(payload)
    if not isinstance(raw, dict):
        return {}
    return {
        service: provider
        for service, provider in raw.items()
        if isinstance(service, str)
        and service
        and isinstance(provider, str)
        and provider
    }


def _extract_new_plugins(new_manifest: Mapping[str, Any]) -> set[str]:
    """Validate-and-extract the new manifest's ``plugins`` list."""
    plugins = new_manifest.get("plugins")
    if not isinstance(plugins, list) or not plugins:
        raise ValueError(
            "new_manifest['plugins'] must be a non-empty list of plugin names"
        )
    result: set[str] = set()
    for entry in plugins:
        if not isinstance(entry, str) or not entry:
            raise ValueError(
                "new_manifest['plugins'] entries must be non-empty strings"
            )
        result.add(entry)
    return result


def _extract_new_bindings(new_manifest: Mapping[str, Any]) -> dict[str, str]:
    """Validate-and-extract the new manifest's ``service_bindings``."""
    bindings = new_manifest.get("service_bindings")
    if not isinstance(bindings, dict):
        raise ValueError(
            "new_manifest['service_bindings'] must be a mapping of "
            "service_name to plugin_name"
        )
    result: dict[str, str] = {}
    for service, provider in bindings.items():
        if not isinstance(service, str) or not service:
            raise ValueError(
                "new_manifest['service_bindings'] keys must be non-empty strings"
            )
        if not isinstance(provider, str) or not provider:
            raise ValueError(
                f"new_manifest['service_bindings'][{service!r}] must be a "
                "non-empty plugin-name string"
            )
        result[service] = provider
    return result


def _render_bindings_bytes(bindings: Mapping[str, str]) -> bytes:
    """Render ``bindings`` as the JSON shape startup_sequence reads."""
    payload = dict(sorted(bindings.items()))
    return (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _render_manifest_bytes(
    *,
    profile_name: str,
    plugins: tuple[str, ...],
) -> bytes:
    """Render the manifest YAML the on-disk reader expects.

    The shape is what ``profile_manifest.load_manifest_plugin_set``
    parses. ``apply_manifest`` is the sole writer of this file since
    ``launch.py``'s retirement (F2 Choice Y, 2026-06-16): the on-disk
    manifest is the durable source of truth — startup gates plugin
    discovery on it at every boot, and blue-green deploys rewrite it
    under the CAS etag.
    """
    document = {
        "profile_name": profile_name,
        "plugins": list(plugins),
    }
    header = (
        "# Written by lifecycle_management_service::apply_manifest.\n"
        "# This file is the durable plugin-manifest source of truth: startup\n"
        "# gates plugin discovery on it at every boot, and blue-green deploys\n"
        "# rewrite it under a CAS etag. Change it via apply_manifest, not by hand.\n"
    )
    return (
        header
        + yaml.safe_dump(document, sort_keys=False, default_flow_style=False)
    ).encode("utf-8")
