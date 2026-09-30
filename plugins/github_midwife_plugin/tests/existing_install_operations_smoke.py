"""Seed-side ``existing::`` handlers (existing-install design sections 3.2, 4.2, 5.1, 6.2, 6.3, 7.6).

Hermetic: a fake ``Runtime`` with a private HOME and a real Git target that
carries the transition bundle and templates.  Legs:

- every row of the section-6.2 three-way table: no marker, stamped previous,
  stamped current, conflict, legacy unstamped matching, legacy unstamped
  unknown, duplicate block, whole-file stamped/modified/unstamped, plist;
- rerun is byte-identical; operator content outside blocks survives
  byte-for-byte; a ``.zshrc`` with two solets' blocks keeps both;
- an adapter that resolves a destination the Manager plan did not name
  refuses (``preserved_surface_write_refused``) and writes nothing;
- the rename migration: fresh pre-boundary fixture applies and verifies;
  already-migrated fixture is verified by probe with byte-identical files;
  "Claude Code running" blocks with ``coding_agent_running``;
- export-root containment is probe-only when unset;
- the plugin cache refresh compares the cache copy against shipped hooks, and is current only when the hooks are
  pinned and the receipt reads back (iss_fa27466f);
- the bootstrap dependency route accepts a declared closure, plans exactly the
  missing declared pieces (never a present one), and applies only those;
- the seed validator accepts ``existing::`` refs only under the
  existing-install flow.
"""

from __future__ import annotations

import hashlib
import json
import os
import plistlib
import subprocess
import sys
import uuid
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, cast

_PLUGIN_ROOT = Path(__file__).resolve().parents[1]
_REPO = _PLUGIN_ROOT.parents[1]
sys.path[:0] = [str(_PLUGIN_ROOT / "src"), str(_REPO)]
from github_midwife_plugin import existing_install_operations as ops  # noqa: E402
from github_midwife_plugin import setup_plugin_operations  # noqa: E402
from github_midwife_plugin.autostart import render_launchagent_plist  # noqa: E402
from github_midwife_plugin.managed_render import marker_lines, sha256_bytes  # noqa: E402
from github_midwife_plugin.setup_adapter import dispatch_request  # noqa: E402
from github_midwife_plugin.setup_adapter_contract import AdapterInputError, AdapterRequest  # noqa: E402
from github_midwife_plugin.setup_adapter_runtime import CommandOutcome, bounded_command_outcome  # noqa: E402

from bootstrap_adapter.routes import execute_adapter_request  # noqa: E402

_KB = _PLUGIN_ROOT / "knowledge_base"
_TEMPLATES = _KB / "hydration_templates"
_CHECKS = 0
_ENV = {**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1"}
_NAME = "iris"


def _check(condition: object, label: str) -> None:
    global _CHECKS
    _CHECKS += 1
    if not condition:
        raise AssertionError(label)


class FakeRuntime:
    def __init__(self, home: Path) -> None:
        self.home = home
        self.commands: list[tuple[str, ...]] = []
        self.writes: list[Path] = []
        self.claude_running = False
        self.launchctl_loaded: set[str] = set()

    def run(self, argv: tuple[str, ...], *, timeout_seconds: int, cwd: Path | None = None, extra_env: dict[str, str] | None = None, input_text: str | None = None, output_limit: int = 4096) -> CommandOutcome:
        del timeout_seconds, input_text
        self.commands.append(argv)
        if argv[0] == "git":
            completed = subprocess.run(argv, cwd=cwd, capture_output=True, text=True, env={**_ENV, **(extra_env or {})}, check=False)
            return bounded_command_outcome(returncode=completed.returncode, timed_out=False, duration_ms=1, stdout=completed.stdout, stderr=completed.stderr, output_limit=output_limit)
        if argv[0] == "/bin/launchctl":
            return self._launchctl(argv)
        if argv[0] == "/fixture/bin/claude":
            return self._claude(argv, output_limit)
        return self._host_probe(argv)

    def _launchctl(self, argv: tuple[str, ...]) -> CommandOutcome:
        if argv[1] != "print":
            return CommandOutcome(0, False, 1, "", "")
        return CommandOutcome(0 if argv[2].rsplit("/", 1)[1] in self.launchctl_loaded else 113, False, 1, "", "")

    @staticmethod
    def _claude(argv: tuple[str, ...], output_limit: int) -> CommandOutcome:
        if argv[1:3] == ("plugin", "list"):
            return bounded_command_outcome(returncode=0, timed_out=False, duration_ms=1, stdout=json.dumps([{"id": f"coordination-hooks@{_NAME}", "enabled": True}]), stderr="", output_limit=output_limit)
        return CommandOutcome(0, False, 1, "", "")

    def _host_probe(self, argv: tuple[str, ...]) -> CommandOutcome:
        if argv[:2] == ("/usr/bin/pgrep", "-x"):
            return CommandOutcome(0 if self.claude_running else 1, False, 1, "", "")
        if argv[:2] == ("/usr/bin/which", "claude"):
            return CommandOutcome(0, False, 1, "/fixture/bin/claude\n", "")
        return CommandOutcome(0, False, 1, "", "")

    def http_json(self, url: str, *, timeout_seconds: int, payload: dict[str, Any] | None = None) -> tuple[int, Any]:
        del url, timeout_seconds, payload
        return 503, None

    def atomic_write(self, path: Path, content: str, *, mode: int) -> None:
        self.writes.append(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        path.chmod(mode)


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(("git", "-C", str(repo), *args), check=True, capture_output=True, text=True, env=_ENV).stdout.strip()


def _digest(name: str) -> str:
    return sha256_bytes((_TEMPLATES / name).read_bytes())


def _bundle(predecessor: str, *, artifacts: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    return {
        "flow_id": "existing-install",
        "schema_version": 1,
        "supported_predecessors": [{"repository": "https://github.com/example/seed.git", "commit": predecessor, "tree": "0" * 40, "provenance_sha256": "1" * 64, "seed_id": str(uuid.uuid4()), "origin_id": str(uuid.uuid4()), "manifest_sha256": "2" * 64, "legacy_anchor_id": None}],
        "source_operation": {"operation_ref": "existing::source.fast_forward", "runner": "manager", "planned_actions": ["manager.acquire_update_candidate", "target.fetch_exact_candidate", "target.fast_forward_exact_candidate"]},
        "runtime_operations": [],
        "managed_artifacts": _default_artifacts() if artifacts is None else artifacts,
        "dependency_closure": {"additions": [], "removals": []},
        "knowledge_removals": [],
        "lifecycle": {"strategy": "router_preferred", "readiness_budget_seconds": 5, "readiness_release_signal": "bridge_health_healthy", "verification_modules": ["ananta.cli"]},
    }


def _default_artifacts() -> list[dict[str, Any]]:
    return [
        {"artifact_id": "instance_launchagent_plist", "kind": "launchd_plist", "logical_destination": "{HOME}/Library/LaunchAgents/local.solet.{NAME}.plist", "preservation_class": "manager_generated_whole", "marker": None, "stamp": "<!-- rendered-from: {TEMPLATE_REF}@{TEMPLATE_DIGEST} -->", "template_ref": "plugins/github_midwife_plugin/knowledge_base/hydration_templates/launchagent.plist.template", "template_digest": _digest("launchagent.plist.template"), "previous_template_digests": [_digest("launchagent.plist.template")]},
        {"artifact_id": "shell_startup_block", "kind": "managed_block", "logical_destination": "{HOME}/.zshrc", "preservation_class": "operator_owned_with_managed_block", "marker": {"begin": "# BEGIN SOLET {NAME} v{TEMPLATE_DIGEST8}", "end": "# END SOLET {NAME}"}, "stamp": None, "template_ref": "plugins/github_midwife_plugin/knowledge_base/hydration_templates/zshrc_block.template", "template_digest": _digest("zshrc_block.template"), "previous_template_digests": [_digest("zshrc_block.template")]},
        {"artifact_id": "user_claude_md_section", "kind": "managed_block", "logical_destination": "{HOME}/.claude/CLAUDE.md", "preservation_class": "operator_owned_with_managed_block", "marker": {"begin": "<!-- BEGIN SOLET {NAME} v{TEMPLATE_DIGEST8} -->", "end": "<!-- END SOLET {NAME} -->"}, "stamp": None, "template_ref": "plugins/github_midwife_plugin/knowledge_base/hydration_templates/user_claude_md_section.template", "template_digest": _digest("user_claude_md_section.template"), "previous_template_digests": [_digest("user_claude_md_section.template")]},
    ]


def _target(root: Path, *, previous_zshrc_body: str | None = None) -> tuple[Path, str]:
    """A Git target whose history carries a predecessor template when asked."""
    target = root / "target"
    target.mkdir(parents=True)
    _git(target, "init", "--quiet", "-b", "main")
    _git(target, "config", "user.name", "Fixture")
    _git(target, "config", "user.email", "fixture@example.invalid")
    templates = target / "plugins/github_midwife_plugin/knowledge_base/hydration_templates"
    templates.mkdir(parents=True)
    if previous_zshrc_body is not None:
        (templates / "zshrc_block.template").write_text(previous_zshrc_body)
        _git(target, "add", "-A")
        _git(target, "commit", "--quiet", "-m", "predecessor")
    predecessor = _git(target, "rev-parse", "HEAD") if previous_zshrc_body is not None else "3" * 40
    for name in ("zshrc_block.template", "user_claude_md_section.template", "launchagent.plist.template"):
        (templates / name).write_bytes((_TEMPLATES / name).read_bytes())
    (target / "plugins/github_midwife_plugin/knowledge_base/existing_install_flow.json").write_text(json.dumps(_bundle(predecessor), indent=2))
    hooks = target / "plugins/github_midwife_plugin/claude_plugin/coordination-hooks/hooks"
    hooks.mkdir(parents=True)
    for name in ("coordination_owner.py", "heartbeat_report_alive.py", "rotation_due_watch.py", "wake_waiter.py", "hooks.json"):
        (hooks / name).write_bytes((_PLUGIN_ROOT / "claude_plugin" / "coordination-hooks" / "hooks" / name).read_bytes())
    _git(target, "add", "-A")
    _git(target, "commit", "--quiet", "-m", "candidate")
    return target, predecessor


def _request(target: Path, ref: str, *, phase: str = "probe", purpose: str | None = "pre_apply", inputs: dict[str, Any] | None = None, flow_id: str = "existing-install") -> AdapterRequest:
    apply = phase == "apply"
    raw = {
        "protocol_version": 1,
        "kind": "operation_request",
        "request_id": str(uuid.uuid4()),
        "operation_id": "fixture_op",
        "operation_ref": ref,
        "phase": phase,
        "probe_purpose": None if apply else purpose,
        "attempt": 1,
        "name": _NAME,
        "target": str(target),
        "flow_id": flow_id,
        "flow_source_revision": "a" * 40,
        "answers_fingerprint": "sha256:" + "b" * 64,
        "approval_fingerprint": "sha256:" + "c" * 64 if apply else None,
        "dry_run": not apply,
        "timeout_seconds": 30,
        "public_inputs": inputs or {},
    }
    return AdapterRequest.from_json(json.dumps(raw))


def _inputs(runtime: FakeRuntime, target: Path, *ids: str) -> dict[str, Any]:
    destinations = {
        "instance_launchagent_plist": str(runtime.home / "Library" / "LaunchAgents" / f"local.solet.{_NAME}.plist"),
        "shell_startup_block": str(runtime.home / ".zshrc"),
        "user_claude_md_section": str(runtime.home / ".claude" / "CLAUDE.md"),
        "feedback_skill": str(runtime.home / ".claude" / "skills" / "feedback" / "SKILL.md"),
        "clone_exclude_block": str(target / ".git" / "info" / "exclude"),
        "fleet_launcher": str(target / "client" / f"{_NAME}-fleet.zsh"),
    }
    return {"artifact_ids": list(ids), "planned_destinations": [f"{item}={destinations[item]}" for item in ids]}


def _state(result: dict[str, Any], artifact_id: str) -> dict[str, str]:
    for item in result["evidence"]:
        if item["id"] == f"artifact.{artifact_id}":
            return dict(pair.split("=", 1) for pair in item["observed"])
    raise AssertionError(f"no evidence for {artifact_id}")


def _probe(target: Path, runtime: FakeRuntime, ref: str, *ids: str, purpose: str = "pre_apply") -> dict[str, Any]:
    return cast(dict[str, Any], dispatch_request(_request(target, ref, purpose=purpose, inputs=_inputs(runtime, target, *ids)), runtime))


def _apply(target: Path, runtime: FakeRuntime, ref: str, *ids: str) -> dict[str, Any]:
    return cast(dict[str, Any], dispatch_request(_request(target, ref, phase="apply", inputs=_inputs(runtime, target, *ids)), runtime))


def _check_managed_block_table(root: Path) -> None:
    target, _ = _target(root)
    runtime = FakeRuntime(root / "home")
    runtime.home.mkdir()
    ref = "existing::hydration.reconcile"
    # no marker -> append
    probe = _probe(target, runtime, ref, "shell_startup_block")
    state = _state(probe, "shell_startup_block")
    _check((probe["checkpoint_status"], state["state"], state["action"]) == ("pending", "absent", "append_block"), "no marker: append")
    applied = _apply(target, runtime, ref, "shell_startup_block")
    _check(applied["checkpoint_status"] == "applied", "append applied")
    zshrc = runtime.home / ".zshrc"
    written = zshrc.read_text()
    begin, end = marker_lines("# BEGIN SOLET {NAME} v{TEMPLATE_DIGEST8}", "# END SOLET {NAME}", _NAME, _digest("zshrc_block.template"))
    shape = (written.startswith(begin + "\n"), written.rstrip("\n").endswith(end), f"'{target}/client/{_NAME}.zsh'" in written)
    _check(shape == (True, True, True), "versioned marker and rendered body")
    _check(sha256_bytes(written.encode()) == state["expected_sha256"], "pre-apply expected digest equals the written file")
    # stamped current -> verified, byte-identical rerun
    again = _probe(target, runtime, ref, "shell_startup_block", purpose="post_apply")
    _check((again["checkpoint_status"], _state(again, "shell_startup_block")["state"]) == ("verified", "stamped_current"), "stamped current: verified")
    _apply(target, runtime, ref, "shell_startup_block")
    _check(zshrc.read_text() == written, "rerun is byte-identical")
    # operator content outside the block survives; a second solet's block is kept
    other_begin, other_end = marker_lines("# BEGIN SOLET {NAME} v{TEMPLATE_DIGEST8}", "# END SOLET {NAME}", "other", _digest("zshrc_block.template"))
    zshrc.write_text("export PATH=$HOME/bin:$PATH\n" + f"{other_begin}\nsource other\n{other_end}\n" + written + "alias ll='ls -l'\n")
    before = zshrc.read_text()
    _apply(target, runtime, ref, "shell_startup_block")
    _check(zshrc.read_text() == before, "operator lines and another solet's block survive byte-for-byte")
    _check_managed_block_conflicts(target, runtime, written)
    _check_claude_md_section(target, runtime)


def _check_managed_block_conflicts(target: Path, runtime: FakeRuntime, written: str) -> None:
    ref = "existing::hydration.reconcile"
    zshrc = runtime.home / ".zshrc"
    # conflict: stamped marker with edited body
    edited = written.replace("source", "source  # edited")
    zshrc.write_text(edited)
    conflict = _probe(target, runtime, ref, "shell_startup_block")
    _check((conflict["checkpoint_status"], conflict["error_kind"]) == ("blocked", "managed_block_conflict"), "edited stamped block is a conflict")
    refused = _apply(target, runtime, ref, "shell_startup_block")
    _check((refused["checkpoint_status"], zshrc.read_text()) == ("blocked", edited), "conflict never picks a side")
    # duplicate blocks
    zshrc.write_text(written + written)
    duplicate = _probe(target, runtime, ref, "shell_startup_block")
    _check(duplicate["error_kind"] == "duplicate_managed_block", "two blocks for one name is refused")
    # legacy unstamped matching -> upgrade to stamped
    zshrc.write_text(f"# BEGIN SOLET {_NAME}\n[ -f '{target}/client/{_NAME}.zsh' ] && source '{target}/client/{_NAME}.zsh'\n# END SOLET {_NAME}\n")
    legacy = _state(_probe(target, runtime, ref, "shell_startup_block"), "shell_startup_block")
    _check((legacy["state"], legacy["action"]) == ("legacy_matched", "replace_block"), "legacy unversioned block recognised as the previous render")
    _apply(target, runtime, ref, "shell_startup_block")
    _check(zshrc.read_text() == written, "legacy block upgraded to the stamped candidate")
    # legacy unstamped unknown -> blocked
    zshrc.write_text(f"# BEGIN SOLET {_NAME}\nsomething else entirely\n# END SOLET {_NAME}\n")
    unknown = _probe(target, runtime, ref, "shell_startup_block")
    _check((unknown["error_kind"], "remove the block" in unknown["repair"]) == ("managed_block_unknown_origin", True), "unknown-origin block blocks with the repair")


def _check_claude_md_section(target: Path, runtime: FakeRuntime) -> None:
    """HTML-comment markers for the user CLAUDE.md section: legacy v1 -> upgrade in place."""
    ref = "existing::hydration.reconcile"
    claude = runtime.home / ".claude" / "CLAUDE.md"
    claude.parent.mkdir()
    legacy_section = (_TEMPLATES / "user_claude_md_section.template").read_text().replace("{{SOLET_NAME}}", _NAME)
    claude.write_text("# my instructions\n\n" + legacy_section)
    section = _probe(target, runtime, ref, "user_claude_md_section")
    _check(_state(section, "user_claude_md_section")["state"] == "legacy_matched", "legacy v1 CLAUDE.md section recognised")
    _apply(target, runtime, ref, "user_claude_md_section")
    content = claude.read_text()
    shape = (content.startswith("# my instructions\n\n<!-- BEGIN SOLET iris v"), content.rstrip().endswith("<!-- END SOLET iris -->"), content.count("BEGIN SOLET"))
    _check(shape == (True, True, 1), "section replaced in place under the versioned marker")


def _check_stamped_previous(root: Path) -> None:
    """A block stamped with a predecessor template digest is replaced when its body is that render."""
    previous_body = "[ -f {{SHELL_FILE_ZSH}} ] && source {{SHELL_FILE_ZSH}}  # v0\n"
    target, predecessor = _target(root, previous_zshrc_body=previous_body)
    previous_digest = sha256_bytes(previous_body.encode())
    artifacts = _default_artifacts()
    artifacts[1]["previous_template_digests"] = [previous_digest]
    (target / "plugins/github_midwife_plugin/knowledge_base/existing_install_flow.json").write_text(json.dumps(_bundle(predecessor, artifacts=artifacts), indent=2))
    runtime = FakeRuntime(root / "home")
    runtime.home.mkdir()
    begin, end = marker_lines("# BEGIN SOLET {NAME} v{TEMPLATE_DIGEST8}", "# END SOLET {NAME}", _NAME, previous_digest)
    (runtime.home / ".zshrc").write_text(f"{begin}\n[ -f '{target}/client/{_NAME}.zsh' ] && source '{target}/client/{_NAME}.zsh'  # v0\n{end}\n")
    probe = _probe(target, runtime, "existing::hydration.reconcile", "shell_startup_block")
    _check(_state(probe, "shell_startup_block")["state"] == "stamped_previous" and _state(probe, "shell_startup_block")["stamped_digest"] == previous_digest, "stamped previous render found through predecessor history")
    _apply(target, runtime, "existing::hydration.reconcile", "shell_startup_block")
    _check("# v0" not in (runtime.home / ".zshrc").read_text() and _digest("zshrc_block.template").removeprefix("sha256:")[:8] in (runtime.home / ".zshrc").read_text(), "replaced in place with the candidate")


def _check_whole_file_and_plist(root: Path) -> None:
    target, _ = _target(root)
    runtime = FakeRuntime(root / "home")
    (runtime.home / "Library" / "LaunchAgents").mkdir(parents=True)
    plist = runtime.home / "Library" / "LaunchAgents" / f"local.solet.{_NAME}.plist"
    ref = "existing::autostart.reconcile"
    absent = _probe(target, runtime, ref, "instance_launchagent_plist")
    _check(_state(absent, "instance_launchagent_plist")["state"] == "absent" and _state(absent, "instance_launchagent_plist")["action"] == "render_whole", "no plist: render")
    _apply(target, runtime, ref, "instance_launchagent_plist")
    rendered = plist.read_bytes()
    _check(rendered == render_launchagent_plist(_NAME, target, runtime.home, template_text=None, stamp=None, stamped=True), "plist render equals genesis's stamped render")
    _check(plistlib.loads(rendered)["Label"] == f"local.solet.{_NAME}", "stamped plist still parses")
    current = _probe(target, runtime, ref, "instance_launchagent_plist", purpose="post_apply")
    _check(current["checkpoint_status"] == "verified" and _state(current, "instance_launchagent_plist")["expected_sha256"] == sha256_bytes(rendered), "post-apply digest equals the expectation")
    plist.write_bytes(render_launchagent_plist(_NAME, target, runtime.home, template_text=None, stamp=None, stamped=False))
    legacy = _probe(target, runtime, ref, "instance_launchagent_plist")
    _check(_state(legacy, "instance_launchagent_plist")["state"] == "legacy_matched", "genesis-era unstamped plist recognised")
    plist.write_bytes(rendered.replace(b"<true/>", b"<false/>", 1))
    modified = _probe(target, runtime, ref, "instance_launchagent_plist")
    _check(modified["error_kind"] == "managed_file_locally_modified", "stamped plist with edited bytes is locally modified")
    plist.write_bytes(b"<plist/>\n")
    unknown = _probe(target, runtime, ref, "instance_launchagent_plist")
    _check(unknown["error_kind"] == "managed_block_unknown_origin", "unstamped unknown plist blocks")
    # wrong kind through the hydration handler, and an unplanned destination
    wrong = cast(dict[str, Any], dispatch_request(_request(target, "existing::hydration.reconcile", inputs=_inputs(runtime, target, "instance_launchagent_plist")), runtime))
    _check(wrong["error_kind"] == "adapter_protocol_error", "the plist is not a hydration.reconcile artifact")
    inputs = _inputs(runtime, target, "shell_startup_block")
    inputs["planned_destinations"] = [f"shell_startup_block={runtime.home}/.zshrc.other"]
    unplanned = cast(dict[str, Any], dispatch_request(_request(target, "existing::hydration.reconcile", phase="apply", inputs=inputs), runtime))
    _check(unplanned["error_kind"] == "preserved_surface_write_refused" and not (runtime.home / ".zshrc").exists(), "a destination the plan did not name is refused with no write")


_SKILL_TEMPLATE = "feedback_skill_SKILL.md.template"
_SKILL_REF = f"plugins/github_midwife_plugin/knowledge_base/hydration_templates/{_SKILL_TEMPLATE}"
_SKILL_STAMP = "<!-- rendered-from: {TEMPLATE_REF}@{TEMPLATE_DIGEST} -->"
_R56_SKILL = _PLUGIN_ROOT / "tests" / "fixtures" / "hydration_predecessors" / "feedback_skill_r56.fixture"


def _skill_artifact(previous: list[str]) -> dict[str, Any]:
    """The record the shipped bundle must declare for the feedback skill (whole file, user scope, refresh-only)."""
    return {"artifact_id": "feedback_skill", "kind": "rendered_whole", "logical_destination": "{HOME}/.claude/skills/feedback/SKILL.md", "preservation_class": "manager_generated_whole", "marker": None, "stamp": _SKILL_STAMP, "template_ref": _SKILL_REF, "template_digest": _digest(_SKILL_TEMPLATE), "previous_template_digests": previous}


def _skill_render(template: bytes, digest: str | None) -> str:
    """The feedback skill as genesis (``digest is None``) or a refresh renders it: the stamp sits AFTER the front matter."""
    body = template.decode("utf-8").replace("{{SOLET_NAME}}", _NAME)
    if digest is None:
        return body
    head, separator, rest = body.partition("\n---\n")
    return f"{head}{separator}{_SKILL_STAMP.replace('{TEMPLATE_REF}', _SKILL_REF).replace('{TEMPLATE_DIGEST}', digest)}\n{rest}"


def _skill_target(root: Path) -> Path:
    """A Git target whose predecessor commit carries the real r56 feedback template and whose candidate declares the skill."""
    target = root / "target"
    templates = target / "plugins/github_midwife_plugin/knowledge_base/hydration_templates"
    templates.mkdir(parents=True)
    _git(target, "init", "--quiet", "-b", "main")
    _git(target, "config", "user.name", "Fixture")
    _git(target, "config", "user.email", "fixture@example.invalid")
    (templates / _SKILL_TEMPLATE).write_bytes(_R56_SKILL.read_bytes())
    _git(target, "add", "-A")
    _git(target, "commit", "--quiet", "-m", "predecessor")
    predecessor = _git(target, "rev-parse", "HEAD")
    (templates / _SKILL_TEMPLATE).write_bytes((_TEMPLATES / _SKILL_TEMPLATE).read_bytes())
    bundle = _bundle(predecessor, artifacts=[_skill_artifact([sha256_bytes(_R56_SKILL.read_bytes())])])
    (target / "plugins/github_midwife_plugin/knowledge_base/existing_install_flow.json").write_text(json.dumps(bundle, indent=2))
    _git(target, "add", "-A")
    _git(target, "commit", "--quiet", "-m", "candidate")
    return target


def _check_feedback_skill_declared() -> None:
    """The shipped bundle declares the skill exactly as the refresh contract needs, with the r56 digest as its predecessor."""
    shipped = {row["artifact_id"]: row for row in json.loads((_KB / "existing_install_flow.json").read_text())["managed_artifacts"]}
    _check("feedback_skill" in shipped, "the shipped bundle declares the feedback skill")
    _check(shipped["feedback_skill"] == _skill_artifact([sha256_bytes(_R56_SKILL.read_bytes())]), "declared record: user-scope whole file, front-matter-safe stamp, r56 digest as previous")
    _check(b"--label defect" in _R56_SKILL.read_bytes() and b"--label defect" not in (_TEMPLATES / _SKILL_TEMPLATE).read_bytes(), "the r56 fixture is the broken skill and the shipped template is not")


def _check_feedback_skill_refresh(root: Path) -> None:
    target = _skill_target(root)
    runtime = FakeRuntime(root / "home")
    skill = runtime.home / ".claude" / "skills" / "feedback" / "SKILL.md"
    ref = "existing::hydration.reconcile"
    current, previous = (_TEMPLATES / _SKILL_TEMPLATE).read_bytes(), _R56_SKILL.read_bytes()
    absent = _probe(target, runtime, ref, "feedback_skill")
    _check((absent["checkpoint_status"], _state(absent, "feedback_skill")["state"], _state(absent, "feedback_skill")["action"]) == ("verified", "absent", "none"), "a missing skill is not created: refresh-only")
    _apply(target, runtime, ref, "feedback_skill")
    _check(not skill.exists(), "apply on a missing skill writes nothing")
    _check_skill_legacy_replaced(target, runtime, skill, ref, current, previous)
    _check_skill_stamped_previous(target, runtime, skill, ref, current, previous)
    _check_skill_edited_is_left(target, runtime, skill, ref, current, previous)


def _check_skill_legacy_replaced(target: Path, runtime: FakeRuntime, skill: Path, ref: str, current: bytes, previous: bytes) -> None:
    skill.parent.mkdir(parents=True)
    skill.write_text(_skill_render(previous, None))
    skill.chmod(0o640)
    _check("--label defect" in skill.read_text(), "fixture: the installed skill carries the defective --label step")
    probe = _probe(target, runtime, ref, "feedback_skill")
    row = _state(probe, "feedback_skill")
    _check((probe["checkpoint_status"], row["state"], row["action"]) == ("pending", "legacy_matched", "render_whole"), "the genesis-era r56 skill is recognised as the previous render and planned for replacement")
    planned = [(action["id"], action["target"]) for action in probe["planned_actions"]]
    _check(planned == [("hydrate.feedback_skill", str(skill))], f"the dry-run plan names the skill write: {planned}")
    _check(skill.read_text() == _skill_render(previous, None), "probe writes nothing")
    _check(_apply(target, runtime, ref, "feedback_skill")["checkpoint_status"] == "applied", "apply succeeds")
    refreshed = skill.read_text()
    _check(refreshed == _skill_render(current, _digest(_SKILL_TEMPLATE)), "the skill is now the stamped candidate render")
    _check(refreshed.startswith("---\nname: feedback\n") and "--label defect" not in refreshed and "issues/$PARENT_NUMBER/sub_issues" not in refreshed, "front matter still first; no --label step, no sub-issues POST")
    _check(skill.stat().st_mode & 0o777 == 0o640, "the file mode survives the refresh")
    again = _probe(target, runtime, ref, "feedback_skill", purpose="post_apply")
    _check((again["checkpoint_status"], _state(again, "feedback_skill")["state"], _state(again, "feedback_skill")["expected_sha256"]) == ("verified", "stamped_current", sha256_bytes(refreshed.encode())), "post-apply the skill reads back as stamped current")
    writes = len(runtime.writes)
    _apply(target, runtime, ref, "feedback_skill")
    _check((skill.read_text(), len(runtime.writes)) == (refreshed, writes), "a second update rewrites nothing")


def _check_skill_stamped_previous(target: Path, runtime: FakeRuntime, skill: Path, ref: str, current: bytes, previous: bytes) -> None:
    previous_digest = sha256_bytes(previous)
    skill.write_text(_skill_render(previous, previous_digest))
    row = _state(_probe(target, runtime, ref, "feedback_skill"), "feedback_skill")
    _check((row["state"], row["action"], row["stamped_digest"]) == ("stamped_previous", "render_whole", previous_digest), "a skill stamped with the r56 digest is a previous render")
    _apply(target, runtime, ref, "feedback_skill")
    _check(skill.read_text() == _skill_render(current, _digest(_SKILL_TEMPLATE)), "a stamped previous render is replaced by the candidate")


def _check_skill_edited_is_left(target: Path, runtime: FakeRuntime, skill: Path, ref: str, current: bytes, previous: bytes) -> None:
    """An operator-edited skill is reported and left byte-for-byte; it never blocks the update."""
    for label, edited, expected in (
        ("unstamped edit of the r56 render", _skill_render(previous, None) + "\nAlways file as urgent.\n", "unknown_origin"),
        ("stamped edit of the current render", _skill_render(current, _digest(_SKILL_TEMPLATE)) + "\nAlways file as urgent.\n", "locally_modified"),
    ):
        skill.write_text(edited)
        writes = len(runtime.writes)
        probe = _probe(target, runtime, ref, "feedback_skill")
        row = _state(probe, "feedback_skill")
        _check((probe["checkpoint_status"], row["state"], row["action"], row["conflict"]) == ("verified", expected, "none", "none"), f"{label}: reported as {expected}, no action, no conflict")
        applied = _apply(target, runtime, ref, "feedback_skill")
        _check((applied["checkpoint_status"], skill.read_text(), len(runtime.writes)) == ("applied", edited, writes), f"{label}: left byte-for-byte")


_FLEET_TEMPLATE = "fleet_functions.zsh.template"
_FLEET_REF = f"plugins/github_midwife_plugin/knowledge_base/hydration_templates/{_FLEET_TEMPLATE}"
_FLEET_STAMP = "# rendered-from: {TEMPLATE_REF}@{TEMPLATE_DIGEST}"
_FLEET_MARKER = "# One function per role the operator chose in Step 4a."
_R56_FLEET = _PLUGIN_ROOT / "tests" / "fixtures" / "hydration_predecessors" / "fleet_functions_r56.fixture"
_EXCLUDE_TEMPLATE = "clone_exclude_block.template"
_ROLE_FUNCTIONS = "\nclaude-iris-lead() { _claude_for_iris Lead }\nclaude-iris-restart-lead() {\n  tmux kill-session -t =Lead 2>/dev/null\n  _claude_for_iris Lead\n}\n"


def _fleet_artifact(previous: list[str]) -> dict[str, Any]:
    """The record the shipped bundle must declare for the fleet launcher: refreshed above the role-function line only."""
    return {"artifact_id": "fleet_launcher", "kind": "rendered_whole", "logical_destination": "{TARGET}/client/{NAME}-fleet.zsh", "preservation_class": "operator_owned_with_managed_block", "marker": None, "stamp": _FLEET_STAMP, "section_end": _FLEET_MARKER, "template_ref": _FLEET_REF, "template_digest": _digest(_FLEET_TEMPLATE), "previous_template_digests": previous}


def _exclude_artifact() -> dict[str, Any]:
    """The record the shipped bundle must declare for the clone's local ignore file."""
    return {"artifact_id": "clone_exclude_block", "kind": "managed_block", "logical_destination": "{TARGET}/.git/info/exclude", "preservation_class": "operator_owned_with_managed_block", "marker": {"begin": "# BEGIN SOLET {NAME} v{TEMPLATE_DIGEST8}", "end": "# END SOLET {NAME}"}, "stamp": None, "template_ref": f"plugins/github_midwife_plugin/knowledge_base/hydration_templates/{_EXCLUDE_TEMPLATE}", "template_digest": _digest(_EXCLUDE_TEMPLATE), "previous_template_digests": []}


def _fleet_render(template: bytes, digest: str | None, controller: str) -> str:
    """The fleet file as hydration renders it: tokens replaced literally, the stamp first when ``digest`` is given."""
    body = template.decode("utf-8").replace("{{SOLET_NAME}}", _NAME).replace("{{GIT_CONTROLLER_NAME}}", controller)
    if digest is None:
        return body
    return _FLEET_STAMP.replace("{TEMPLATE_REF}", _FLEET_REF).replace("{TEMPLATE_DIGEST}", digest) + "\n" + body


def _refreshed(candidate: str, original: str) -> str:
    """What a refresh must produce: the candidate's launcher section, then the installed file from the marker line on, untouched."""
    return candidate.partition(_FLEET_MARKER)[0] + _FLEET_MARKER + original.partition(_FLEET_MARKER)[2]


def _fleet_target(root: Path) -> Path:
    """A Git target whose predecessor commit carries the real r56 fleet template and whose candidate declares both shipped in-clone artifacts."""
    target = root / "target"
    templates = target / "plugins/github_midwife_plugin/knowledge_base/hydration_templates"
    templates.mkdir(parents=True)
    _git(target, "init", "--quiet", "-b", "main")
    _git(target, "config", "user.name", "Fixture")
    _git(target, "config", "user.email", "fixture@example.invalid")
    (templates / _FLEET_TEMPLATE).write_bytes(_R56_FLEET.read_bytes())
    _git(target, "add", "-A")
    _git(target, "commit", "--quiet", "-m", "predecessor")
    predecessor = _git(target, "rev-parse", "HEAD")
    for name in (_FLEET_TEMPLATE, _EXCLUDE_TEMPLATE):
        (templates / name).write_bytes((_TEMPLATES / name).read_bytes())
    bundle = _bundle(predecessor, artifacts=[_exclude_artifact(), _fleet_artifact([sha256_bytes(_R56_FLEET.read_bytes())])])
    (target / "plugins/github_midwife_plugin/knowledge_base/existing_install_flow.json").write_text(json.dumps(bundle, indent=2))
    _git(target, "add", "-A")
    _git(target, "commit", "--quiet", "-m", "candidate")
    return target


def _check_fleet_declared() -> None:
    """The shipped bundle declares the exclude block first and the fleet launcher with the r56 digest as its predecessor."""
    rows = json.loads((_KB / "existing_install_flow.json").read_text())["managed_artifacts"]
    shipped = {row["artifact_id"]: row for row in rows}
    _check(shipped.get("clone_exclude_block") == _exclude_artifact(), "declared record: the clone's local ignore file as a managed block")
    _check(shipped.get("fleet_launcher") == _fleet_artifact([sha256_bytes(_R56_FLEET.read_bytes())]), "declared record: the fleet launcher, refreshed above the role-function line, r56 digest as previous")
    _check([row["artifact_id"] for row in rows].index("clone_exclude_block") < [row["artifact_id"] for row in rows].index("fleet_launcher"), "the exclude block is written before the file it ignores")
    _check(b"_tmux_host_for_" not in _R56_FLEET.read_bytes() and b"_tmux_host_for_" in (_TEMPLATES / _FLEET_TEMPLATE).read_bytes(), "the r56 fixture is the launcher without tmux hosting and the shipped template is not")
    _check((_R56_FLEET.read_bytes().count(_FLEET_MARKER.encode()), (_TEMPLATES / _FLEET_TEMPLATE).read_bytes().count(_FLEET_MARKER.encode())) == (1, 1), "the section marker occurs once in the r56 and the shipped template")


def _check_fleet_refresh(root: Path) -> None:
    target = _fleet_target(root)
    runtime = FakeRuntime(root / "home")
    fleet = target / "client" / f"{_NAME}-fleet.zsh"
    ref = "existing::hydration.reconcile"
    current, previous = (_TEMPLATES / _FLEET_TEMPLATE).read_bytes(), _R56_FLEET.read_bytes()
    absent = _probe(target, runtime, ref, "fleet_launcher")
    row = _state(absent, "fleet_launcher")
    _check((absent["checkpoint_status"], row["state"], row["action"], row["conflict"]) == ("verified", "absent", "none", "none"), "no fleet file: reported absent, no action, no conflict")
    writes = len(runtime.writes)
    _apply(target, runtime, ref, "fleet_launcher")
    _check((fleet.exists(), len(runtime.writes)) == (False, writes), "no fleet file: apply creates nothing")
    _check_fleet_legacy_replaced(target, runtime, fleet, ref, current, previous)
    _check_fleet_stamped_previous(target, runtime, fleet, ref, current, previous)
    _check_fleet_edited_is_left(target, runtime, fleet, ref, current, previous)


def _check_fleet_legacy_replaced(target: Path, runtime: FakeRuntime, fleet: Path, ref: str, current: bytes, previous: bytes) -> None:
    fleet.parent.mkdir(parents=True)
    original = _fleet_render(previous, None, "Lead-Git") + _ROLE_FUNCTIONS
    fleet.write_text(original)
    fleet.chmod(0o640)
    controller_line = '  GIT_CONTROLLER_NAME="Lead-Git" \\'
    _check(controller_line in original.splitlines() and "_tmux_host_for_" not in original, "fixture: the installed r56 launcher has the operator's controller and role functions but no tmux hosting")
    probe = _probe(target, runtime, ref, "fleet_launcher")
    row = _state(probe, "fleet_launcher")
    _check((probe["checkpoint_status"], row["state"], row["action"], row["conflict"]) == ("pending", "legacy_matched", "render_whole", "none"), "an r56 launcher with custom role functions is recognised as the previous render")
    _check([(action["id"], action["target"]) for action in probe["planned_actions"]] == [("hydrate.fleet_launcher", str(fleet))], "the dry-run plan names the fleet file write")
    _check(fleet.read_text() == original, "probe writes nothing")
    _check(_apply(target, runtime, ref, "fleet_launcher")["checkpoint_status"] == "applied", "apply succeeds")
    refreshed = fleet.read_text()
    _check(refreshed == _refreshed(_fleet_render(current, _digest(_FLEET_TEMPLATE), "Lead-Git"), original), "the launcher section is the stamped candidate render, the operator's tail is unchanged")
    _check(refreshed.count(_FLEET_MARKER) == 1 and refreshed.partition(_FLEET_MARKER)[2] == original.partition(_FLEET_MARKER)[2], "role functions and everything below the marker are byte-identical")
    _check(controller_line in refreshed.splitlines() and "_tmux_host_for_iris" in refreshed, "the operator's GIT_CONTROLLER_NAME line is unchanged and the launcher now hosts each role in tmux")
    _check(fleet.stat().st_mode & 0o777 == 0o640, "the file mode survives the refresh")
    again = _probe(target, runtime, ref, "fleet_launcher", purpose="post_apply")
    _check((again["checkpoint_status"], _state(again, "fleet_launcher")["state"], _state(again, "fleet_launcher")["expected_sha256"]) == ("verified", "stamped_current", sha256_bytes(refreshed.encode())), "post-apply the launcher reads back as stamped current")
    writes = len(runtime.writes)
    _apply(target, runtime, ref, "fleet_launcher")
    _check((fleet.read_text(), len(runtime.writes)) == (refreshed, writes), "a second update rewrites nothing")


def _check_fleet_stamped_previous(target: Path, runtime: FakeRuntime, fleet: Path, ref: str, current: bytes, previous: bytes) -> None:
    previous_digest = sha256_bytes(previous)
    original = _fleet_render(previous, previous_digest, "Git-Controller") + _ROLE_FUNCTIONS
    fleet.write_text(original)
    row = _state(_probe(target, runtime, ref, "fleet_launcher"), "fleet_launcher")
    _check((row["state"], row["action"], row["stamped_digest"]) == ("stamped_previous", "render_whole", previous_digest), "a launcher stamped with the r56 digest is a previous render")
    _apply(target, runtime, ref, "fleet_launcher")
    _check(fleet.read_text() == _refreshed(_fleet_render(current, _digest(_FLEET_TEMPLATE), "Git-Controller"), original), "a stamped previous launcher is replaced above the marker only")
    solo = _fleet_render(previous, None, "") + _ROLE_FUNCTIONS
    fleet.write_text(solo)
    _check(_state(_probe(target, runtime, ref, "fleet_launcher"), "fleet_launcher")["state"] == "legacy_matched", "a blank controller choice is kept as chosen and still matches")


def _check_fleet_edited_is_left(target: Path, runtime: FakeRuntime, fleet: Path, ref: str, current: bytes, previous: bytes) -> None:
    """An edited launcher section is reported and left byte-for-byte; it never blocks the update."""
    r56_text = _fleet_render(previous, None, "Lead-Git")
    current_text = _fleet_render(current, _digest(_FLEET_TEMPLATE), "Lead-Git")
    for label, edited, expected in (
        ("unstamped edit of the r56 launcher section", r56_text.replace("_claude_for_iris() {", "export MY_EDIT=1\n_claude_for_iris() {", 1) + _ROLE_FUNCTIONS, "unknown_origin"),
        ("stamped edit of the current launcher section", current_text.replace("_claude_for_iris() {", "export MY_EDIT=1\n_claude_for_iris() {", 1) + _ROLE_FUNCTIONS, "locally_modified"),
        ("launcher whose marker line was deleted", r56_text.replace(_FLEET_MARKER, "# roles") + _ROLE_FUNCTIONS, "unknown_origin"),
        ("launcher whose controller line was deleted", "\n".join(line for line in r56_text.splitlines() if "GIT_CONTROLLER_NAME=" not in line) + "\n" + _ROLE_FUNCTIONS, "unknown_origin"),
    ):
        fleet.write_text(edited)
        writes = len(runtime.writes)
        probe = _probe(target, runtime, ref, "fleet_launcher")
        row = _state(probe, "fleet_launcher")
        _check((probe["checkpoint_status"], row["state"], row["action"], row["conflict"]) == ("verified", expected, "none", "none"), f"{label}: reported as {expected}, no action, no conflict")
        applied = _apply(target, runtime, ref, "fleet_launcher")
        _check((applied["checkpoint_status"], fleet.read_text(), len(runtime.writes)) == ("applied", edited, writes), f"{label}: left byte-for-byte")
    doubled = _ROLE_FUNCTIONS + _FLEET_MARKER + "\n# a copied marker inside the operator's own functions\n"
    _check(ops._split_section(current_text + doubled, _FLEET_MARKER) == (current_text.partition(_FLEET_MARKER)[0], _FLEET_MARKER + current_text.partition(_FLEET_MARKER)[2] + doubled), "a duplicated marker splits at its first occurrence")  # noqa: SLF001
    fleet.write_text(r56_text + doubled)
    _apply(target, runtime, ref, "fleet_launcher")
    _check(fleet.read_text() == _refreshed(current_text, r56_text + doubled), "a marker copied into the role functions is kept with them, byte-for-byte")
    tail_only = current_text + _ROLE_FUNCTIONS + "alias extra='echo more'\n"
    fleet.write_text(tail_only)
    row = _state(_probe(target, runtime, ref, "fleet_launcher"), "fleet_launcher")
    _check((row["state"], row["action"]) == ("stamped_current", "none"), "operator additions below the marker never make the section stale")


def _check_clone_exclude(root: Path) -> None:
    """The local ignore file gains one versioned block, keeps every other line, and then ignores the client directory."""
    target = _fleet_target(root)
    runtime = FakeRuntime(root / "home")
    ref = "existing::hydration.reconcile"
    exclude = target / ".git" / "info" / "exclude"
    original = exclude.read_text()
    _check(subprocess.run(("git", "-C", str(target), "check-ignore", "-q", "--no-index", "--", f"client/{_NAME}-fleet.zsh"), env=_ENV, check=False).returncode == 1, "fixture: the clone does not ignore client/")
    probe = _probe(target, runtime, ref, "clone_exclude_block")
    row = _state(probe, "clone_exclude_block")
    _check((probe["checkpoint_status"], row["state"], row["action"]) == ("pending", "absent", "append_block"), "no block: the plan appends one")
    _check([(action["id"], action["target"]) for action in probe["planned_actions"]] == [("hydrate.clone_exclude_block", str(exclude))], "the dry-run plan names the ignore file write")
    _check(exclude.read_text() == original, "probe writes nothing")
    _apply(target, runtime, ref, "clone_exclude_block")
    written = exclude.read_text()
    begin, end = marker_lines("# BEGIN SOLET {NAME} v{TEMPLATE_DIGEST8}", "# END SOLET {NAME}", _NAME, _digest(_EXCLUDE_TEMPLATE))
    _check(written.startswith(original) and f"{begin}\n" in written and "\nclient/\n" in written and written.rstrip("\n").endswith(end), "the block is appended under the versioned marker and the existing lines are untouched")
    _check(subprocess.run(("git", "-C", str(target), "check-ignore", "-q", "--no-index", "--", f"client/{_NAME}-fleet.zsh"), env=_ENV, check=False).returncode == 0, "the clone now ignores the fleet file")
    again = _probe(target, runtime, ref, "clone_exclude_block", purpose="post_apply")
    _check((again["checkpoint_status"], _state(again, "clone_exclude_block")["state"]) == ("verified", "stamped_current"), "post-apply the block reads back as stamped current")
    writes = len(runtime.writes)
    exclude.write_text(written + "profile/local/\n")
    _apply(target, runtime, ref, "clone_exclude_block")
    _check((exclude.read_text(), len(runtime.writes)) == (written + "profile/local/\n", writes), "an operator line after the block survives and a second update rewrites nothing")


def _check_rename_migration(root: Path) -> None:
    target, _ = _target(root)
    runtime = FakeRuntime(root / "home")
    (runtime.home / "Library" / "LaunchAgents").mkdir(parents=True)
    ref = "existing::migration.solet_rename"
    clean = cast(dict[str, Any], dispatch_request(_request(target, ref, purpose="post_apply"), runtime))
    _check(clean["checkpoint_status"] == "verified", "already-migrated install verifies by probe")
    old_plist = runtime.home / "Library" / "LaunchAgents" / f"local.homunculus.{_NAME}.plist"
    old_plist.write_bytes(plistlib.dumps({"Label": f"local.homunculus.{_NAME}", "ProgramArguments": [str(target / ".venv/bin/python3"), "-m", "ananta.cli", "--homunculus", _NAME], "EnvironmentVariables": {"HOMUNCULUS_NAME": _NAME}}))
    (runtime.home / ".zshrc").write_text('AGENT_WAKE_CLI="homunculus"\n')
    (target / "root_manifest.yaml").write_text("homunculus_name: iris\n")
    (runtime.home / ".claude.json").write_text(json.dumps({"mcpServers": {"iris": {"env": {"HOMUNCULUS_NAME": "iris"}}}}))
    stale = cast(dict[str, Any], dispatch_request(_request(target, ref), runtime))
    targets = {action["target"] for action in stale["planned_actions"]}
    expected_targets = {str(old_plist), str(runtime.home / ".zshrc"), str(target / "root_manifest.yaml"), str(runtime.home / ".claude.json")}
    _check((stale["checkpoint_status"], targets) == ("pending", expected_targets), f"pre-boundary fixture plans every stale surface: {targets}")
    runtime.claude_running = True
    blocked = cast(dict[str, Any], dispatch_request(_request(target, ref, phase="apply"), runtime))
    _check((blocked["error_kind"], old_plist.exists()) == ("coding_agent_running", True), "Claude Code running blocks the apply before any write")
    runtime.claude_running = False
    _check_rename_applied(target, runtime, old_plist)


def _check_rename_applied(target: Path, runtime: FakeRuntime, old_plist: Path) -> None:
    ref = "existing::migration.solet_rename"
    applied = cast(dict[str, Any], dispatch_request(_request(target, ref, phase="apply"), runtime))
    _check(applied["checkpoint_status"] == "applied", "rename applied")
    new_plist = runtime.home / "Library" / "LaunchAgents" / f"local.solet.{_NAME}.plist"
    payload = plistlib.loads(new_plist.read_bytes())
    relabelled = (old_plist.exists(), payload["Label"], payload["EnvironmentVariables"], "--solet" in payload["ProgramArguments"])
    _check(relabelled == (False, f"local.solet.{_NAME}", {"SOLET_NAME": _NAME}, True), "plist relabelled, env and args renamed")
    migrated = ((runtime.home / ".zshrc").read_text(), (target / "root_manifest.yaml").read_text(), json.loads((runtime.home / ".claude.json").read_text())["mcpServers"]["iris"]["env"])
    _check(migrated == ('AGENT_WAKE_CLI="solet-bridge"\n', "solet_name: iris\n", {"SOLET_NAME": "iris"}), "zshrc, manifest, and claude.json migrated")
    after = {path: path.read_bytes() for path in (new_plist, runtime.home / ".zshrc", target / "root_manifest.yaml", runtime.home / ".claude.json")}
    verified = cast(dict[str, Any], dispatch_request(_request(target, ref, purpose="post_apply"), runtime))
    _check(verified["checkpoint_status"] == "verified", "postcondition verifies after apply")
    dispatch_request(_request(target, ref, phase="apply"), runtime)
    _check({path: path.read_bytes() for path in after} == after, "re-apply on a migrated surface is byte-identical")


def _check_export_root_and_cache(root: Path) -> None:
    target, _ = _target(root)
    runtime = FakeRuntime(root / "home")
    runtime.home.mkdir()
    _check_export_root(root, target, runtime)
    _check_plugin_cache(target, runtime)


def _check_export_root(root: Path, target: Path, runtime: FakeRuntime) -> None:
    ref = "existing::migration.export_root_containment"
    unset = cast(dict[str, Any], dispatch_request(_request(target, ref), runtime))
    operator_action = any("operator_action_required=" in fact for item in unset["evidence"] for fact in item["observed"])
    _check((unset["checkpoint_status"], operator_action) == ("verified", True), "export root unset is probe-only with operator action evidence")
    (target / "plugins" / "jira_plugin").mkdir()
    (target / "plugins" / "salesforce_plugin").mkdir()
    config = target / "profile" / "config" / "plugins"
    config.mkdir(parents=True)
    (config / "jira_plugin.json").write_text(json.dumps({"export_allowed_roots": [str(root / "work")]}))
    (root / "work").mkdir()
    pending = cast(dict[str, Any], dispatch_request(_request(target, ref), runtime))
    planned = [action["target"] for action in pending["planned_actions"]]
    _check((pending["checkpoint_status"], planned) == ("pending", [str(config / "salesforce_plugin.json")]), "a connector lacking the chosen root is planned exactly")
    applied = cast(dict[str, Any], dispatch_request(_request(target, ref, phase="apply"), runtime))
    propagated = json.loads((config / "salesforce_plugin.json").read_text())["export_allowed_roots"]
    _check((applied["checkpoint_status"], propagated) == ("applied", [str(root / "work")]), "root propagated to the missing connector only")


def _check_plugin_cache(target: Path, runtime: FakeRuntime) -> None:
    ref = "existing::runtime.plugin_cache_refresh"
    cache = runtime.home / ".claude" / "plugins" / "cache" / _NAME / "coordination-hooks" / "1.0.0"
    (cache / "hooks").mkdir(parents=True)
    (target / ".venv/bin").mkdir(parents=True, exist_ok=True)
    (target / ".venv/bin/python3").write_text("fixture")
    (runtime.home / ".claude" / "plugins" / "installed_plugins.json").write_text(json.dumps({"plugins": {f"coordination-hooks@{_NAME}": [{"installPath": str(cache)}]}}))
    _copy_hooks(target, cache)
    bare = cast(dict[str, Any], dispatch_request(_request(target, ref), runtime))
    _check((bare["checkpoint_status"], [action["id"] for action in bare["planned_actions"]]) == ("pending", ["claude.patch_hook_interpreter", "cache.reinstall", "claude.publish_coordination_receipt"]), "iss_fa27466f: a matching cache copy of bare hooks with no receipt is not current")
    _settle_as_create(target, runtime, cache)
    current = cast(dict[str, Any], dispatch_request(_request(target, ref, purpose="post_apply"), runtime))
    _check(current["checkpoint_status"] == "verified", "a matching, pinned and received cache copy verifies")
    (cache / "hooks" / "wake_waiter.py").write_bytes(b"# stale\n")
    stale = cast(dict[str, Any], dispatch_request(_request(target, ref), runtime))
    _check((stale["checkpoint_status"], [action["id"] for action in stale["planned_actions"]]) == ("pending", ["cache.reinstall", "claude.publish_coordination_receipt"]), "diff-based check plans the refresh and the republish")
    refreshed = cast(dict[str, Any], dispatch_request(_request(target, ref, phase="apply"), runtime))
    vectors = [argv[2] for argv in runtime.commands if argv[:2] == ("/fixture/bin/claude", "plugin") and argv[2] in {"uninstall", "install"}]
    # This fake install copies nothing, so the receipt publish reads the stale cache back and refuses, never publishes.
    _check((refreshed["checkpoint_status"], refreshed.get("error_kind"), vectors) == ("blocked", "coordination_receipt_invalid", ["uninstall", "install"]), "refresh is uninstall then install, then a receipt only over the cache it reads back")


def _copy_hooks(target: Path, cache: Path) -> None:
    for name in ("coordination_owner.py", "heartbeat_report_alive.py", "rotation_due_watch.py", "wake_waiter.py", "hooks.json"):
        (cache / "hooks" / name).write_bytes((target / "plugins/github_midwife_plugin/claude_plugin/coordination-hooks/hooks" / name).read_bytes())


def _settle_as_create(target: Path, runtime: FakeRuntime, cache: Path) -> None:
    """Create's pin, a cache copy of the pinned hooks, and create's receipt."""
    manifest = target / "plugins/github_midwife_plugin/claude_plugin/coordination-hooks/hooks/hooks.json"
    _check(setup_plugin_operations._patch_hook_manifest(manifest, target, cast(Any, runtime)) is None, "fixture: create's pin applies")  # noqa: SLF001
    _copy_hooks(target, cache)
    published = setup_plugin_operations._publish_claude_receipt(_request(target, "existing::runtime.plugin_cache_refresh", phase="apply"), cast(Any, runtime), "/fixture/bin/claude", _NAME, f"coordination-hooks@{_NAME}")  # noqa: SLF001
    _check(published is None, f"fixture: create's receipt publishes: {published}")


def _check_dependency_route(root: Path) -> None:
    target = root / "deps"
    for relative in ("solet_setup_contracts", "ananta", "plugins/macos_vault_plugin", "plugins/github_midwife_plugin", "plugins/agent_messaging_plugin", "plugins/extra_plugin"):
        (target / relative).mkdir(parents=True)
    (target / ".venv/bin").mkdir(parents=True)
    (target / ".venv/bin/python3").write_text("fixture")
    (target / ".venv/bin/solet-bridge").write_text("fixture")
    closure = [f"{name}={rel}" for name, rel in (("solet-setup-contracts", "solet_setup_contracts"), ("ananta", "ananta"), ("macos-vault-plugin", "plugins/macos_vault_plugin"), ("github_midwife_plugin", "plugins/github_midwife_plugin"), ("agent_messaging_plugin", "plugins/agent_messaging_plugin"), ("extra_plugin", "plugins/extra_plugin"))]
    pip_calls: list[list[str]] = []
    present = {"solet-setup-contracts", "ananta", "macos-vault-plugin", "github_midwife_plugin", "agent_messaging_plugin"}

    def runner(command: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        if "-I" in command and "-c" in command:
            if "packages =" not in command[-1]:
                return subprocess.CompletedProcess(command, 0, "3.13.7\n", "")
            packages = {name: {"version": "1.0.0", "direct_url": json.dumps({"url": (target / rel).resolve().as_uri(), "dir_info": {"editable": True}})} for name, rel in (item.split("=") for item in closure) if name in present}
            return subprocess.CompletedProcess(command, 0, json.dumps({"pip": True, "build_backend": True, "wheel": True, "packages": packages}), "")
        if command[-1:] == ["--version"]:
            return subprocess.CompletedProcess(command, 0, "solet-bridge 0.1.0\n", "")
        if "pip" in command:
            pip_calls.append(list(command))
            present.add("extra_plugin")
            return subprocess.CompletedProcess(command, 0, "", "")
        return subprocess.CompletedProcess(command, 0, "", "")

    def raw(phase: str, purpose: str | None, inputs: dict[str, Any]) -> dict[str, Any]:
        return {"protocol_version": 1, "kind": "operation_request", "request_id": str(uuid.uuid4()), "operation_id": "dependencies_reconcile", "operation_ref": "existing::dependencies.reconcile", "phase": phase, "probe_purpose": purpose, "attempt": 1, "name": _NAME, "target": str(target), "flow_id": "existing-install", "flow_source_revision": "a" * 40, "answers_fingerprint": "sha256:" + "b" * 64, "approval_fingerprint": None if phase == "probe" else "sha256:" + "c" * 64, "dry_run": phase == "probe", "timeout_seconds": 30, "public_inputs": inputs}

    probe = execute_adapter_request(raw("probe", "pre_apply", {"declared_closure": closure}), runner=runner, which=lambda _name: None, base_python=sys.executable)
    _check(probe["checkpoint_status"] == "pending" and [action["id"] for action in probe["planned_actions"]] == ["pip.install_editable.plugins.extra_plugin"], f"one missing declared plugin plans exactly that editable install: {probe['planned_actions']}")
    applied = execute_adapter_request(raw("apply", None, {"declared_closure": closure}), runner=runner, which=lambda _name: None, base_python=sys.executable)
    _check(applied["checkpoint_status"] == "applied" and len(pip_calls) == 1 and pip_calls[0][-1].endswith("plugins/extra_plugin"), "apply installs only the missing piece")
    after = execute_adapter_request(raw("probe", "post_apply", {"declared_closure": closure}), runner=runner, which=lambda _name: None, base_python=sys.executable)
    _check(after["checkpoint_status"] == "verified", "postcondition verifies")
    refused = execute_adapter_request(raw("probe", "pre_apply", {"declared_closure": ["ananta=ananta"]}), runner=runner, which=lambda _name: None, base_python=sys.executable)
    _check(refused["checkpoint_status"] in {"blocked", "failed"}, "a closure omitting a required distribution is refused")
    wrong_flow = execute_adapter_request(raw("probe", "pre_apply", {"declared_closure": closure}) | {"flow_id": "macos.repository_setup"}, runner=runner, which=lambda _name: None, base_python=sys.executable)
    _check(wrong_flow["error_kind"] == "adapter_protocol_error", "an existing:: ref under the create flow is refused by the bootstrap validator")


def _check_seed_validator() -> None:
    with TemporaryDirectory() as temporary:
        target = Path(temporary)
        _request(target, "existing::hydration.reconcile")
        try:
            _request(target, "existing::hydration.reconcile", flow_id="macos.repository_setup")
        except AdapterInputError:
            _check(True, "existing:: ref under the create flow is refused")
        else:
            raise AssertionError("accepted existing:: under the create flow")
        try:
            _request(target, "hydration::shell.install", flow_id="existing-install")
        except AdapterInputError:
            _check(True, "create ref under the existing-install flow is refused")
        else:
            raise AssertionError("accepted create ref under existing-install")
        _check(_request(target, "hydration::shell.install", flow_id="macos.repository_setup").flow_id == "macos.repository_setup", "create flow still parses")
        unknown = cast(dict[str, Any], dispatch_request(_request(target, "existing::lifecycle.cutover"), FakeRuntime(target / "home")))
        _check(unknown["error_kind"] == "adapter_missing", "a Manager-side ref has no seed handler")
        _check(hashlib.sha256(b"").hexdigest() != "", "hashlib available")


def main() -> int:
    with TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        _check_managed_block_table(root / "blocks")
        _check_stamped_previous(root / "previous")
        _check_whole_file_and_plist(root / "whole")
        _check_feedback_skill_declared()
        _check_feedback_skill_refresh(root / "skill")
        _check_fleet_declared()
        _check_fleet_refresh(root / "fleet")
        _check_clone_exclude(root / "exclude")
        _check_rename_migration(root / "rename")
        _check_export_root_and_cache(root / "export")
        _check_dependency_route(root / "route")
    _check_seed_validator()
    _check(set(ops.operation_handlers()) == set(ops.EXISTING_ALLOWED_PUBLIC_INPUTS), "handler table and allowed inputs agree")
    print(f"existing_install_operations_smoke OK: {_CHECKS} checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
