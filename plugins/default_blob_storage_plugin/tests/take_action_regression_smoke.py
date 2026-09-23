#!/usr/bin/env python3
"""Regression smoke for Task #29 — DefaultBlobStoragePlugin.take_action restoration.

Run:

    .venv/bin/python3 plugins/default_blob_storage_plugin/tests/take_action_regression_smoke.py

Context
-------

The §9.H plugin god-class decomposition (commit ``aba7c7dc``) removed the
public ``take_action`` method on :class:`DefaultBlobStoragePlugin`,
folding its body into the framework-required private ``_execute_action``
hook. That broke every caller that goes through ``BlobStorageService``
(``ananta/src/ananta/services/blob_storage_service/__init__.py``), which
publishes ``BlobStoragePluginProtocol.take_action`` and calls
``plugin.take_action(params=..., state={})`` at line 180. The protocol
declared the method but the concrete plugin no longer implemented it
→ ``AttributeError`` → ``blob_storage_service.action_failed`` →
upstream callers (including ``SessionLedgerBlobAdapter._store``) saw
``BlobAdapterError`` and silently skipped every >4KB event.

Symptom in the wild (2026-05-31): every Architect / peer report >4KB
in today's LLM session ledger ingest was dropped; ``today_events`` count
in ``session_ledger__event`` was 15 with zero ``content_blob_id``
populated. Claude-C's pass-2 resilience kept the ledger alive past
the per-session failure but did NOT fix the upstream contract bug —
that's THIS regression.

What this smoke verifies
------------------------

1. ``take_action`` is a public, callable attribute on the plugin (the
   contract that ``BlobStoragePluginProtocol`` publishes; the
   ``hasattr`` check that ``BlobStorageService`` implicitly relies on).
2. Calling ``plugin.take_action(params={"action": {"name": "store_file"}, ...}, state={})``
   with the exact param shape ``BlobStorageService._execute_blob_storage_action``
   constructs at line 177 does NOT raise ``AttributeError`` and dispatches
   to the typed ``store_blob`` path under the hood.
3. The end-to-end path through ``BlobStorageService.store_blob`` with
   >4KB content (the exact case the ledger importer hit today) returns
   ``action_status='completed'`` and surfaces a non-empty ``blob_id``.
4. ``_execute_action`` (the framework hook) still works as a thin
   delegate — it's how ``PluginBase.execute(...)`` reaches us.

The smoke uses an in-memory stub provider so it doesn't need Postgres or
a real filesystem. The ``DefaultBlobStoragePlugin``'s normal provider
construction is bypassed via direct attribute injection; what we're
proving is the dispatcher contract, not the storage backend.
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))
sys.path.insert(
    0,
    str(REPO_ROOT / "plugins" / "default_blob_storage_plugin" / "src"),
)

from ananta.services.blob_storage_service import BlobStorageService  # noqa: E402

from default_blob_storage_plugin.plugin import DefaultBlobStoragePlugin  # noqa: E402

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


# ─── Fixtures ────────────────────────────────────────────────────────────────


class _StubProvider:
    """Minimal stand-in for FilesystemProvider that records calls.

    Only implements the methods :meth:`DefaultBlobStoragePlugin._dispatch_action`
    actually touches for ``store_blob``. We're proving the dispatcher contract,
    not the underlying storage; an in-memory dict suffices.
    """

    def __init__(self) -> None:
        self._counter = 0
        self.stored: dict[str, dict[str, Any]] = {}

    def store_blob(
        self,
        namespace: str,
        content: bytes,
        metadata: dict[str, object],
    ) -> dict[str, Any]:
        self._counter += 1
        blob_id = f"blob_stub_{self._counter:04d}"
        self.stored[blob_id] = {
            "namespace": namespace,
            "content_len": len(content),
            "metadata": dict(metadata),
        }
        return {
            "action_status": "completed",
            "data": {"blob_id": blob_id},
            "actions": [],
            "error": None,
            "timestamp": datetime.now(UTC).isoformat(),
        }

    def retrieve_blob(self, blob_id: str) -> dict[str, Any]:  # pragma: no cover (not exercised)
        _ = blob_id
        return {"action_status": "error", "data": {}, "actions": [], "error": "stub", "timestamp": ""}

    def set_state_service(self, _state_service: object) -> None:
        return None


class _StubPluginManager:
    """Minimal PluginManagerProtocol implementation for BlobStorageService."""

    def __init__(self, plugin: DefaultBlobStoragePlugin) -> None:
        self._plugin = plugin

    def get_plugin(self, plugin_name: str) -> object:
        _ = plugin_name
        return self._plugin


def _build_plugin_with_stub_provider() -> DefaultBlobStoragePlugin:
    """Bypass the heavy FilesystemProvider construction."""
    plugin = DefaultBlobStoragePlugin()
    plugin.provider = cast(Any, _StubProvider())
    plugin.set_ready()  # mark plugin as ready so BlobStorageService.is_ready() passes
    return plugin


def _build_oversize_payload() -> bytes:
    """A >4KB payload — the exact case the ledger importer hit today.

    The CONTENT_INLINE_TEXT_MAX_BYTES threshold lives at
    ``ananta/src/ananta/llm/session_ledger/schema.py``; the ledger
    importer staged anything above it through ``store_blob``. Six KB is
    comfortably over the threshold and small enough to keep the smoke
    in-memory.
    """
    return ("x" * 6144).encode("utf-8")


# ─── Cases ───────────────────────────────────────────────────────────────────


def test_take_action_is_public_callable() -> None:
    """Contract #1: BlobStoragePluginProtocol declares it; the plugin must publish it."""
    plugin = _build_plugin_with_stub_provider()
    _check(
        hasattr(plugin, "take_action"),
        "DefaultBlobStoragePlugin has a public 'take_action' attribute",
    )
    _check(
        callable(getattr(plugin, "take_action", None)),
        "'take_action' is a callable on the plugin instance",
    )


def test_take_action_dispatch_matches_service_call_shape() -> None:
    """Contract #2: the exact param shape BlobStorageService constructs at line 177
    must dispatch without raising AttributeError and return an ActionResult-shaped
    dict carrying ``blob_id`` on success."""
    plugin = _build_plugin_with_stub_provider()
    content = _build_oversize_payload()
    action_params = {
        "action": {"name": "store_file"},
        "namespace": "session_ledger",
        "content": content,
        "metadata": {"kind": "event_content_text", "session_id": "les-test-001"},
    }

    raised_attr_error = False
    try:
        result = plugin.take_action(params=action_params, state={})
    except AttributeError:
        raised_attr_error = True
        result = {}

    _check(
        not raised_attr_error,
        "plugin.take_action(...) does NOT raise AttributeError (Task #29 root regression)",
    )
    _check(
        result.get("action_status") == "completed",
        f"action_status='completed' (got {result.get('action_status')!r})",
    )
    data = result.get("data", {})
    _check(
        isinstance(data, dict) and isinstance(data.get("blob_id"), str) and data["blob_id"],
        f"result.data carries a non-empty blob_id string (got {data!r})",
    )


def test_execute_action_still_works_via_framework_hook() -> None:
    """Contract #4: PluginBase routes through _execute_action; the new shape must keep working.

    The framework hook signature is (action_params, state, APP_HOME, plugin_config).
    """
    plugin = _build_plugin_with_stub_provider()
    action_params = {
        "action": {"name": "store_file"},
        "namespace": "session_ledger",
        "content": b"small payload",
        "metadata": {},
    }
    result = plugin._execute_action(action_params, {}, "/tmp/fake-app-home", {})  # noqa: SLF001
    _check(
        result.get("action_status") == "completed",
        f"_execute_action delegates cleanly via take_action (got {result.get('action_status')!r})",
    )


def test_end_to_end_through_blob_storage_service() -> None:
    """Contract #3: the exact upstream path the ledger importer hit today."""
    plugin = _build_plugin_with_stub_provider()
    plugin_manager = _StubPluginManager(plugin)
    service = BlobStorageService(
        plugin_manager=cast(Any, plugin_manager),
        blob_storage_plugin_name="default_blob_storage_plugin",
        app_home="/tmp/fake-app-home",
    )
    # Mark service as out of bootstrap mode so the plugin path is exercised.
    service.bootstrap_mode = False

    large_content = _build_oversize_payload()
    metadata: dict[str, object] = {
        "kind": "event_content_text",
        "session_id": "les-test-002",
        "external_session_id": "ext-001",
        "sequence": 42,
        "mime_type": "text/plain",
    }
    result = service.store_blob(
        namespace="session_ledger",
        content=large_content,
        metadata=metadata,
    )
    _check(
        result.get("action_status") == "completed",
        f"BlobStorageService.store_blob → completed for >4KB payload (got {result.get('action_status')!r})",
    )
    data = result.get("data") or {}
    blob_id = data.get("blob_id", "")
    _check(
        isinstance(blob_id, str) and bool(blob_id),
        f"end-to-end store returns a blob_id (got {data!r})",
    )
    # Surface error details if the test failed — these are exactly the strings
    # that surfaced in the 2026-05-31 ledger log lines before the fix.
    if result.get("action_status") != "completed":
        err = result.get("error")
        print(f"    error envelope: {err!r}")


def main() -> int:
    print("=== take_action_regression_smoke (Task #29) ===")
    test_take_action_is_public_callable()
    test_take_action_dispatch_matches_service_call_shape()
    test_execute_action_still_works_via_framework_hook()
    test_end_to_end_through_blob_storage_service()
    print(f"\n{_passed} passed, {len(_failed)} failed")
    if _failed:
        for label in _failed:
            print(f"  FAILED: {label}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
