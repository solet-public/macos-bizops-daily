"""iss_92735a39 (r66): every plist reader the Manager and the seed own blocks on an unparseable plist, none raises.

``plistlib.loads`` raises more than ``ValueError`` and ``InvalidFileException``: expat's ``ExpatError`` (truncated or mismatched XML), ``ValueError`` (a
bad ``<integer>`` or ``<real>``, a key with no value, invalid UTF-8), ``AttributeError`` (a bad ``<date>``), ``LookupError`` (an unknown encoding) and
``RecursionError`` (deep nesting).  Hermetic, no fixture builder.

- every corruption in the corpus is read as "not a plist" by ``launch_topology.parse_plist`` and so by ``plist_label``, ``plist_program_arguments``
  and ``derive_launch_topology`` (unsupported topology), by the LM Studio login check (``None``, undetermined) and by the doctor's launch-vector check
  (``False``); a valid plist reads as before (the controls);
- the Manager's ``PLIST_PARSE_ERRORS`` is exactly the seed's ``target_reconciliation.PLIST_PARSE_ERRORS`` (the Manager never imports seed code, so the
  twin is held equal here);
- the class guard: every ``plistlib.load``/``loads`` call in the Manager and the seed's migration code sits inside a ``try`` whose handler names
  ``PLIST_PARSE_ERRORS`` or catches ``Exception``, so a reader added later with a partial tuple fails this smoke. The guard walks every enclosing ``try``, so a read
  nested under an unrelated broad ``except Exception`` further out also counts as guarded.
"""

from __future__ import annotations

import ast
import plistlib
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

_ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(_ROOT / "solet_cli" / "src"), str(_ROOT / "solet_setup_contracts" / "src"), str(_ROOT / "plugins" / "github_midwife_plugin" / "src")]

from github_midwife_plugin import target_reconciliation  # noqa: E402
from solet_manager import launch_topology  # noqa: E402
from solet_manager.existing_install_doctor_service_checks import _target_launch_vector  # noqa: E402
from solet_manager.lm_studio_diagnostics import _login_valid  # noqa: E402

_CHECKS = 0
_LABEL = "local.solet.lm-studio"


def _check(condition: object, label: str) -> None:
    global _CHECKS
    _CHECKS += 1
    if not condition:
        raise AssertionError(label)


def _valid_xml() -> bytes:
    return plistlib.dumps(
        {"Label": "local.solet.demo", "ProgramArguments": ["/opt/demo/.venv/bin/python3", "-m", "ananta.cli", "--app-home", "/opt/demo/profile"], "RunAtLoad": True},
        fmt=plistlib.FMT_XML,
    )


def _corpus() -> dict[str, bytes]:
    """Each way ``plistlib.loads`` rejects bytes, one corruption of a valid plist per entry."""
    valid = _valid_xml()
    return {
        "truncated XML": valid[:-40],
        "mismatched tag": valid.replace(b"</dict>", b"</array>", 1),
        "bad integer": valid.replace(b"<true/>", b"<integer>x</integer>", 1),
        "bad real": valid.replace(b"<true/>", b"<real>x</real>", 1),
        "bad date": valid.replace(b"<true/>", b"<date>nope</date>", 1),
        "key with no value": valid.replace(b"<true/>", b"", 1),
        "binary garbage": b"bplist00" + bytes(range(1, 60)),
        "empty": b"",
        "invalid UTF-8": b"\xff\xfe\xfa not a plist \xc3\x28",
        "unknown encoding": valid.replace(b'<?xml version="1.0" encoding="UTF-8"?>', b'<?xml version="1.0" encoding="no-such-codec"?>', 1),
        "deep nesting": b"<?xml version=\"1.0\"?><plist version=\"1.0\">" + b"<array>" * 5000 + b"</array>" * 5000 + b"</plist>",
        "array root": plistlib.dumps(["Label"], fmt=plistlib.FMT_XML),
        "string root": plistlib.dumps("Label", fmt=plistlib.FMT_XML),
    }


def _check_manager_readers() -> None:
    for mode, raw in _corpus().items():
        _check(launch_topology.parse_plist(raw) is None, f"{mode}: parse_plist reads it as not a plist")
        _check(launch_topology.plist_label(raw) == "" and launch_topology.plist_program_arguments(raw) == (), f"{mode}: no label and no arguments")
        _check(launch_topology.derive_launch_topology(raw) == launch_topology.UNSUPPORTED_TOPOLOGY, f"{mode}: the topology is unsupported, not a raise")
    valid = launch_topology.parse_plist(_valid_xml())
    _check(valid is not None and valid["Label"] == "local.solet.demo", "control: a valid plist parses")
    _check(launch_topology.derive_launch_topology(_valid_xml()) == launch_topology.LEGACY_DIRECT, "control: a valid legacy_direct plist derives its topology")


def _check_lm_studio_login() -> None:
    with tempfile.TemporaryDirectory() as raw_home:
        home = Path(raw_home)
        agents = home / "Library" / "LaunchAgents"
        helper = home / "Library" / "Application Support" / "Solet" / "LM Studio" / "start.sh"
        agents.mkdir(parents=True)
        helper.parent.mkdir(parents=True)
        helper.write_text("#!/bin/sh\n")
        plist = agents / f"{_LABEL}.plist"
        for mode, raw in _corpus().items():
            plist.write_bytes(raw)
            _check(_login_valid(home) is None, f"{mode}: the LM Studio login check is undetermined, not a raise")
        plist.write_bytes(plistlib.dumps({"Label": _LABEL, "ProgramArguments": ["/bin/sh", str(helper)], "RunAtLoad": True}))
        _check(_login_valid(home) is True, "control: the login item this solet wrote reads as valid")


def _check_doctor_launch_vector() -> None:
    with tempfile.TemporaryDirectory() as raw_home:
        home = Path(raw_home)
        label = "local.solet.demo"
        interpreter, app_home = Path("/opt/demo/.venv/bin/python3"), Path("/opt/demo/profile")
        plist = launch_topology.launchagent_plist_path(home, label)
        plist.parent.mkdir(parents=True)
        probe = cast(Any, SimpleNamespace(seams=SimpleNamespace(home=home)))
        for mode, raw in _corpus().items():
            plist.write_bytes(raw)
            _check(_target_launch_vector(probe, label, interpreter, app_home) is False, f"{mode}: the launch-vector check is False, not a raise")
        plist.write_bytes(_valid_xml())
        _check(_target_launch_vector(probe, label, interpreter, app_home) is True, "control: the exact launch vector is accepted")


def _check_twin_is_equal() -> None:
    _check(set(launch_topology.PLIST_PARSE_ERRORS) == set(target_reconciliation.PLIST_PARSE_ERRORS), "the Manager's PLIST_PARSE_ERRORS equals the seed's")


def _guarded(call: ast.Call, parents: dict[ast.AST, ast.AST]) -> bool:
    node: ast.AST = call
    while node in parents:
        child, node = node, parents[node]
        if isinstance(node, ast.Try) and child in node.body:
            for handler in node.handlers:
                names = {item.id for item in ast.walk(handler.type) if isinstance(item, ast.Name)} if handler.type is not None else {"Exception"}
                if names & {"PLIST_PARSE_ERRORS", "Exception", "BaseException"}:
                    return True
    return False


def _is_plistlib_read(node: ast.AST) -> bool:
    func = node.func if isinstance(node, ast.Call) else None
    return isinstance(func, ast.Attribute) and func.attr in {"load", "loads"} and isinstance(func.value, ast.Name) and func.value.id == "plistlib"


def _file_read_sites(path: Path) -> list[tuple[str, int, bool]]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    parents = {child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}
    return [(str(path.relative_to(_ROOT)), node.lineno, _guarded(node, parents)) for node in ast.walk(tree) if isinstance(node, ast.Call) and _is_plistlib_read(node)]


def _plist_read_sites(roots: tuple[Path, ...]) -> list[tuple[str, int, bool]]:
    files = [path for root in roots for path in (sorted(root.rglob("*.py")) if root.is_dir() else [root])]
    return [site for path in files for site in _file_read_sites(path)]


def _check_class_guard() -> None:
    sites = _plist_read_sites(
        (
            _ROOT / "solet_cli" / "src" / "solet_manager",
            _ROOT / "plugins" / "github_midwife_plugin" / "src" / "github_midwife_plugin",
            _ROOT / "deployment" / "scripts" / "migrate_to_solet.py",
        )
    )
    _check(len(sites) >= 5, f"the guard sees the plist readers it is meant to guard ({len(sites)})")
    unguarded = [f"{path}:{line}" for path, line, guarded in sites if not guarded]
    _check(not unguarded, f"every plistlib read is inside a try that catches PLIST_PARSE_ERRORS or Exception; unguarded: {unguarded}")


def main() -> int:
    _check_manager_readers()
    _check_lm_studio_login()
    _check_doctor_launch_vector()
    _check_twin_is_equal()
    _check_class_guard()
    print(f"launch_topology_plist_robustness_smoke: ok ({_CHECKS} checks)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
