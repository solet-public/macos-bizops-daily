"""r64 fix, extended in r65: the ``claude`` CLI check also finds Claude Code's native install in ``$HOME/.local/bin``.

Claude Code's native installer puts ``claude`` in ``~/.local/bin``.  A solet whose CLI is that install, run from a
context without that directory on PATH (a launchd job, a solet session, a non-login shell), was refused
``claude_cli_missing`` by the source preview although the CLI is installed.  Two resolvers must agree, or the
preview passes and the runtime stage refuses after the source has advanced: the Manager's preview probe
(``host_software.update_prerequisite_checks``) and the seed adapter's ``resolve_executable``.  r65 (iss_26fdde33): the
bootstrap adapter's create-path ``resolve_executable`` is the third, and held equal to the other two; without it a solet
create planned ``brew install --cask claude-code`` on a host whose native ``claude`` was already installed.

- **native only**: ``claude`` only in ``<home>/.local/bin`` with PATH excluding it is verified by the preview probe,
  the preview preflight and the seed adapter's resolver.
- **control, absent**: no ``claude`` anywhere still refuses ``claude_cli_missing`` and both the refusal message and
  its repair name every searched directory, the native one included; the runtime stage's refusal names them too.
- **control, PATH wins**: ``claude`` on PATH is the one returned, never a fallback.
- **agreement**: the Manager, the seed adapter and the bootstrap adapter search the same directories in the same
  order, the native one last (after PATH and both Homebrew directories).
- **bootstrap adapter**: native only resolves and the create route verifies it without planning the Homebrew cask;
  absent still plans the cask install; PATH wins; the native directory comes from ``$HOME``, not a hard-coded string.

Every host read goes through an injected seam over a temporary tree, so the host's own ``claude`` never decides.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

_ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [
    str(_ROOT),
    str(_ROOT / "solet_cli" / "src"),
    str(_ROOT / "solet_setup_contracts" / "src"),
    str(_ROOT / "plugins" / "github_midwife_plugin" / "src"),
]

from github_midwife_plugin.existing_install_migrations import plugin_cache_refresh  # noqa: E402
from github_midwife_plugin.setup_adapter_contract import AdapterRequest  # noqa: E402
from github_midwife_plugin.setup_adapter_runtime import CommandOutcome, resolve_executable  # noqa: E402
from solet_manager import host_software  # noqa: E402
from solet_manager.errors import HostRequirementError  # noqa: E402
from solet_manager.update_local_state import host_preflight  # noqa: E402
from solet_manager.update_runtime_plan import RuntimeSeams  # noqa: E402

import bootstrap_adapter.protocol as bootstrap_protocol  # noqa: E402
from bootstrap_adapter.models import AdapterRuntime  # noqa: E402
from bootstrap_adapter.routes import _coding_tool_route  # noqa: E402

_CHECKS = 0
_HOMEBREW_DIRECTORIES = ("/opt/homebrew/bin", "/usr/local/bin")


def _check(condition: object, label: str) -> None:
    global _CHECKS
    _CHECKS += 1
    if not condition:
        raise AssertionError(label)


class _Host:
    """One temporary host: a HOME (with the native install directory) and a PATH directory."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.home = root / "home"
        self.path_bin = root / "path_bin"
        self.native_bin = self.home / ".local" / "bin"
        for directory in (self.home, self.path_bin):
            directory.mkdir(parents=True)

    def install(self, directory: Path, name: str = "claude") -> Path:
        directory.mkdir(parents=True, exist_ok=True)
        stub = directory / name
        stub.write_text("#!/bin/sh\necho claude fixture 0.0\n", encoding="utf-8")
        stub.chmod(0o755)
        return stub

    def _resolve(self, name: str) -> str | None:
        """``shutil.which`` semantics over the fixture: a bare name searches ``path_bin``; an absolute path counts only
        inside the fixture root, so a ``claude`` the developer's host really has in /opt/homebrew/bin never leaks in."""
        if os.path.isabs(name):
            inside = Path(name).is_relative_to(self.root)
            return name if inside and Path(name).is_file() and os.access(name, os.X_OK) else None
        return shutil.which(name, path=str(self.path_bin))

    def seams(self) -> RuntimeSeams:
        return RuntimeSeams(home=self.home, resolve_base_python=lambda: Path(sys.executable), which=self._resolve)

    def runtime(self) -> _Runtime:
        return _Runtime(self)


class _Runtime:
    """The seed adapter's host boundary: ``/usr/bin/which`` answered by the same fixture resolution."""

    def __init__(self, host: _Host) -> None:
        self.home = host.home
        self._host = host
        self.which_calls: list[str] = []

    def run(self, argv: tuple[str, ...], **_: object) -> CommandOutcome:
        if argv[0] != "/usr/bin/which":
            raise AssertionError(f"unexpected command: {argv!r}")
        self.which_calls.append(argv[1])
        found = self._host._resolve(argv[1])  # noqa: SLF001 - the fixture's own resolution
        return CommandOutcome(0, False, 1, found, "") if found is not None else CommandOutcome(1, False, 1, "", "")


def _row(seams: RuntimeSeams) -> host_software.HostCheck:
    (row,) = host_software.update_prerequisite_checks(seams)
    return row


def _refusal(seams: RuntimeSeams, target: Path) -> HostRequirementError | None:
    try:
        host_preflight(seams, target)
    except HostRequirementError as exc:
        return exc
    return None


def _request(target: Path) -> AdapterRequest:
    return AdapterRequest(
        request_id="8f2f3ed3-03fc-4f58-915e-eb400a172a67",
        operation_id="plugin_cache_refresh",
        operation_ref="existing::plugin_cache.refresh",
        phase="probe",
        probe_purpose="pre_apply",
        attempt=1,
        name="fixture",
        target=target,
        flow_source_revision="a" * 40,
        answers_fingerprint="sha256:" + "b" * 64,
        approval_fingerprint=None,
        dry_run=True,
        timeout_seconds=30,
        public_inputs={},
    )


class _BootstrapProbe:
    """The bootstrap adapter's ``which`` seam over the fixture, recording every lookup in order."""

    def __init__(self, host: _Host) -> None:
        self._host = host
        self.calls: list[str] = []

    def which(self, name: str) -> str | None:
        self.calls.append(name)
        return self._host._resolve(name)  # noqa: SLF001 - the fixture's own resolution

    def runtime(self, root: Path) -> AdapterRuntime:
        def run(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(command, 0, "fixture\n", "")

        return AdapterRuntime(run=run, which=self.which, now=lambda: datetime(2026, 9, 30, tzinfo=UTC), name="fixture", target=root)


def _create_route(runtime: AdapterRuntime) -> dict[str, object]:
    request = {"operation_id": "install_claude_cli", "phase": "probe", "probe_purpose": None, "request_id": "00000000-0000-0000-0000-000000000000", "name": "fixture"}
    return dict(_coding_tool_route(request, runtime))  # type: ignore[arg-type]


def _planned_ids(route: dict[str, object]) -> list[str]:
    return [str(action["id"]) for action in route["planned_actions"]]  # type: ignore[attr-defined]


def _leg_bootstrap_adapter(root: Path) -> None:
    host = _Host(root)
    native = host.install(host.native_bin)
    with patch.dict(os.environ, {"HOME": str(host.home)}):
        probe = _BootstrapProbe(host)
        _check(bootstrap_protocol.resolve_executable(probe.runtime(root), "claude") == str(native), "the bootstrap adapter resolves the native claude")
        route = _create_route(_BootstrapProbe(host).runtime(root))
    _check(route["checkpoint_status"] == "verified", f"the create route verifies a claude only ~/.local/bin holds: {route['checkpoint_status']}")
    _check("claude.install_homebrew_package" not in _planned_ids(route), "the create route plans no Homebrew claude cask beside the native one")


def _leg_bootstrap_absent(root: Path) -> None:
    host = _Host(root)
    with patch.dict(os.environ, {"HOME": str(host.home)}):
        probe = _BootstrapProbe(host)
        _check(bootstrap_protocol.resolve_executable(probe.runtime(root), "claude") is None, "the bootstrap adapter resolves nothing when claude is nowhere")
        route = _create_route(_BootstrapProbe(host).runtime(root))
    _check(route["checkpoint_status"] == "pending" and _planned_ids(route) == ["claude.install_homebrew_package"], f"no claude anywhere still plans the cask install: {route['checkpoint_status']} {_planned_ids(route)}")


def _leg_bootstrap_path_wins(root: Path) -> None:
    host = _Host(root)
    on_path = host.install(host.path_bin)
    host.install(host.native_bin)
    with patch.dict(os.environ, {"HOME": str(host.home)}):
        probe = _BootstrapProbe(host)
        _check(bootstrap_protocol.resolve_executable(probe.runtime(root), "claude") == str(on_path), "claude on PATH wins over the native one in the bootstrap adapter")
    _check(probe.calls[0] == "claude", f"the bootstrap adapter asks PATH first: {probe.calls}")


def _leg_native_only(root: Path) -> None:
    host = _Host(root)
    native = host.install(host.native_bin)
    seams = host.seams()
    row = _row(seams)
    _check(row.status.value == "verified" and row.observed == str(native), f"the preview probe finds claude in ~/.local/bin: {row}")
    _check(_refusal(seams, root) is None, "the preview preflight does not refuse a claude that only ~/.local/bin holds")
    _check(resolve_executable(host.runtime(), "claude") == str(native), "the seed adapter resolves the same native claude")


def _leg_absent(root: Path) -> None:
    host = _Host(root)
    seams = host.seams()
    row = _row(seams)
    _check(row.status.value == "missing" and row.reason == "claude_cli_missing", f"no claude anywhere is claude_cli_missing: {row}")
    refusal = _refusal(seams, root)
    _check(refusal is not None and refusal.error_kind == "claude_cli_missing", f"the preview still refuses claude_cli_missing: {refusal}")
    assert refusal is not None
    searched = [*_HOMEBREW_DIRECTORIES, str(host.native_bin)]
    for text_name, text in (("message", str(refusal)), ("repair", str(refusal.repair))):
        _check(all(directory in text for directory in searched), f"the refusal {text_name} names every searched directory {searched}: {text}")
    _check("brew install --cask claude-code" in str(refusal.repair) and "command -v claude" in str(refusal.repair), "the repair keeps the exact install command")
    _check(resolve_executable(host.runtime(), "claude") is None, "the seed adapter still resolves nothing")
    blocked = plugin_cache_refresh(_request(root), host.runtime())
    _check(blocked["checkpoint_status"] == "blocked" and blocked["error_kind"] == "claude_cli_missing", f"the runtime stage still refuses claude_cli_missing: {blocked}")
    _check(all(directory in str(blocked["repair"]) for directory in searched), f"the runtime stage's repair names every searched directory: {blocked['repair']}")


def _leg_path_wins(root: Path) -> None:
    host = _Host(root)
    on_path = host.install(host.path_bin)
    host.install(host.native_bin)
    row = _row(host.seams())
    _check(row.status.value == "verified" and row.observed == str(on_path), f"claude on PATH wins over the native one in the preview probe: {row}")
    runtime = host.runtime()
    _check(resolve_executable(runtime, "claude") == str(on_path), "claude on PATH wins over the native one in the seed adapter")
    _check(runtime.which_calls == ["claude"], f"a PATH hit stops the search: {runtime.which_calls}")


def _leg_agreement(root: Path) -> None:
    host = _Host(root)
    runtime = host.runtime()
    resolve_executable(runtime, "claude")
    adapter_order = [Path(item).parent for item in runtime.which_calls[1:]]
    manager_order = list(host_software.claude_search_directories(host.seams()))
    _check(adapter_order == manager_order, f"the Manager and the seed adapter search the same directories in the same order: {manager_order} vs {adapter_order}")
    with patch.dict(os.environ, {"HOME": str(host.home)}):
        probe = _BootstrapProbe(host)
        bootstrap_protocol.resolve_executable(probe.runtime(root), "claude")
    _check(probe.calls[0] == "claude", f"the bootstrap adapter asks PATH first: {probe.calls}")
    bootstrap_order = [Path(item).parent for item in probe.calls[1:]]
    _check(bootstrap_order == manager_order, f"the bootstrap adapter searches the same directories in the same order: {manager_order} vs {bootstrap_order}")
    _check(manager_order[-1] == host.native_bin, f"the native directory comes last, after PATH and both Homebrew directories: {manager_order}")


def main() -> int:
    legs = (
        ("native only", _leg_native_only),
        ("absent control", _leg_absent),
        ("PATH wins control", _leg_path_wins),
        ("bootstrap adapter native only", _leg_bootstrap_adapter),
        ("bootstrap adapter absent control", _leg_bootstrap_absent),
        ("bootstrap adapter PATH wins control", _leg_bootstrap_path_wins),
        ("manager and both adapters agree", _leg_agreement),
    )
    for label, leg in legs:
        with tempfile.TemporaryDirectory(prefix="claude_native_location_") as scratch:
            leg(Path(scratch).resolve())
        print(f"  ok: {label}")
    print(f"claude_native_location_smoke OK: {_CHECKS} checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
