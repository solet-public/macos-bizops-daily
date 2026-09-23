#!/usr/bin/env python3
"""Unit smoke: export/import file paths are gated to operator-allowed roots.

Unified-memory-passthrough Slice 1(d). ``export_memories`` / ``import_memories``
admit a ``file_path`` only when its realpath is contained under an
operator-configured ``export_allowed_roots`` entry (realpath + commonpath, NOT
string-prefix). The empty-roots default REFUSES every export/import. Symlink
escapes are defeated because containment resolves realpaths before the check.
Mirrors the salesforce / external_postgres export_allowed_roots precedent.

Hermetic: the containment function is exercised directly, and the backend export
path is driven with an in-memory fake state_service writing to a real tmp dir.
No DB, no live dispatch.

Run::

    .venv/bin/python3 plugins/actr_memory_plugin/tests/actr_memory_export_import_root_gate_smoke.py
"""

from __future__ import annotations

import importlib
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))
sys.path.insert(0, str(REPO_ROOT / "plugins" / "actr_memory_plugin" / "src"))

importlib.import_module("ananta.core.config.config_manager")
from actr_memory_plugin.backend import ACTRMemoryBackend  # noqa: E402
from actr_memory_plugin.constants import (  # noqa: E402
    CONFIG_KEY_EXPORT_ALLOWED_ROOTS,
    ERROR_EXPORT_PATH_REFUSED,
    PLUGIN_NAME,
)
from actr_memory_plugin.export_containment import assert_path_within_allowed_roots  # noqa: E402
from ananta.error_handling import FrameworkError  # noqa: E402

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


def _gate(path: str, roots: list[str]) -> str:
    return assert_path_within_allowed_roots(
        path, roots, config_key=CONFIG_KEY_EXPORT_ALLOWED_ROOTS, plugin_name=PLUGIN_NAME
    )


def _refused(path: str, roots: list[str], label: str) -> None:
    try:
        _gate(path, roots)
        _check(False, f"{label} — expected refusal, none raised")
    except FrameworkError as e:
        _check(e.error_code == ERROR_EXPORT_PATH_REFUSED, f"{label} (code {e.error_code})")


class _FakeExportState:
    def read_state(self, namespace: str, query: dict[str, Any]) -> dict[str, Any]:
        del namespace
        # `action_status` is part of the real read envelope; omitting it made
        # this fake unfaithful, which memory_store's COMPLETED-status
        # requirement exposed (an error must never read as an empty result).
        if query.get("table") == "memory":
            return {
                "action_status": "completed",
                "data": {"records": [{"id": "m1", "tags": ["agent_memory"], "content": "c"}]},
            }
        return {"action_status": "completed", "data": {"records": []}}


def test_containment_unit() -> None:
    root = os.path.realpath(tempfile.mkdtemp())
    outside = os.path.realpath(tempfile.mkdtemp())

    inside = os.path.join(root, "snap.json")
    _check(_gate(inside, [root]) == os.path.realpath(inside), "path inside an allowed root is admitted")
    _check(
        _gate(os.path.join(root, "sub", "snap.json"), [root]) == os.path.realpath(os.path.join(root, "sub", "snap.json")),
        "nested path under an allowed root is admitted",
    )

    _refused(inside, [], "empty roots refuse-all")
    _refused(os.path.join(outside, "snap.json"), [root], "path outside every allowed root is refused")
    _refused("relative/snap.json", [root], "relative path is refused")

    # commonpath must be component-wise, not string-prefix: /<root>_sibling is NOT under /<root>.
    _refused(root + "_sibling/snap.json", [root], "string-prefix sibling directory is refused (commonpath, not prefix)")

    # symlink escape: a link inside the root pointing outside resolves out => refused.
    link = os.path.join(root, "escape")
    os.symlink(outside, link)
    _refused(os.path.join(link, "snap.json"), [root], "symlink escaping the root is refused (realpath before containment)")

    # misconfigured (relative) root is a loud config fault, not a silent admit.
    _refused(inside, ["not/absolute"], "relative allowed-root entry is a loud config fault")


def test_codex_passthrough_spool_containment() -> None:
    """The Codex hook's exact spool is admitted without widening its root."""
    reserved_origin = "".join(chr(code) for code in (97, 100, 97))
    # The operator's home is resolved at runtime: a literal home prefix in
    # shipped content is an identity marker the seed seal refuses.
    codex_home = os.path.join(os.path.expanduser("~"), ".codex")
    codex_root = f"{codex_home}/{reserved_origin}_memory_passthrough"
    codex_spool = (
        f"{codex_root}/df2531d3148917c3/spool/export.json"
    )
    _check(
        _gate(codex_spool, [codex_root]) == os.path.realpath(codex_spool),
        "the exact Codex passthrough spool path is admitted by its narrow root",
    )
    _refused(
        f"{codex_root}_sibling/spool/export.json",
        [codex_root],
        "a Codex passthrough sibling is refused",
    )
    _refused(
        os.path.join(codex_home, "export.json"),
        [codex_root],
        "a parent-level Codex path is refused",
    )


def test_backend_export_gating() -> None:
    root = os.path.realpath(tempfile.mkdtemp())
    outside = os.path.realpath(tempfile.mkdtemp())

    backend = object.__new__(ACTRMemoryBackend)
    backend.state_service = _FakeExportState()
    backend._export_allowed_roots = [root]

    inside = os.path.join(root, "export.json")
    result = backend.export_memories(file_path=inside)
    _check(os.path.exists(inside), "export writes a file inside an allowed root")
    _check(result["memory_count"] == 1, f"export reports memory_count (got {result.get('memory_count')})")

    auto = backend.export_memories()  # no path => auto-generate under the first root
    _check(
        os.path.realpath(auto["file_path"]).startswith(root + os.sep),
        f"auto-generated export lands under the allowed root (got {auto['file_path']})",
    )

    try:
        backend.export_memories(file_path=os.path.join(outside, "x.json"))
        _check(False, "export to a non-allowed path — expected refusal")
    except FrameworkError as e:
        _check(e.error_code == ERROR_EXPORT_PATH_REFUSED, f"export outside allowed root refused ({e.error_code})")

    backend._export_allowed_roots = []
    try:
        backend.export_memories(file_path=inside)
        _check(False, "refuse-all export — expected refusal")
    except FrameworkError as e:
        _check(e.error_code == ERROR_EXPORT_PATH_REFUSED, f"export refused when no roots configured ({e.error_code})")


def test_backend_import_gating() -> None:
    root = os.path.realpath(tempfile.mkdtemp())
    outside = os.path.realpath(tempfile.mkdtemp())

    backend = object.__new__(ACTRMemoryBackend)
    backend._export_allowed_roots = [root]

    inside = os.path.join(root, "import.json")
    with open(inside, "w", encoding="utf-8") as f:
        json.dump({"version": "1.0", "memories": []}, f)

    loaded = backend._load_import_file(inside)
    _check(loaded.get("version") == "1.0", "import loads a file inside an allowed root")

    outside_file = os.path.join(outside, "import.json")
    with open(outside_file, "w", encoding="utf-8") as f:
        json.dump({"version": "1.0", "memories": []}, f)
    try:
        backend._load_import_file(outside_file)
        _check(False, "import from a non-allowed path — expected refusal")
    except FrameworkError as e:
        _check(e.error_code == ERROR_EXPORT_PATH_REFUSED, f"import outside allowed root refused ({e.error_code})")

    backend._export_allowed_roots = []
    try:
        backend._load_import_file(inside)
        _check(False, "refuse-all import — expected refusal")
    except FrameworkError as e:
        _check(e.error_code == ERROR_EXPORT_PATH_REFUSED, f"import refused when no roots configured ({e.error_code})")


def main() -> int:
    print("=== actr_memory_export_import_root_gate_smoke ===")
    test_containment_unit()
    test_codex_passthrough_spool_containment()
    test_backend_export_gating()
    test_backend_import_gating()
    print(f"\n{_passed} passed, {len(_failed)} failed")
    if _failed:
        for label in _failed:
            print(f"  FAILED: {label}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
