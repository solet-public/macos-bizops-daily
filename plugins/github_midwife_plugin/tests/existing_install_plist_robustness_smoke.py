"""iss_92735a39 and iss_68bc97bb (r66): the seed's plist and managed-file readers block cleanly, none raises.

Hermetic, on the same fake ``Runtime`` and Git target ``existing_install_operations_smoke`` builds.

- **Unparseable plists in the rename migration** (``existing_install_migrations._load_plist``): a plist that ``plistlib.loads`` rejects in any of its
  ways (expat's ``ExpatError`` for truncated or mismatched XML, ``AttributeError`` for a bad ``<date>``, ``LookupError``, ``RecursionError`` and the
  rest) is left alone by the probe, which verifies instead of raising out of ``update --dry-run``; a valid legacy plist is still planned (the control).
- **A plist that changes between probe and apply**: every plist is re-read before the first is rewritten, so one that no longer parses stops the
  rename as ``probe_drift`` with nothing written.
- **Non-UTF-8 managed files** (``existing_install_migrations.read_text``): a binary1 LaunchAgent plist (what ``plutil -convert binary1`` writes), a
  non-UTF-8 ``.zshrc`` and a non-UTF-8 ``root_manifest.yaml`` block the probe and the apply with ``managed_block_unknown_origin`` and a repair that
  names the file and, for a plist, ``plutil -convert xml1``; the file is never read as absent, never rewritten; the same plist as XML text still
  adopts (the control).
- **Every read site** (round 2): ``NotTextError`` is mapped once, in the handler table, so the export-root connector config, ``installed_plugins.json``
  and ``~/.claude.json`` block the probe and the apply as well; a census fails when a ``read_text``/``read_json`` caller appears that is not listed.
- **A CRLF plist** is still adopted, and its row binds the digest of its raw bytes (the digest the Manager's backup takes), not of the newline-translated text.
- **Seed text for the Manager**: every new repair passes the Manager's real ``public_string``; the same text with a Homebrew keg path is refused raw.
"""

from __future__ import annotations

import ast
import plistlib
import sys
from collections.abc import Callable
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, cast
from unittest.mock import patch

_REPO = Path(__file__).resolve().parents[3]
sys.path[:0] = [str(Path(__file__).resolve().parent), str(_REPO / "solet_cli" / "src"), str(_REPO / "solet_setup_contracts" / "src")]
import existing_install_operations_smoke as base  # noqa: E402
from existing_install_operations_smoke import FakeRuntime, _apply, _probe, _request, _state, _target  # noqa: E402
from existing_install_plist_adopt_smoke import _adopt_fixture, _dax_plist  # noqa: E402
from github_midwife_plugin import existing_install_migrations, existing_install_operations, existing_install_plugin_transitions  # noqa: E402
from github_midwife_plugin.existing_install_migrations import NotTextError, read_text  # noqa: E402
from github_midwife_plugin.managed_render import sha256_bytes, sha256_text  # noqa: E402
from github_midwife_plugin.setup_adapter import dispatch_request  # noqa: E402
from solet_manager.adapter_validation import public_string  # noqa: E402

_NAME = base._NAME  # noqa: SLF001 -- the fixture's solet name
_CHECKS = 0
_RENAME = "existing::migration.solet_rename"
_AUTOSTART = "existing::autostart.reconcile"
_HYDRATION = "existing::hydration.reconcile"
_ARTIFACT = "instance_launchagent_plist"
_REPAIRS: list[str] = []


def _check(condition: object, label: str) -> None:
    global _CHECKS
    _CHECKS += 1
    if not condition:
        raise AssertionError(label)


def _legacy_plist(target: Path) -> bytes:
    return plistlib.dumps(
        {"Label": f"local.homunculus.{_NAME}", "ProgramArguments": [str(target / ".venv/bin/python3"), "-m", "ananta.cli", "--homunculus", _NAME], "EnvironmentVariables": {"HOMUNCULUS_NAME": _NAME}, "RunAtLoad": True},
        fmt=plistlib.FMT_XML,
    )


def _corpus(target: Path) -> dict[str, bytes]:
    """Each way ``plistlib.loads`` rejects bytes, one corruption of a valid plist per entry."""
    valid = _legacy_plist(target)
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
        "deep nesting": b'<?xml version="1.0"?><plist version="1.0">' + b"<array>" * 5000 + b"</array>" * 5000 + b"</plist>",
        "array root": plistlib.dumps(["Label"], fmt=plistlib.FMT_XML),
    }


def _rename(target: Path, runtime: FakeRuntime, *, phase: str = "probe") -> dict[str, Any]:
    return cast(dict[str, Any], dispatch_request(_request(target, _RENAME, phase=phase), runtime))


def _fixture(root: Path) -> tuple[Path, FakeRuntime, Path]:
    target, _ = _target(root)
    runtime = FakeRuntime(root / "home")
    agents = runtime.home / "Library" / "LaunchAgents"
    agents.mkdir(parents=True)
    return target, runtime, agents


def _check_unparseable_plists_in_rename(root: Path) -> None:
    for index, (mode, raw) in enumerate(_corpus(Path("/x")).items()):
        for label, filename in (("legacy filename", f"local.homunculus.{_NAME}.plist"), ("solet filename", f"local.solet.{_NAME}.plist")):
            target, runtime, agents = _fixture(root / f"rename-{index}-{label.split()[0]}")
            plist = agents / filename
            plist.write_bytes(raw)
            probe = _rename(target, runtime)
            _check(probe["checkpoint_status"] == "verified" and not probe["planned_actions"], f"{mode} ({label}): the probe leaves it alone and verifies, it does not raise ({probe['checkpoint_status']})")
            applied = _rename(target, runtime, phase="apply")
            _check(applied["checkpoint_status"] == "applied" and plist.read_bytes() == raw and not runtime.writes, f"{mode} ({label}): the apply writes nothing")
    target, runtime, agents = _fixture(root / "rename-control")
    (agents / f"local.homunculus.{_NAME}.plist").write_bytes(_legacy_plist(target))
    probe = _rename(target, runtime)
    _check(probe["checkpoint_status"] == "pending" and len(probe["planned_actions"]) == 1, "control: a valid legacy plist is still planned for rename")


def _check_plist_changed_between_probe_and_apply(root: Path) -> None:
    target, runtime, agents = _fixture(root / "race")
    first, second = agents / f"local.homunculus.{_NAME}.plist", agents / f"local.homunculus.{_NAME}.z.plist"  # the planner sorts by name, so the plist that fails is the last one
    for plist in (first, second):
        plist.write_bytes(_legacy_plist(target))
    runtime.launchctl_loaded.add(f"local.homunculus.{_NAME}")
    real = existing_install_migrations._load_plist  # noqa: SLF001 -- the one reader the rename plans and re-reads through
    calls: dict[Path, int] = {}

    def load(path: Path) -> dict[str, object] | None:
        calls[path] = calls.get(path, 0) + 1
        return None if path == second and calls[path] > 1 else real(path)

    before = {plist: plist.read_bytes() for plist in (first, second)}
    with patch.object(existing_install_migrations, "_load_plist", load):
        blocked = _rename(target, runtime, phase="apply")
    _check(blocked["error_kind"] == "probe_drift" and blocked["checkpoint_status"] == "blocked", f"a plist that stops parsing after it was planned stops the rename as probe_drift ({blocked.get('error_kind')})")
    _check({plist: plist.read_bytes() for plist in (first, second)} == before and not runtime.writes and not any("bootout" in command for command in runtime.commands), "nothing was rewritten, not even the plist that still parsed")
    _REPAIRS.append(blocked["repair"])


def _check_binary_plist_blocks(root: Path) -> None:
    target, runtime, plist = _adopt_fixture(root / "binary-plist")
    xml = _dax_plist(target, runtime)
    binary = plistlib.dumps(plistlib.loads(xml), fmt=plistlib.FMT_BINARY)
    _check(not binary.startswith(b"<?xml") and binary.startswith(b"bplist00"), "the fixture is a real binary1 plist")
    plist.write_bytes(binary)
    probe = _probe(target, runtime, _AUTOSTART, _ARTIFACT)
    _check(probe["checkpoint_status"] == "blocked" and probe["error_kind"] == "managed_block_unknown_origin", f"a binary1 plist blocks the probe instead of raising ({probe['checkpoint_status']}, {probe['error_kind']})")
    _check(str(plist) in probe["repair"] and "plutil -convert xml1" in probe["repair"] and probe["repair"].count(str(plist)) == 1, "the repair names the file once and the plutil command that makes it text")
    applied = _apply(target, runtime, _AUTOSTART, _ARTIFACT)
    _check(applied["checkpoint_status"] == "blocked" and plist.read_bytes() == binary and not runtime.writes, "the apply refuses and the binary plist is untouched")
    _REPAIRS.append(probe["repair"])
    plist.write_bytes(xml)
    control = _probe(target, runtime, _AUTOSTART, _ARTIFACT)
    _check(control["checkpoint_status"] == "pending" and _state(control, _ARTIFACT)["action"] == "render_whole", "control: the same plist as XML text is adopted")


def _check_non_utf8_shell_and_manifest(root: Path) -> None:
    target, _ = _target(root / "zshrc")
    runtime = FakeRuntime(root / "zshrc" / "home")
    runtime.home.mkdir()
    zshrc = runtime.home / ".zshrc"
    zshrc.write_bytes(b"export A=1\n\xff\xfe\x00 latin1 \xe9 bytes\n")
    probe = _probe(target, runtime, _HYDRATION, "shell_startup_block")
    _check(probe["checkpoint_status"] == "blocked" and probe["error_kind"] == "managed_block_unknown_origin" and str(zshrc) in probe["repair"], f"a non-UTF-8 .zshrc blocks the hydration probe ({probe['checkpoint_status']}, {probe['error_kind']})")
    applied = _apply(target, runtime, _HYDRATION, "shell_startup_block")
    _check(applied["checkpoint_status"] == "blocked" and zshrc.read_bytes().startswith(b"export A=1") and not runtime.writes, "the apply leaves the non-UTF-8 .zshrc as it was")
    _REPAIRS.append(probe["repair"])
    zshrc.write_text("export A=1\n")
    _check(_probe(target, runtime, _HYDRATION, "shell_startup_block")["checkpoint_status"] == "pending", "control: a text .zshrc appends the block as before")
    target, _ = _target(root / "manifest")
    runtime = FakeRuntime(root / "manifest" / "home")
    (runtime.home / "Library" / "LaunchAgents").mkdir(parents=True)
    manifest = target / "root_manifest.yaml"
    manifest.write_bytes(b"homunculus_name: iris\n\xff\xfe\x00")
    blocked = _rename(target, runtime)
    _check(blocked["checkpoint_status"] == "blocked" and blocked["error_kind"] == "managed_block_unknown_origin" and str(manifest) in blocked["repair"], f"a non-UTF-8 root_manifest.yaml blocks the rename probe ({blocked['checkpoint_status']})")
    _check(manifest.read_bytes().endswith(b"\xff\xfe\x00") and not runtime.writes, "the rename leaves it as it was")
    _REPAIRS.append(blocked["repair"])


def _check_read_text_is_typed(root: Path) -> None:
    path = root / "not-text.bin"
    path.write_bytes(b"bplist00\xff\xfe")
    for label, call in (("read_text", lambda: read_text(path)), ("transitions._require_regular", lambda: existing_install_plugin_transitions._require_regular(path, "code"))):  # noqa: SLF001
        _check(_raises(call, NotTextError), f"{label}: bytes that are not UTF-8 raise the typed NotTextError, not UnicodeDecodeError")
    _check(read_text(root / "absent.txt") is None, "control: an absent file still reads as None")
    path.write_text("plain text\n")
    _check(read_text(path) == "plain text\n", "control: a text file still reads")


def _check_plugin_transition_entry_blocks(root: Path) -> None:
    """The plugin-transition operation reads release and profile files as text; a ``NotTextError`` out of any of them is a blocked result, not an exception."""
    target, runtime, _ = _fixture(root / "transition")
    not_text = NotTextError(target / "unreadable-fixture-file")

    def unreadable(_: Path) -> object:
        raise not_text

    with patch.object(existing_install_plugin_transitions, "load_declaration", unreadable):
        probe = cast(dict[str, Any], dispatch_request(_request(target, "existing::migration.plugin_transition"), runtime))
    _check(probe["checkpoint_status"] == "blocked" and probe["error_kind"] == "managed_block_unknown_origin" and str(not_text.path) in probe["repair"], f"the plugin-transition probe blocks on a non-text file ({probe['checkpoint_status']}, {probe.get('error_kind')})")
    _REPAIRS.append(probe["repair"])


def _check_crlf_plist_digest(root: Path) -> None:
    """The row binds the digest of the raw bytes it replaces, so the Manager's backup of those bytes agrees with it; a digest of the newline-translated text would not."""
    target, runtime, plist = _adopt_fixture(root / "crlf")
    lf = _dax_plist(target, runtime)
    crlf = lf.replace(b"\n", b"\r\n")
    _check(crlf != lf and sha256_text(crlf.decode().replace("\r\n", "\n")) != sha256_bytes(crlf), "the fixture is a CRLF plist whose newline-translated text digest and raw byte digest differ")
    plist.write_bytes(crlf)
    probe = _probe(target, runtime, _AUTOSTART, _ARTIFACT)
    state = _state(probe, _ARTIFACT)
    _check(probe["checkpoint_status"] == "pending" and state["action"] == "render_whole", f"a valid CRLF plist is still adopted ({probe['checkpoint_status']}, {state['action']})")
    _check(state["current_sha256"] == sha256_bytes(crlf), "its row binds the digest of its raw bytes, the digest the Manager's backup takes")
    plist.write_bytes(lf)
    _check(_state(_probe(target, runtime, _AUTOSTART, _ARTIFACT), _ARTIFACT)["current_sha256"] == sha256_bytes(lf), "control: an LF plist binds its own digest, as before")


def _check_every_read_site_blocks(root: Path) -> None:
    """Every operator-writable file the existing-install operations read, other than the ones above, blocks the probe and the apply when it is not text."""
    not_text = b"\xff\xfe\x00 not utf-8 \xc3\x28"
    target, runtime, agents = _fixture(root / "sites")
    del agents
    config = target / "profile" / "config" / "plugins"
    config.mkdir(parents=True)
    for name in ("jira_plugin", "salesforce_plugin"):
        (target / "plugins" / name).mkdir(parents=True)
    (config / "jira_plugin.json").write_bytes(not_text)
    sites = {
        "existing::migration.export_root_containment": config / "jira_plugin.json",
        "existing::migration.solet_rename": runtime.home / ".claude.json",
        "existing::runtime.plugin_cache_refresh": runtime.home / ".claude" / "plugins" / "installed_plugins.json",
    }
    (runtime.home / ".claude" / "plugins").mkdir(parents=True)
    for ref, path in sites.items():
        path.write_bytes(not_text)
        for phase in ("probe", "apply"):
            outcome = cast(dict[str, Any], dispatch_request(_request(target, ref, phase=phase), runtime))
            _check(outcome["checkpoint_status"] == "blocked" and outcome["error_kind"] == "managed_block_unknown_origin" and str(path) in outcome["repair"], f"{ref} {phase}: a non-UTF-8 {path.name} blocks with a repair naming it ({outcome['checkpoint_status']}, {outcome.get('error_kind')})")
        _check(path.read_bytes() == not_text and not runtime.writes, f"{ref}: the file is untouched and nothing was written")
        _REPAIRS.append(outcome["repair"])
        path.unlink()


_READ_SITES: frozenset[tuple[str, str]] = frozenset(
    {
        ("existing_install_migrations.py", "_rename_plan"),
        ("existing_install_migrations.py", "_apply_text_renames"),
        ("existing_install_migrations.py", "_claude_json_servers"),
        ("existing_install_migrations.py", "_apply_claude_json"),
        ("existing_install_migrations.py", "_connector_roots"),
        ("existing_install_migrations.py", "_cache_root"),
        ("existing_install_migrations.py", "read_json"),
        ("existing_install_plugin_transitions.py", "_require_regular"),
        ("existing_install_plugin_transitions.py", "_plugin_config"),
    }
)


def _read_site_callers(path: Path) -> set[tuple[str, str]]:
    """``(file, function)`` for every function in ``path`` that calls ``read_text`` or ``read_json`` by name."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return {
        (path.name, function.name)
        for function in ast.walk(tree)
        if isinstance(function, ast.FunctionDef)
        for call in ast.walk(function)
        if isinstance(call, ast.Call) and isinstance(call.func, ast.Name) and call.func.id in {"read_text", "read_json"}
    }


def _check_read_site_census() -> None:
    """The class guard for iss_68bc97bb: every caller of ``read_text``/``read_json`` is listed, and every handler in the table is wrapped to block on ``NotTextError``.

    A new caller fails this until it is listed here, which is the moment to confirm the handler it runs under is in ``operation_handlers()``.
    The release-owned reads (the bundle, the templates, the declaration) are ``Path`` reads of files the release ships and are not in the census.
    """
    package = Path(existing_install_migrations.__file__).parent
    found: set[tuple[str, str]] = set()
    for name in ("existing_install_migrations.py", "existing_install_plugin_transitions.py", "existing_install_operations.py"):
        found |= _read_site_callers(package / name)
    _check(found == _READ_SITES, f"the read_text/read_json callers are exactly the listed ones: new {sorted(found - _READ_SITES)}, gone {sorted(_READ_SITES - found)}")
    handlers = existing_install_operations.operation_handlers()
    _check(len(handlers) == 6 and all(hasattr(handler, "__wrapped__") for handler in handlers.values()), "every existing:: handler is wrapped to block on a file that is not text")


def _raises(call: Callable[[], object], kind: type[Exception]) -> bool:
    try:
        call()
    except kind:
        return True
    except Exception:  # noqa: BLE001 -- any other type is the failure this reports
        return False
    return False


def _check_repairs_are_public() -> None:
    _check(len(_REPAIRS) == 8, f"every new repair was collected ({len(_REPAIRS)})")
    for repair in _REPAIRS:
        _check(public_string(repair, "repair", maximum=2048) == repair, f"the real Manager validator accepts: {repair[:60]}")
        _check(_raises(lambda text=repair: public_string(f"{text} /opt/homebrew/Cellar/solet/0.1.0_64/libexec", "repair", maximum=2048), ValueError), "control: the same text with a Homebrew keg path is refused raw")


def main() -> int:
    with TemporaryDirectory() as raw:
        root = Path(raw)
        _check_unparseable_plists_in_rename(root / "a")
        _check_plist_changed_between_probe_and_apply(root / "b")
        _check_binary_plist_blocks(root / "c")
        _check_non_utf8_shell_and_manifest(root / "d")
        (root / "e").mkdir()
        _check_read_text_is_typed(root / "e")
        _check_plugin_transition_entry_blocks(root / "f")
        _check_crlf_plist_digest(root / "g")
        _check_every_read_site_blocks(root / "h")
        _check_read_site_census()
    _check_repairs_are_public()
    print(f"existing_install_plist_robustness_smoke: ok ({_CHECKS} checks)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
