"""Constructed on-disk fixtures for the release-identity smokes (design §7.4).

No real keg, no network, no running solet: a throwaway git repository stands
in for the seed checkout, a directory of small files for the installed
``solet_manager`` package, and hand-built JSON for the manifest, seed lock
and install-source receipt.  Every declared digest is computed from the
fixture bytes so a green here means the comparator hashed what it claims.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(_ROOT / "solet_cli" / "src"), str(_ROOT / "solet_setup_contracts" / "src")]

SOURCE_COMMIT = "5" * 40
OTHER_COMMIT = "6" * 40
SEED_ID = "11111111-2222-4333-8444-555555555555"
ORIGIN_ID = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
MANAGER_PREFIX = "solet_cli/src/solet_manager/"

_GIT_ENV = {
    "GIT_AUTHOR_NAME": "fixture",
    "GIT_AUTHOR_EMAIL": "fixture@example.invalid",
    "GIT_COMMITTER_NAME": "fixture",
    "GIT_COMMITTER_EMAIL": "fixture@example.invalid",
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_CONFIG_NOSYSTEM": "1",
    "LC_ALL": "C",
    "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
    "HOME": os.environ.get("HOME", "/"),
}


def git(repo: Path, *arguments: str) -> str:
    completed = subprocess.run(("git", "-C", str(repo), *arguments), check=True, capture_output=True, text=True, env=_GIT_ENV)
    return completed.stdout.strip()


def build_seed_checkout(root: Path, plugins: tuple[str, ...] = ("alpha", "beta")) -> dict[str, str]:
    """A committed checkout with one file per plugin; returns head, tree and per-plugin subtree hashes."""

    root.mkdir(parents=True, exist_ok=True)
    git(root, "init", "-q", "-b", "main")
    for name in plugins:
        plugin_dir = root / "plugins" / name
        plugin_dir.mkdir(parents=True, exist_ok=True)
        (plugin_dir / "plugin.py").write_text(f"NAME = {name!r}\n", encoding="utf-8")
    (root / "README.md").write_text("fixture seed\n", encoding="utf-8")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "fixture seed")
    identity = {"commit": git(root, "rev-parse", "HEAD"), "tree_hash": git(root, "rev-parse", "HEAD^{tree}")}
    for name in plugins:
        identity[f"plugin:{name}"] = git(root, "rev-parse", f"HEAD:plugins/{name}")
    return identity


def build_package(root: Path, files: dict[str, str] | None = None) -> dict[str, str]:
    """An installed-package look-alike; returns the manifest-keyed digests of its files."""

    contents = {"__init__.py": "", "models.py": "MANAGER_VERSION = '0.1.0'\n", "released_metadata/contract.v1.json": "{}\n"} if files is None else files
    root.mkdir(parents=True, exist_ok=True)
    digests: dict[str, str] = {}
    for relative, text in contents.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        digests[MANAGER_PREFIX + relative] = "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()
    cache = root / "__pycache__"
    cache.mkdir(exist_ok=True)
    (cache / "models.cpython-313.pyc").write_bytes(b"\x00bytecode")
    return digests


def manifest(
    *,
    seed: dict[str, str],
    file_digests: dict[str, str],
    source_commit: str = SOURCE_COMMIT,
    seed_source_commit: str = SOURCE_COMMIT,
    allow_manager_seed_skew: str | None = None,
    surface_sha256: str | None = "sha256:" + "7" * 64,
    components: bool = True,
) -> dict[str, Any]:
    """A schema-v1 release manifest (§7.1) declaring exactly the fixture's identities."""

    provenance = {"schema_version": 1, "seed_id": SEED_ID, "manifest_sha256": "8" * 64, "source_commit": seed_source_commit}
    plugin_components = [
        {"component": key, "subtree_hash": value, "signature_id": None, "signature_key": None}
        for key, value in seed.items()
        if key.startswith("plugin:")
    ]
    return {
        "schema_version": 1,
        "release_label": "r44",
        "manager_release_tag": "manager-v0.1.0-r44",
        "seed": {
            "repository": "https://github.com/solet-public/macos-bizops.git",
            "release_tag": "release-2026-09-19-fixture",
            "commit": seed["commit"],
            "tree_hash": seed["tree_hash"],
            "seed_id": SEED_ID,
            "manifest_sha256": "8" * 64,
            "source_commit": seed_source_commit,
            "profile": "macos-bizops",
            "channel_id": "stable",
            "provenance": provenance,
        },
        "components": [*plugin_components, {"component": "platform_base", "subtree_hash": None, "signature_id": None, "signature_key": None}] if components else None,
        "bundle_verdict": None,
        "guest_validation": None,
        "manager": {
            "source_repository": "https://github.com/solet-public/manager-source.git",
            "source_commit": source_commit,
            "source_tree_hash": "9" * 40,
            "version": "0.1.0",
            "payload_asset": {"name": "solet-0.1.0-r44.tar.gz", "url": "file:///fixture", "sha256": "a" * 64},
            "file_digests": dict(file_digests),
            "contract_digest": "sha256:" + "b" * 64,
            "transition_bundle_digest": "sha256:" + "c" * 64,
            "allow_manager_seed_skew": allow_manager_seed_skew,
        },
        "tap": None,
        "surface_digests": None if surface_sha256 is None else {"release_surface_sha256": surface_sha256, "reconciliation_surface_sha256": "sha256:" + "d" * 64},
        "produced_by": None,
        "factory_signature": None,
    }


def write_json(path: Path, value: dict[str, Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def write_receipt(path: Path, source_commit: str = SOURCE_COMMIT, mode: str = "release") -> Path:
    return write_json(path, {"schema_version": 1, "mode": mode, "source_commit": source_commit})


def seed_lock_v3(seed: dict[str, str], *, source_commit: str = SOURCE_COMMIT) -> dict[str, Any]:
    """A closed v3 seed lock whose identity is the fixture checkout's."""

    return {
        "schema_version": 3,
        "channel_id": "stable",
        "repository": "https://github.com/solet-public/macos-bizops.git",
        "release_tag": "release-2026-09-19-fixture",
        "commit": seed["commit"],
        "tree_hash": seed["tree_hash"],
        "archive_sha256": "e" * 64,
        "profile": "macos-bizops",
        "provenance": {
            "schema_version": 1,
            "provenance_sha256": "f" * 64,
            "seed_id": SEED_ID,
            "origin_id": ORIGIN_ID,
            "manifest_sha256": "8" * 64,
            "bundle_name": "macos-bizops",
            "platform": "local",
            "source_commit": source_commit,
            "source_date": "2026-09-19T00:00:00Z",
        },
        "existing_install_contract": {"flow_id": "existing-install", "flow_schema_version": 1, "bundle_digest": "sha256:" + "c" * 64},
        "allowed_repository_migrations": [],
    }


def fake_bridge(target: Path, payload: dict[str, Any] | None, *, exit_code: int = 0) -> Path:
    """A stand-in ``<target>/.venv/bin/solet-bridge`` that answers with ``payload`` (or fails)."""

    bridge = target / ".venv" / "bin" / "solet-bridge"
    bridge.parent.mkdir(parents=True, exist_ok=True)
    body = json.dumps({"result": {"data": payload, "success": True}} if payload is not None else {})
    bridge.write_text(f"#!/bin/sh\nif [ {exit_code} -ne 0 ]; then echo 'no bridge port file' >&2; exit {exit_code}; fi\ncat <<'EOF'\n{body}\nEOF\n", encoding="utf-8")
    bridge.chmod(0o755)
    return bridge
