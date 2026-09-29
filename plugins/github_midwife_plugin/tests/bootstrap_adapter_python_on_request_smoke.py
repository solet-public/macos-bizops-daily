"""The dependency-closure route marks ``python@3.13`` installed-on-request before r61 drops the dependency.

``iss_d62aeab7`` / ``dec_b08cf4c7``.  On every real install ``python@3.13`` carries
``installed_on_request=false`` and survives only because the solet keg depends on it.
r61 removes that dependency, after which ``brew autoremove`` (run by ``brew upgrade``'s
periodic cleanup) would delete the interpreter every solet venv links.  r60 closes the
hole first: ``create`` and ``update`` both run the bootstrap dependency-closure route,
so that route plans and applies ``brew tab --installed-on-request python@3.13``.

The route is exercised in its PRODUCTION shape: the adapter is called with no
``base_python`` and no venv exists yet on a fresh create.  Each leg asserts a NAMED
outcome, and each has a control that keeps it honest:

* the receipt says not-on-request           -> the probe plans ``homebrew.python_installed_on_request``
* the receipt says on-request (control)     -> nothing is planned and no ``brew`` command runs
* the interpreter is not a Homebrew keg     -> nothing is planned and no ``brew`` command runs
* apply                                     -> exactly one ``brew tab --installed-on-request python@3.13``
* fresh create / dangling venv (no launcher)-> the pre-apply plan lists the step whenever apply runs the tab
* launcher on a python@3.12 keg, or one that fails the version probe -> apply rebuilds the venv from the
  interpreter it discovers, so the plan judges THAT keg, not the launcher's; the plan lists the step whenever apply runs it
* flag-only gap, doctor's ``completion`` probe -> ``verified`` (a required check never goes red for it)
* flag-only gap, ``post_apply`` probe       -> ``blocked`` ``python_not_installed_on_request``
* two Homebrew prefixes                     -> the tab runs with the brew that owns the keg
* Homebrew absent at apply                  -> loud ``python_on_request_brew_missing``, nothing installed
* ``brew tab`` fails, or the guard env fails-> loud ``python_on_request_mark_failed``

Offline: a constructed Cellar tree under a temporary directory and a fake runner.  Nothing
here runs Homebrew or touches the real receipt of the host's ``python@3.13``.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path
from typing import Any
from unittest.mock import patch

_ROOT = Path(__file__).resolve().parents[3]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from bootstrap_adapter.models import AdapterError  # noqa: E402
from bootstrap_adapter.routes import execute_adapter_request  # noqa: E402

_ACTION_ID = "homebrew.python_installed_on_request"
_TAB = ["tab", "--installed-on-request", "python@3.13"]
_PATH_BREW = "/opt/homebrew/bin/brew"
_CLOSURE = (
    ("solet-setup-contracts", "solet_setup_contracts"),
    ("ananta", "ananta"),
    ("macos-vault-plugin", "plugins/macos_vault_plugin"),
    ("github_midwife_plugin", "plugins/github_midwife_plugin"),
    ("agent_messaging_plugin", "plugins/agent_messaging_plugin"),
)
_CHECKS = 0


def _check(condition: object, message: str) -> None:
    global _CHECKS
    _CHECKS += 1
    if not condition:
        raise AssertionError(f"red: {message}")


class _Host:
    """A constructed Homebrew Cellar, one instance target, and a recording fake runner.

    ``venv`` selects the launcher: ``"keg"`` (a symlink into the keg), ``"copied"`` (a real file),
    ``"absent"`` (a fresh create: no ``.venv/bin/python3``; ``-m venv`` builds one), ``"dangling"``,
    ``"py312"`` (a symlink into a python@3.12 keg that answers 3.12.x) or ``"failprobe"`` (a real-file launcher
    whose version probe fails). The last two make apply rebuild the venv from the discovered python@3.13.
    """

    def __init__(self, root: Path, *, on_request: bool | None, venv: str = "keg") -> None:
        root = root.resolve()  # the keg's realpath is what the route sees (macOS /var -> /private/var)
        self.root = root
        self.target = root / "instance"
        self.brew = root / "bin" / "brew"
        self.calls: list[list[str]] = []
        self.brew_available = True
        self.tab_returncode = 0
        self.broken_launcher = venv == "failprobe"
        keg = root / "Cellar" / "python@3.13" / "3.13.15"
        self.interpreter = keg / "Frameworks/Python.framework/Versions/3.13/bin/python3.13"
        self.interpreter.parent.mkdir(parents=True)
        self.interpreter.write_bytes(b"\x00")
        self.interpreter.chmod(0o755)
        self.receipt = keg / "INSTALL_RECEIPT.json"
        if on_request is not None:
            self.receipt.write_text(json.dumps({"installed_on_request": on_request}), encoding="utf-8")
        for _name, relative in _CLOSURE:
            (self.target / relative).mkdir(parents=True)
        if venv == "absent":
            return
        bin_dir = self.target / ".venv" / "bin"
        bin_dir.mkdir(parents=True)
        (bin_dir / "solet-bridge").write_text("fixture", encoding="utf-8")
        if venv == "keg":
            (bin_dir / "python3.13").symlink_to(self.interpreter)
            (bin_dir / "python3").symlink_to("python3.13")
        elif venv == "dangling":
            (bin_dir / "python3").symlink_to(root / "gone" / "python3.13")
        elif venv == "py312":
            old = root / "Cellar" / "python@3.12" / "3.12.9" / "bin" / "python3.12"
            old.parent.mkdir(parents=True)
            old.write_bytes(b"\x00")
            old.chmod(0o755)
            (old.parents[1] / "INSTALL_RECEIPT.json").write_text(json.dumps({"installed_on_request": True}), encoding="utf-8")
            (bin_dir / "python3").symlink_to(old)
        else:
            (bin_dir / "python3").write_bytes(b"\x00")
            (bin_dir / "python3").chmod(0o755)

    def on_request_now(self) -> bool:
        return bool(json.loads(self.receipt.read_text(encoding="utf-8"))["installed_on_request"])

    def run(self, command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        if command and command[0].endswith("/brew"):
            return self._brew(command, kwargs)
        if command[1:3] == ["-m", "venv"]:
            return self._build_venv(command)
        if "-I" in command and "-c" in command:
            return self._closure_probe(command)
        if command[-1:] == ["--version"]:
            banner = "Python 3.13.15\n" if command[0].endswith("python3.13") else "solet-bridge 0.1.0\n"
            return subprocess.CompletedProcess(command, 0, banner, "")
        return subprocess.CompletedProcess(command, 0, "", "")

    def _build_venv(self, command: list[str]) -> subprocess.CompletedProcess[str]:
        bin_dir = Path(command[-1]) / "bin"
        bin_dir.mkdir(parents=True, exist_ok=True)
        for name in ("python3.13", "python3"):
            (bin_dir / name).unlink(missing_ok=True)
        (bin_dir / "python3").symlink_to(command[0])  # the interpreter apply chose, not this fixture's own keg
        (bin_dir / "solet-bridge").write_text("fixture", encoding="utf-8")  # what the seed pip install leaves behind
        return subprocess.CompletedProcess(command, 0, "", "")

    def _closure_probe(self, command: list[str]) -> subprocess.CompletedProcess[str]:
        if "packages =" not in command[-1]:
            if self.broken_launcher and not Path(command[0]).is_symlink():
                return subprocess.CompletedProcess(command, 1, "", "launcher failed its version probe")
            version = "3.12.9\n" if "python@3.12" in os.path.realpath(command[0]) else "3.13.15\n"
            return subprocess.CompletedProcess(command, 0, version, "")
        packages = {
            name: {
                "version": "1.0.0",
                "direct_url": json.dumps({"url": (self.target / relative).resolve().as_uri(), "dir_info": {"editable": True}}),
            }
            for name, relative in _CLOSURE
        }
        return subprocess.CompletedProcess(command, 0, json.dumps({"pip": True, "build_backend": True, "wheel": True, "packages": packages}), "")

    def _brew(self, command: list[str], kwargs: dict[str, Any]) -> subprocess.CompletedProcess[str]:
        if command[0] == str(self.brew) and not self.brew_available:
            raise OSError("brew is not executable here")
        if command[1:] == ["--version"]:
            return subprocess.CompletedProcess(command, 0, "Homebrew 7.0.7\n", "")
        self.calls.append([*command, f"HOMEBREW_NO_AUTO_UPDATE={kwargs.get('env', {}).get('HOMEBREW_NO_AUTO_UPDATE')}"])
        owning_tab = command[0] == str(self.brew) and command[1:] == _TAB
        if owning_tab and self.tab_returncode == 0:
            self.receipt.write_text(json.dumps({"installed_on_request": True}), encoding="utf-8")
        failed = owning_tab and self.tab_returncode != 0
        return subprocess.CompletedProcess(command, self.tab_returncode if failed else 0, "", "tab failed" if failed else "")

    def tabs(self) -> list[list[str]]:
        return [call for call in self.calls if call[1:4] == _TAB]

    def request(self, flavor: str, phase: str, purpose: str | None) -> dict[str, Any]:
        existing = flavor == "existing"
        return {
            "protocol_version": 1,
            "kind": "operation_request",
            "request_id": str(uuid.uuid4()),
            "operation_id": "dependencies_reconcile" if existing else "build_instance_environment",
            "operation_ref": "existing::dependencies.reconcile" if existing else "bootstrap::environment.ensure_dependency_closure",
            "phase": phase,
            "probe_purpose": purpose,
            "attempt": 1,
            "name": "iris",
            "target": str(self.target),
            "flow_id": "existing-install" if existing else "macos.repository_setup",
            "flow_source_revision": "a" * 40,
            "answers_fingerprint": "sha256:" + "b" * 64,
            "approval_fingerprint": None if phase == "probe" else "sha256:" + "c" * 64,
            "dry_run": phase == "probe",
            "timeout_seconds": 30,
            "public_inputs": {"declared_closure": [f"{name}={relative}" for name, relative in _CLOSURE]} if existing else {},
        }

    def call(self, flavor: str, phase: str, purpose: str | None) -> dict[str, Any]:
        """The PRODUCTION shape: no ``base_python``; PATH offers a brew that does not own the keg."""

        def which(name: str) -> str | None:
            return {"brew": _PATH_BREW, "python3.13": str(self.interpreter)}.get(name)

        return execute_adapter_request(self.request(flavor, phase, purpose), runner=self.run, which=which)


def _action_ids(result: dict[str, Any]) -> list[str]:
    return [str(action["id"]) for action in result.get("planned_actions", [])]


def _assert_plan_names_the_step(flavor: str) -> None:
    with tempfile.TemporaryDirectory() as raw:
        host = _Host(Path(raw), on_request=False)
        probe = host.call(flavor, "probe", "pre_apply")
        _check(probe["checkpoint_status"] == "pending", f"{flavor}: a dependency-only python@3.13 verified: {probe['checkpoint_status']}")
        _check(_action_ids(probe) == [_ACTION_ID], f"{flavor}: the plan is not exactly the tab step: {_action_ids(probe)}")
        action = probe["planned_actions"][0]
        _check(action["target"] == "homebrew:python@3.13", f"{flavor}: the tab step names the wrong target: {action['target']}")
        _check(host.calls == [], f"{flavor}: planning ran a mutating brew command: {host.calls}")
        _check(host.on_request_now() is False, f"{flavor}: planning changed the receipt")


def _assert_apply_runs_exactly_the_tab(flavor: str) -> None:
    with tempfile.TemporaryDirectory() as raw:
        host = _Host(Path(raw), on_request=False)
        applied = host.call(flavor, "apply", None)
        _check(applied["checkpoint_status"] == "applied", f"{flavor}: apply was not applied: {applied}")
        _check([call[1:4] for call in host.calls] == [_TAB], f"{flavor}: apply did not run exactly one brew tab: {host.calls}")
        _check(host.calls[0][-1] == "HOMEBREW_NO_AUTO_UPDATE=1", f"{flavor}: the tab ran without the Homebrew guard environment")
        post = host.call(flavor, "probe", "post_apply")
        _check(post["checkpoint_status"] == "verified", f"{flavor}: the post-apply probe did not verify: {post['checkpoint_status']}")


def _assert_controls_do_nothing(flavor: str) -> None:
    with tempfile.TemporaryDirectory() as raw:
        host = _Host(Path(raw), on_request=True)
        probe = host.call(flavor, "probe", "pre_apply")
        _check(probe["checkpoint_status"] == "verified", f"{flavor}: an on-request python@3.13 was not verified: {probe['checkpoint_status']}")
        _check(_action_ids(probe) == [], f"{flavor}: an on-request python@3.13 still planned a step: {_action_ids(probe)}")
        applied = host.call(flavor, "apply", None)
        _check(applied["checkpoint_status"] == "applied" and host.calls == [], f"{flavor}: apply was not a no-op on an on-request python: {host.calls}")
    with tempfile.TemporaryDirectory() as raw:
        host = _Host(Path(raw), on_request=False, venv="copied")
        probe = host.call(flavor, "probe", "pre_apply")
        _check(probe["checkpoint_status"] == "verified", f"{flavor}: a non-Homebrew interpreter was not verified: {probe['checkpoint_status']}")
        applied = host.call(flavor, "apply", None)
        _check(applied["checkpoint_status"] == "applied" and host.calls == [], f"{flavor}: a non-Homebrew interpreter ran brew: {host.calls}")
    with tempfile.TemporaryDirectory() as raw:
        host = _Host(Path(raw), on_request=None)
        probe = host.call(flavor, "probe", "pre_apply")
        _check(_action_ids(probe) == [], f"{flavor}: an unreadable receipt planned a mutation: {_action_ids(probe)}")


def _assert_what_apply_runs_was_previewed(flavor: str, venv: str) -> None:
    """F1: with no venv launcher the preview must still cover the interpreter apply will build the venv from."""

    with tempfile.TemporaryDirectory() as raw:
        host = _Host(Path(raw), on_request=False, venv=venv)
        probe = host.call(flavor, "probe", "pre_apply")
        previewed = _ACTION_ID in _action_ids(probe)
        applied = host.call(flavor, "apply", None)
        _check(applied["checkpoint_status"] == "applied", f"{flavor}/{venv}: apply was not applied: {applied}")
        _check(len(host.tabs()) == 1, f"{flavor}/{venv}: apply did not run the tab exactly once: {host.calls}")
        _check(previewed, f"{flavor}/{venv}: apply ran brew tab but the pre-apply plan never listed it: {_action_ids(probe)}")
        post = host.call(flavor, "probe", "post_apply")
        _check(post["checkpoint_status"] == "verified", f"{flavor}/{venv}: the post-apply probe did not verify: {post['checkpoint_status']}")
    with tempfile.TemporaryDirectory() as raw:
        host = _Host(Path(raw), on_request=True, venv=venv)
        probe = host.call(flavor, "probe", "pre_apply")
        host.call(flavor, "apply", None)
        _check(_ACTION_ID not in _action_ids(probe) and host.tabs() == [], f"{flavor}/{venv}: control: an on-request python was previewed or tabbed: {host.calls}")


def _assert_flag_only_gap_never_reddens_a_required_check(flavor: str) -> None:
    """F2: the doctor's ``completion`` probe stays verified; only a post-apply contradiction is named."""

    with tempfile.TemporaryDirectory() as raw:
        host = _Host(Path(raw), on_request=False)
        completion = host.call(flavor, "probe", "completion")
        _check(completion["checkpoint_status"] == "verified", f"{flavor}: a flag-only gap turned the completion probe {completion['checkpoint_status']}")
        _check(completion["error_kind"] is None, f"{flavor}: a flag-only gap gave the completion probe a reason: {completion['error_kind']}")
        post = host.call(flavor, "probe", "post_apply")
        _check(post["checkpoint_status"] == "blocked", f"{flavor}: a flag still false after apply was not a contradiction: {post['checkpoint_status']}")
        _check(post["error_kind"] == "python_not_installed_on_request", f"{flavor}: the contradiction has the wrong name: {post['error_kind']}")
        _check(host.calls == [], f"{flavor}: probing ran brew: {host.calls}")
    with tempfile.TemporaryDirectory() as raw:
        host = _Host(Path(raw), on_request=True)
        completion = host.call(flavor, "probe", "completion")
        _check(completion["checkpoint_status"] == "verified", f"{flavor}: control: an on-request python failed the completion probe")


def _assert_tab_uses_the_brew_that_owns_the_keg() -> None:
    with tempfile.TemporaryDirectory() as raw:
        host = _Host(Path(raw), on_request=False)
        applied = host.call("existing", "apply", None)
        _check(applied["checkpoint_status"] == "applied", f"two prefixes: apply was not applied: {applied}")
        _check([call[0] for call in host.tabs()] == [str(host.brew)], f"two prefixes: the tab did not run with the keg's own brew: {host.calls}")
        _check(all(call[0] != _PATH_BREW for call in host.calls), f"two prefixes: the PATH brew was used: {host.calls}")
        _check(host.on_request_now() is True, "two prefixes: the keg's receipt is still unmarked")


def _assert_absence_and_failure_are_loud() -> None:
    with tempfile.TemporaryDirectory() as raw:
        host = _Host(Path(raw), on_request=False)
        host.brew_available = False
        applied = host.call("existing", "apply", None)
        _check(applied["checkpoint_status"] == "failed", f"absent Homebrew did not fail: {applied['checkpoint_status']}")
        _check(applied["error_kind"] == "python_on_request_brew_missing", f"absent Homebrew has the wrong name: {applied['error_kind']}")
        _check(host.calls == [], "absent Homebrew still ran a brew command (the PATH brew must not stand in for the keg's own)")
    with tempfile.TemporaryDirectory() as raw:
        host = _Host(Path(raw), on_request=False)
        host.tab_returncode = 1
        applied = host.call("existing", "apply", None)
        _check(applied["error_kind"] == "python_on_request_mark_failed", f"a failed tab has the wrong name: {applied['error_kind']}")
        _check(host.on_request_now() is False, "a failed tab still flipped the receipt")
    with tempfile.TemporaryDirectory() as raw:
        host = _Host(Path(raw), on_request=False)
        with patch("bootstrap_adapter.dependency.homebrew_guard_environment", side_effect=AdapterError("no account home")):
            applied = host.call("existing", "apply", None)
        _check(applied["checkpoint_status"] == "failed", f"a guard-environment failure was not a JSON failure: {applied['checkpoint_status']}")
        _check(applied["error_kind"] == "python_on_request_mark_failed", f"a guard-environment failure has the wrong name: {applied['error_kind']}")
        _check(host.calls == [], "a guard-environment failure still ran brew")


def main() -> int:
    for flavor in ("existing", "create"):
        _assert_plan_names_the_step(flavor)
        _assert_apply_runs_exactly_the_tab(flavor)
        _assert_controls_do_nothing(flavor)
        for venv in ("absent", "dangling", "py312", "failprobe"):
            _assert_what_apply_runs_was_previewed(flavor, venv)
        _assert_flag_only_gap_never_reddens_a_required_check(flavor)
    _assert_tab_uses_the_brew_that_owns_the_keg()
    _assert_absence_and_failure_are_loud()
    print(f"bootstrap_adapter_python_on_request_smoke: {_CHECKS} checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
