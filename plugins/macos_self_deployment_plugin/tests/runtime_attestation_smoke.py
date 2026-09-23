#!/usr/bin/env python3
"""T1 runtime-attestation smoke; uses only a temporary immutable release."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import types
from hashlib import sha256
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[3]
for _path in (_ROOT / "ananta" / "src", _ROOT / "plugins" / "macos_self_deployment_plugin" / "src"):
    sys.path.insert(0, str(_path))

from macos_self_deployment_plugin.plugin import MacosSelfDeploymentPlugin  # noqa: E402
from macos_self_deployment_plugin.surface_digest import (  # noqa: E402
    reconciliation_surface_digest,
    release_surface_digest,
    surface_digest,
)


class _Manager:
    def __init__(self, root: Path) -> None:
        self.releases_root = root
        self.current_release = "rel-smoke"


class _Router:
    def status(self) -> dict[str, str]:
        return {"active_instance_id": "instance-smoke", "active_color": "blue"}


def _surface_digest_rejected(root: Path, relative_root: Path) -> bool:
    try:
        surface_digest(root, (relative_root,))
    except ValueError:
        return True
    return False


def _reconciliation_surface_digest_rejected(root: Path) -> bool:
    try:
        reconciliation_surface_digest(root)
    except ValueError:
        return True
    return False


def _release_surface_digest_rejected(root: Path) -> bool:
    try:
        release_surface_digest(root)
    except ValueError:
        return True
    return False


def _write_attestation_tree(root: Path, plugin_layout: str) -> None:
    for relative, content in (
        ("ananta/a.py", "a = 1\n"),
        ("solet_cli/a.py", "a = 1\n"),
        ("solet_setup_contracts/src/a.py", "a = 1\n"),
    ):
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    plugins_root = root / "plugins"
    plugins_root.mkdir()
    if plugin_layout == "regular":
        source = plugins_root / "example/src/example.py"
        source.parent.mkdir(parents=True)
        source.write_text("value = 1\n", encoding="utf-8")
    elif plugin_layout == "existing-parent-link":
        source = root / "real/src/example.py"
        source.parent.mkdir(parents=True)
        source.write_text("value = 1\n", encoding="utf-8")
        (plugins_root / "example").symlink_to(root / "real", target_is_directory=True)
    elif plugin_layout == "dangling-parent-link":
        (plugins_root / "example").symlink_to(root / "missing-plugin", target_is_directory=True)
    elif plugin_layout == "dangling-src-link":
        plugin_root = plugins_root / "example"
        plugin_root.mkdir()
        (plugin_root / "src").symlink_to(root / "missing-src", target_is_directory=True)
    else:
        raise ValueError(f"unsupported plugin layout: {plugin_layout}")


def _surface_digest_checks() -> list[tuple[bool, str]]:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        positive = root / "positive"
        positive.mkdir()
        source = positive / "module.py"
        source.write_text("answer = 42\n", encoding="utf-8")
        source.chmod(0o644)
        expected_payload = [
            {
                "mode": 0o644,
                "relative_path": "positive/module.py",
                "sha256": sha256(b"answer = 42\n").hexdigest(),
            }
        ]
        encoded_payload = json.dumps(
            expected_payload,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        expected_digest = f"sha256:{sha256(encoded_payload).hexdigest()}"
        linked_root = root / "linked-root"
        linked_root.symlink_to(positive, target_is_directory=True)
        intermediate = root / "intermediate"
        intermediate.mkdir()
        linked_directory = intermediate / "linked-directory"
        linked_directory.symlink_to(root / "directory-target", target_is_directory=True)
        (root / "directory-target").mkdir()
        directory_link_tree = root / "directory-link-tree"
        directory_link_tree.mkdir()
        (directory_link_tree / "linked-directory").symlink_to(root / "directory-target", target_is_directory=True)
        file_link_tree = root / "file-link-tree"
        file_link_tree.mkdir()
        linked_file = file_link_tree / "linked-file.py"
        linked_file.symlink_to(source)
        dangling_tree = root / "dangling-tree"
        dangling_tree.mkdir()
        dangling_link = dangling_tree / "dangling.py"
        dangling_link.symlink_to(root / "missing.py")
        fifo_tree = root / "fifo-tree"
        fifo_tree.mkdir()
        fifo = fifo_tree / "surface.fifo"
        os.mkfifo(fifo)
        _write_attestation_tree(root, "existing-parent-link")
        ordinary_root = root / "ordinary"
        ordinary_copy = root / "ordinary-copy"
        _write_attestation_tree(ordinary_root, "regular")
        _write_attestation_tree(ordinary_copy, "regular")
        dangling_parent_root = root / "dangling-parent"
        dangling_src_root = root / "dangling-src"
        _write_attestation_tree(dangling_parent_root, "dangling-parent-link")
        _write_attestation_tree(dangling_src_root, "dangling-src-link")
        ordinary_surface = surface_digest(ordinary_root, (Path("plugins/example/src"),))
        ordinary_reconciliation = reconciliation_surface_digest(ordinary_root)
        ordinary_release = release_surface_digest(ordinary_root)
        return [
            (
                surface_digest(root, (Path("positive"),)) == expected_digest,
                "regular surface digest remains canonical",
            ),
            (
                _surface_digest_rejected(root, Path("linked-root")),
                "linked root directory is rejected",
            ),
            (
                _surface_digest_rejected(linked_root, Path(".")),
                "linked declared code root is rejected",
            ),
            (
                _surface_digest_rejected(root, Path("intermediate")),
                "linked intermediate directory is rejected",
            ),
            (
                _surface_digest_rejected(root, Path("plugins/example/src")),
                "linked relative-root ancestor is rejected",
            ),
            (
                _reconciliation_surface_digest_rejected(root),
                "reconciliation rejects linked relative-root ancestor",
            ),
            (
                _release_surface_digest_rejected(root),
                "whole-release linked relative-root ancestor remains rejected",
            ),
            (
                _surface_digest_rejected(dangling_parent_root, Path("plugins/example/src")),
                "direct digest rejects dangling plugin-root link",
            ),
            (
                _reconciliation_surface_digest_rejected(dangling_parent_root),
                "reconciliation rejects dangling plugin-root link",
            ),
            (
                _release_surface_digest_rejected(dangling_parent_root),
                "whole-release rejects dangling plugin-root link",
            ),
            (
                _surface_digest_rejected(dangling_src_root, Path("plugins/example/src")),
                "direct digest rejects dangling selected-src link",
            ),
            (
                _reconciliation_surface_digest_rejected(dangling_src_root),
                "reconciliation rejects dangling selected-src link",
            ),
            (
                _release_surface_digest_rejected(dangling_src_root),
                "whole-release rejects dangling selected-src link",
            ),
            (
                ordinary_surface == surface_digest(ordinary_copy, (Path("plugins/example/src"),)),
                "ordinary direct surface digest is unchanged",
            ),
            (
                ordinary_reconciliation == reconciliation_surface_digest(ordinary_copy),
                "ordinary reconciliation digest is unchanged",
            ),
            (
                ordinary_release == release_surface_digest(ordinary_copy),
                "ordinary release digest is unchanged",
            ),
            (
                _surface_digest_rejected(root, Path("directory-link-tree")),
                "directory link is rejected",
            ),
            (
                _surface_digest_rejected(root, Path("file-link-tree")),
                "file link is rejected",
            ),
            (
                _surface_digest_rejected(root, Path("dangling-tree")),
                "dangling link is rejected",
            ),
            (
                _surface_digest_rejected(root, Path("fifo-tree")),
                "non-regular entry is rejected",
            ),
        ]


def main() -> int:
    with tempfile.TemporaryDirectory() as directory:
        releases = Path(directory)
        code = releases / "rel-smoke" / "code"
        for relative, content in (
            ("ananta/a.py", "a = 1\n"),
            ("plugins/example/src/example/loaded.py", "value = 1\n"),
            ("plugins/example/src/example/unimported.py", "value = 2\n"),
            ("solet_cli/a.py", "a = 1\n"),
            ("solet_setup_contracts/src/a.py", "a = 1\n"),
        ):
            target = code / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        version = releases / "rel-smoke" / "VERSION"
        version.write_text(json.dumps({"manifest_etag": "etag-smoke"}), encoding="utf-8")
        loaded_path = code / "plugins/example/src/example/loaded.py"
        loaded = types.ModuleType("example.loaded")
        loaded.__file__ = str(loaded_path)
        sys.modules["example.loaded"] = loaded
        original_release = os.environ.get("SOLET_RELEASE_ID")
        os.environ["SOLET_RELEASE_ID"] = "rel-smoke"
        try:
            plugin = MacosSelfDeploymentPlugin()
            plugin._release_manager = _Manager(releases)  # noqa: SLF001
            plugin._router_client = _Router()  # noqa: SLF001
            plugin._self_instance_id = "instance-smoke"  # noqa: SLF001
            plugin._self_color = "blue"  # noqa: SLF001
            result = plugin.attest_runtime_code(
                reconciliation_id="rec-smoke",
                verification_modules=("example.loaded", "example.unimported"),
            )
            checks = [
                (result["served_by_self"] is True, "router-served identity"),
                (result["release_id"] == "rel-smoke", "immutable release identity"),
                (str(result["source_surface_sha256"]).startswith("sha256:"), "source digest"),
                (str(result["release_surface_sha256"]).startswith("sha256:"), "release digest"),
                (result["modules"][0]["loaded"] is True, "loaded module observed"),
                (result["modules"][1]["loaded"] is False, "unimported module not imported"),
            ]
            action_result = plugin.attest_runtime_code_action(
                {
                    "reconciliation_id": "rec-smoke",
                    "verification_modules": ["example.loaded"],
                },
                {},
            )
            checks.extend(
                [
                    (
                        action_result["action_status"] == "completed",
                        "platform-process wrapper resolves and runs",
                    ),
                    (
                        action_result["data"]["served_by_self"] is True,
                        "platform-process wrapper returns served proof",
                    ),
                    (
                        "attest_runtime_code" in plugin.get_edge_process_definitions(),
                        "edge process definition registered",
                    ),
                ]
            )
            plugin._router_client = type("_WrongRouter", (), {"status": lambda self: {"active_instance_id": "other", "active_color": "green"}})()  # noqa: E501, SLF001
            checks.append((plugin.attest_runtime_code(reconciliation_id="", verification_modules=())["served_by_self"] is False, "non-self route is exposed"))
        finally:
            sys.modules.pop("example.loaded", None)
            if original_release is None:
                os.environ.pop("SOLET_RELEASE_ID", None)
            else:
                os.environ["SOLET_RELEASE_ID"] = original_release
    checks.extend(_surface_digest_checks())
    for passed, label in checks:
        print(f"{'PASS' if passed else 'FAIL'} {label}")
    return 0 if all(passed for passed, _ in checks) else 1


if __name__ == "__main__":
    raise SystemExit(main())
