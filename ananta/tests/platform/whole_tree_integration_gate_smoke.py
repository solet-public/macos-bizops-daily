#!/usr/bin/env python3
"""Meta-smoke for the whole-tree integration gate (W-INT Cycle 1).

Exercises the gate's collaborator functions against synthetic surfaces
(in-memory `ServiceSurface` / `PluginSurface` / `CallSiteRef` objects with
hand-crafted decorator/JSON mappings) and confirms each check family
emits the expected findings for the documented failure modes.

Cases:
  1. C1.5 — abstract method has no companion KB JSON (the canonical
     vault `write_state` regression that motivated the gate).
  2. C1.7 — orphan KB JSON without a matching decorator.
  3. C1.6 — KB JSON `process_key` does not match the canonical shape.
  4. C2.2 — plugin @platform_process method has no companion JSON.
  5. C2.4 — orphan plugin KB JSON.
  6. C3.1 — call-site references a non-existent process_key.
  7. C3.2 — call-site references a bound-plugin namespace (Task #21 surface).
  8. C5.1 — scheduling action literal omits `result_processor_kind`.
  9. Allowlist coverage — wildcard + exact-match.
 10. End-to-end — the live gate against the real tree returns exit 0
     with the shipped allowlist (proves wiring + allowlist remain valid).
 11. Negative-fixture marker — `# wint:negative-fixture` is honored in a
     tests/ path (line-scoped suppression) but IGNORED in a src/ path
     (red-first: the convention must not weaken src detection).
 12. C3.* executable-context matching (iss_3dc2ecc3) — a process_key quoted
     inside PROSE (a comment, a docstring, a `description=` format example)
     is not a reference, while a whole-literal key still is. Also pins the
     assembled-key boundary: f-strings and implicit adjacent-literal
     concatenation are deliberately NOT reported.
 13. Bindings fail-loud (iss_06a5d953) — a tree with no materialized
     `service_bindings.json` derives them from the shipped profile template
     genesis uses; with no provenance to say which profile, the gate exits 2
     (harness error) instead of silently scanning against an empty map.
 17. C6.1 git-awareness (iss_ce45713b) — an undeclared root entry that Git
     does not track is reported, not blocking; the same entry staged or
     committed blocks exactly as before, and so does every unknown entry
     when no Git directory exists to ask.

Project policy: no pytest. Exits 0 on success, 1 on first failure.
"""

from __future__ import annotations

import ast
import json
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "quality_gates"))

import whole_tree_integration_gate as wint  # noqa: E402
from whole_tree_integration_gate import (  # noqa: E402
    Allowlist,
    AllowlistEntry,
    BindingsResolutionError,
    CallSiteRef,
    Finding,
    PluginSurface,
    ServiceSurface,
    _bindings_from_profile_template,
    _check_plugin_decorator_vs_kb,
    _check_plugin_kb_process_keys,
    _check_service_decorator_vs_kb,
    _check_service_kb_process_keys,
    _classify_call_site_ref,
    _collect_service_interface_decorated,
    _is_test_path,
    _scan_single_file_for_refs,
    check_scheduling_rpk_propagation,
    load_allowlist,
)

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


def _synthetic_func(name: str = "verb") -> ast.FunctionDef:
    """Return a minimal FunctionDef AST node for use in synthetic surfaces."""
    node = ast.FunctionDef(
        name=name,
        args=ast.arguments(posonlyargs=[], args=[], kwonlyargs=[],
                           kw_defaults=[], defaults=[]),
        body=[ast.Pass()],
        decorator_list=[],
        returns=None,
        lineno=1,
    )
    return node


def _case_c1_5_decorator_without_json() -> None:
    print("\nCase 1: C1.5 — service-interface decorator without KB JSON")
    surface = ServiceSurface(
        name="example_service",
        public_path=REPO_ROOT / "ananta/src/ananta/services/example_service/interfaces/public.py",
        decorated={"write_state": _synthetic_func("write_state")},
        kb_jsons={},
    )
    findings = _check_service_decorator_vs_kb(surface)
    _check(any(f.check_id == "C1.5" for f in findings),
           "emits C1.5 finding when decorator has no JSON (vault write_state precedent)")
    _check(any("write_state" in f.specifier for f in findings),
           "C1.5 specifier names the offending method")


def _case_c1_5_disabled_decorator_without_json() -> None:
    print("\nCase 1b: C1.5 — disabled decorator needs no KB JSON")
    module = ast.parse('''
class API:
    @service_interface_process(name="disabled_verb", is_enabled=False)
    def disabled_verb(self):
        pass
    @service_interface_process(name="enabled_verb", is_enabled=True)
    def enabled_verb(self):
        pass
''')
    decorated = _collect_service_interface_decorated(module)
    surface = ServiceSurface(
        name="example_service",
        public_path=REPO_ROOT / "ananta/src/ananta/services/example_service/interfaces/public.py",
        decorated=decorated,
        kb_jsons={},
    )
    findings = _check_service_decorator_vs_kb(surface)
    _check(
        "disabled_verb" not in decorated and "enabled_verb" in decorated,
        "shared enabled predicate omits disabled decorators from the C1.5 surface",
    )
    _check(
        [finding.specifier for finding in findings] == ["enabled_verb"],
        "disabled method without JSON passes C1.5; enabled method without JSON fails",
    )


def _case_c1_7_orphan_kb_json() -> None:
    print("\nCase 2: C1.7 — orphan service-interface KB JSON")
    fake_json = REPO_ROOT / "ananta/knowledge_base/processes/example_service/orphan.json"
    surface = ServiceSurface(
        name="example_service",
        public_path=REPO_ROOT / "ananta/src/ananta/services/example_service/interfaces/public.py",
        decorated={},
        kb_jsons={"orphan": fake_json},
    )
    findings = _check_service_decorator_vs_kb(surface)
    _check(any(f.check_id == "C1.7" for f in findings),
           "emits C1.7 finding when JSON has no matching decorator")


def _case_c1_6_wrong_process_key_in_json() -> None:
    print("\nCase 3: C1.6 — KB JSON process_key mismatch")
    with tempfile.TemporaryDirectory() as tmpdir:
        bad_json = Path(tmpdir) / "verb.json"
        bad_json.write_text(json.dumps({
            "process_key": "service_interface::WRONG_PROVIDER::verb",
        }))
        surface = ServiceSurface(
            name="example_service",
            public_path=REPO_ROOT / "ananta/src/ananta/services/example_service/interfaces/public.py",
            decorated={"verb": _synthetic_func("verb")},
            kb_jsons={"verb": bad_json},
        )
        findings = _check_service_kb_process_keys(surface)
        _check(any(f.check_id == "C1.6" for f in findings),
               "emits C1.6 when JSON process_key does not match canonical shape")


def _case_c2_2_plugin_decorator_without_json() -> None:
    print("\nCase 4: C2.2 — @platform_process method without KB JSON")
    surface = PluginSurface(
        name="example_plugin",
        plugin_py=REPO_ROOT / "plugins/example_plugin/src/example_plugin/plugin.py",
        decorated={"upload_track": _synthetic_func("upload_track")},
        kb_jsons={},
    )
    findings = _check_plugin_decorator_vs_kb(surface)
    _check(any(f.check_id == "C2.2" for f in findings),
           "emits C2.2 finding when @platform_process method has no JSON")


def _case_c2_4_orphan_plugin_json() -> None:
    print("\nCase 5: C2.4 — orphan plugin KB JSON")
    fake_json = REPO_ROOT / "plugins/example_plugin/knowledge_base/processes/orphan.json"
    surface = PluginSurface(
        name="example_plugin",
        plugin_py=REPO_ROOT / "plugins/example_plugin/src/example_plugin/plugin.py",
        decorated={},
        kb_jsons={"orphan": fake_json},
    )
    findings = _check_plugin_decorator_vs_kb(surface)
    _check(any(f.check_id == "C2.4" for f in findings),
           "emits C2.4 finding when plugin JSON has no matching decorator")


def _case_c2_3_wrong_plugin_process_key() -> None:
    print("\nCase 6: C2.3 — plugin JSON process_key mismatch")
    with tempfile.TemporaryDirectory() as tmpdir:
        bad_json = Path(tmpdir) / "upload_track.json"
        bad_json.write_text(json.dumps({
            "process_key": "service_interface::WRONG::upload_track",
        }))
        surface = PluginSurface(
            name="example_plugin",
            plugin_py=REPO_ROOT / "plugins/example_plugin/src/example_plugin/plugin.py",
            decorated={"upload_track": _synthetic_func("upload_track")},
            kb_jsons={"upload_track": bad_json},
        )
        findings = _check_plugin_kb_process_keys(surface)
        _check(any(f.check_id == "C2.3" for f in findings),
               "emits C2.3 when plugin JSON process_key does not match canonical")


def _case_c3_1_nonexistent_call_site() -> None:
    print("\nCase 7: C3.1 — call-site references a non-existent process_key")
    ref = CallSiteRef(
        namespace="service_interface",
        provider="ghost_service",
        verb="nope",
        file_path=REPO_ROOT / "ananta/src/ananta/example.py",
        lineno=42,
    )
    findings = _classify_call_site_ref(ref, set(), {}, {})
    _check(any(f.check_id == "C3.1" for f in findings),
           "emits C3.1 when call-site references unknown service")


def _case_c3_2_bound_plugin_namespace() -> None:
    print("\nCase 8: C3.2 — call-site references a bound-plugin namespace (Task #21)")
    plugin_surface = PluginSurface(
        name="actr_memory_plugin",
        plugin_py=REPO_ROOT / "plugins/actr_memory_plugin/src/actr_memory_plugin/plugin.py",
        decorated={"remember": _synthetic_func("remember")},
        kb_jsons={"remember": Path("/tmp/fake.json")},
    )
    ref = CallSiteRef(
        namespace="plugin",
        provider="actr_memory_plugin",
        verb="remember",
        file_path=REPO_ROOT / "ananta/src/ananta/example.py",
        lineno=100,
    )
    bound = {"actr_memory_plugin"}
    findings = _classify_call_site_ref(
        ref, bound, {}, {"actr_memory_plugin": plugin_surface},
    )
    _check(any(f.check_id == "C3.2" for f in findings),
           "emits C3.2 when call-site references bound-plugin namespace")


def _case_c5_1_scheduling_rpk_missing() -> None:
    print("\nCase 9: C5.1 — scheduling action literal omits result_processor_kind")
    with tempfile.TemporaryDirectory() as tmpdir:
        src = Path(tmpdir) / "fixture.py"
        src.write_text(
            "self._scheduling_service.create_cron_schedule(\n"
            "    cron_expression='*/5 * * * *',\n"
            "    actions=[{\n"
            "        'process_key': 'service_interface::x::y',\n"
            "        'arguments': {},\n"
            "    }],\n"
            ")\n"
        )
        findings = check_scheduling_rpk_propagation([Path(tmpdir)])
        _check(any(f.check_id == "C5.1" for f in findings),
               "emits C5.1 when scheduling action dict omits result_processor_kind")


def _case_c5_1_scheduling_rpk_present_is_clean() -> None:
    print("\nCase 10: C5.1 — scheduling action with rpk emits no finding")
    with tempfile.TemporaryDirectory() as tmpdir:
        src = Path(tmpdir) / "fixture.py"
        src.write_text(
            "self._scheduling_service.create_cron_schedule(\n"
            "    cron_expression='*/5 * * * *',\n"
            "    actions=[{\n"
            "        'process_key': 'service_interface::x::y',\n"
            "        'arguments': {},\n"
            "        'result_processor_kind': 'inference',\n"
            "    }],\n"
            ")\n"
        )
        findings = check_scheduling_rpk_propagation([Path(tmpdir)])
        _check(not findings,
               "no C5.1 finding when result_processor_kind is set")


def _case_allowlist_wildcard_and_exact() -> None:
    print("\nCase 11: Allowlist coverage — wildcard + exact-match semantics")
    allowlist = Allowlist(frozenset({
        AllowlistEntry("C2.4", "example_plugin", "*"),
        AllowlistEntry("C3.1", "ananta/src/x.py", "plugin::y::z"),
    }))
    wildcard_finding = Finding("C2.4", "example_plugin", "anything", "msg")
    exact_finding = Finding("C3.1", "ananta/src/x.py", "plugin::y::z", "msg")
    miss_finding = Finding("C3.1", "ananta/src/x.py", "plugin::OTHER::z", "msg")
    _check(allowlist.covers(wildcard_finding),
           "wildcard `*` covers any specifier under same (check, scope)")
    _check(allowlist.covers(exact_finding),
           "exact specifier match is covered")
    _check(not allowlist.covers(miss_finding),
           "exact specifier mismatch is NOT covered")


def _case_allowlist_file_round_trip() -> None:
    print("\nCase 12: Allowlist file parses and round-trips correctly")
    with tempfile.TemporaryDirectory() as tmpdir:
        path = Path(tmpdir) / "allow.txt"
        _tag = "  # owner: smoke-fixture reason: tracked test debt expires: 2099-01-01\n"
        path.write_text(
            "# comment\n"
            "\n"
            "C1.5::svc::method" + _tag +
            "C2.4::plugin::*" + _tag +
            "C3.1::a/b/c.py::plugin::x::y" + _tag
        )
        loaded = load_allowlist(path)
        _check(len(loaded.entries) == 3,
               "parses 3 entries, skipping comment + blank line")
        _check(any(e.check_id == "C1.5" for e in loaded.entries),
               "C1.5 entry parsed")
        _check(any(e.specifier == "plugin::x::y" for e in loaded.entries),
               "specifier with embedded `::` parsed verbatim")


def _case_negative_fixture_marker() -> None:
    print("\nCase 14: C3.* negative-fixture marker — honored in tests/, IGNORED in src/")
    marked = 'KEY = "plugin::ghost_plugin::no_such_verb"  # wint:negative-fixture\n'
    plain = 'OTHER = "plugin::ghost_plugin::also_missing"\n'
    with tempfile.TemporaryDirectory() as tmpdir:
        # A tests/ file: the marked line's ref is suppressed; a plain line on
        # the SAME file is still scanned (line-scoped, not a blanket wildcard).
        test_file = Path(tmpdir) / "plugins" / "x" / "tests" / "y_smoke.py"
        test_file.parent.mkdir(parents=True, exist_ok=True)
        test_file.write_text(marked + plain, encoding="utf-8")
        verbs = {r.verb for r in _scan_single_file_for_refs(test_file)}
        _check("no_such_verb" not in verbs,
               "marker suppresses the marked line's ref in a tests/ file")
        _check("also_missing" in verbs,
               "unmarked line on the same file STILL scanned (line-scoped)")

        # A src/ file: the marker is IGNORED — the ref is still detected. This
        # is the red-first guarantee that the convention cannot weaken src.
        src_file = Path(tmpdir) / "plugins" / "x" / "src" / "y.py"
        src_file.parent.mkdir(parents=True, exist_ok=True)
        src_file.write_text(marked, encoding="utf-8")
        src_verbs = {r.verb for r in _scan_single_file_for_refs(src_file)}
        _check("no_such_verb" in src_verbs,
               "RED-FIRST: marker IGNORED in src/ — the ref is STILL detected")

    _check(_is_test_path(Path("plugins/x/tests/y_smoke.py")),
           "_is_test_path True for plugins/<x>/tests/…")
    _check(_is_test_path(Path("ananta/tests/platform/z_smoke.py")),
           "_is_test_path True for ananta/tests/…")
    _check(not _is_test_path(Path("plugins/x/src/x/y.py")),
           "_is_test_path False for a src/ path")


_C6_MANIFEST = """\
schema_version: 1
solet_name: smokebot
universal:
  files: [root_manifest.yaml]
  directories: []
platform_managed:
  directories: [.git]
sanctioned: []
overrides: []
diagnostic:
  report_categories: [unknown_root_entries]
  ignore_patterns: []
"""


def _fixture_git(root: Path, *arguments: str) -> None:
    import os
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    env.update(
        GIT_AUTHOR_NAME="smoke", GIT_AUTHOR_EMAIL="smoke@example.invalid",
        GIT_COMMITTER_NAME="smoke", GIT_COMMITTER_EMAIL="smoke@example.invalid",
    )
    subprocess.run(("git", *arguments), cwd=root, env=env, check=True, capture_output=True)


def _case_c6_1_untracked_root_entry_does_not_block() -> None:
    """iss_ce45713b: C6.1 blocks on drift the repository CARRIES, never on a
    neighbouring lane's untracked scratch file — and the discriminator is that
    the SAME entry, once tracked, blocks exactly as before."""
    print("\nCase 17: C6.1 — untracked root entry reported, not blocking; tracked one blocks")
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp).resolve()
        (root / "root_manifest.yaml").write_text(_C6_MANIFEST)
        _fixture_git(root, "init", "-q")
        _fixture_git(root, "add", "-A")
        _fixture_git(root, "commit", "-qm", "base")
        _check(wint.check_root_manifest_drift(root) == [],
               "declared-only tree yields no C6 findings")

        (root / "HANDOVER_NOTE.md").write_text("another lane's note\n")
        findings = wint.check_root_manifest_drift(root)
        _check(findings == [],
               f"untracked root entry yields no blocking finding (got {findings})")

        _fixture_git(root, "add", "HANDOVER_NOTE.md")
        findings = wint.check_root_manifest_drift(root)
        _check([(f.check_id, f.scope_qualifier, f.specifier) for f in findings]
               == [("C6.1", "root", "HANDOVER_NOTE.md")],
               f"the same entry, staged, is C6.1::root::HANDOVER_NOTE.md (got {findings})")

        _fixture_git(root, "commit", "-qm", "undeclared root entry")
        findings = wint.check_root_manifest_drift(root)
        _check([(f.check_id, f.scope_qualifier, f.specifier) for f in findings]
               == [("C6.1", "root", "HANDOVER_NOTE.md")],
               f"the same entry, committed, is still C6.1 (got {findings})")

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp).resolve()
        (root / "root_manifest.yaml").write_text(_C6_MANIFEST)
        (root / "HANDOVER_NOTE.md").write_text("no git here at all\n")
        findings = wint.check_root_manifest_drift(root)
        _check([(f.check_id, f.specifier) for f in findings] == [("C6.1", "HANDOVER_NOTE.md")],
               f"with no git directory every unknown entry still blocks (got {findings})")


def _case_e2e_live_gate_clean() -> None:
    print("\nCase 13: end-to-end — live gate against the real tree returns exit 0")
    gate = REPO_ROOT / "quality_gates" / "whole_tree_integration_gate.py"
    allowlist = REPO_ROOT / "quality_gates" / "whole_tree_integration_gate_allowlist.txt"
    venv = REPO_ROOT / ".venv" / "bin" / "python3"
    if not venv.exists():
        _check(True, "skipped (no venv at .venv/bin/python3)")
        return
    result = subprocess.run(
        [str(venv), str(gate), "--allowlist", str(allowlist)],
        capture_output=True, text=True, timeout=180,
    )
    _check(result.returncode == 0,
           f"live gate exits 0 with shipped allowlist (got {result.returncode})")
    _check("OK:" in result.stdout,
           "live gate prints OK: summary line")


def _case_c3_executable_context_only() -> None:
    print("\nCase 15: C3.* — a key quoted in PROSE is not a reference")
    # Every shape below carries a real, correctly-spelled process key. What
    # separates them is CONTEXT, which is exactly what the pre-2026-09-05
    # raw-line detector could not see (iss_3dc2ecc3).
    source = (
        '''"""Module docstring mentioning \'plugin::ghost_plugin::doc_verb\'."""
'''
        "\n"
        "# A comment naming 'plugin::ghost_plugin::comment_verb' as an example.\n"
        "\n"
        "def f() -> None:\n"
        '    """Returns: Process key string (e.g., \'plugin::ghost_plugin::inner_doc_verb\').\n'
        '    """\n'
        "\n"
        "META = Param(\n"
        '    description=(\n'
        '        "The fully-qualified process identifier. "\n'
        '        "Format: \'provider_type::provider::function_name\' "\n'
        '        "(e.g., \'plugin::ghost_plugin::kwarg_verb\')"\n'
        "    ),\n"
        ")\n"
        "\n"
        "# Positive controls — whole-literal keys in executable position.\n"
        'ROUTE = {"target": "plugin::ghost_plugin::dict_value_verb"}\n'
        'call("plugin::ghost_plugin::call_arg_verb")\n'
        "\n"
        "# Assembled keys — deliberately OUT of scope.\n"
        'FSTR = f"plugin::ghost_plugin::fstring_{suffix}"\n'
        'IMPL = ("plugin::ghost_plugin::"\n'
        '        "implicit_concat_verb")\n'
    )
    with tempfile.TemporaryDirectory() as tmpdir:
        mod = Path(tmpdir) / "plugins" / "x" / "src" / "prose.py"
        mod.parent.mkdir(parents=True, exist_ok=True)
        mod.write_text(source, encoding="utf-8")
        verbs = {r.verb for r in _scan_single_file_for_refs(mod)}

    # Prose — each of these was a Group A/C born-clone false positive.
    _check("doc_verb" not in verbs, "module docstring key is NOT a reference")
    _check("comment_verb" not in verbs, "comment key is NOT a reference")
    _check("inner_doc_verb" not in verbs, "function docstring key is NOT a reference")
    _check("kwarg_verb" not in verbs, "`description=` format example is NOT a reference")
    # Positive controls — these MUST still fire, or the fix over-pruned and the
    # gate has quietly stopped detecting the drift it exists to catch.
    _check("dict_value_verb" in verbs, "POSITIVE CONTROL: whole-literal key as a dict value still detected")
    _check("call_arg_verb" in verbs, "POSITIVE CONTROL: whole-literal key as a call argument still detected")
    # Assembled — documented boundary.
    _check("fstring_" not in verbs, "f-string prefix is NOT reported as a whole key")
    _check("implicit_concat_verb" not in verbs, "implicit adjacent-literal concatenation is NOT reported")


def _write_fixture_clone(root: Path, *, bundle: str | None, template: str | None) -> None:
    """Materialize the minimum a born clone carries for bindings resolution."""
    if bundle is not None:
        (root / "PROVENANCE.json").write_text(
            json.dumps({"bundle": {"name": bundle, "platform": "macos"}}), encoding="utf-8",
        )
    if template is not None:
        tdir = root / "plugins" / "github_midwife_plugin" / "knowledge_base" / "profile_templates"
        tdir.mkdir(parents=True, exist_ok=True)
        (tdir / f"{template}.yaml").write_text(
            "service_bindings:\n"
            "  state_service: postgres_state_management_plugin\n"
            "  vault_service: macos_vault_plugin\n",
            encoding="utf-8",
        )


def _case_bindings_fail_loud() -> None:
    print("\nCase 16: bindings — derived from the shipped template, never a silent {}")
    with tempfile.TemporaryDirectory() as tmpdir:
        # (a) No bindings file AND no provenance: the gate cannot know which
        # profile to measure against. Before this fix it returned {} and every
        # bound-plugin ref reclassified C3.2 -> C3.1 (iss_06a5d953).
        bare = Path(tmpdir) / "bare"
        bare.mkdir()
        try:
            _bindings_from_profile_template(bare)
            _check(False, "no bindings + no provenance raises (got a value instead)")
        except BindingsResolutionError:
            _check(True, "no bindings + no provenance raises BindingsResolutionError")
        _check(issubclass(BindingsResolutionError, RuntimeError),
               "BindingsResolutionError is a RuntimeError, so `main` renders it as exit 2")

        # (b) Provenance present: bindings equal the shipped template's mapping.
        clone = Path(tmpdir) / "clone"
        clone.mkdir()
        _write_fixture_clone(clone, bundle="macos-bizops", template="macos-bizops")
        derived = _bindings_from_profile_template(clone)
        _check(derived == {"state_service": "postgres_state_management_plugin",
                           "vault_service": "macos_vault_plugin"},
               "bindings derived from the profile template genesis materializes from")

        # (c) Provenance present but the template is missing: still loud.
        headless = Path(tmpdir) / "headless"
        headless.mkdir()
        _write_fixture_clone(headless, bundle="macos-bizops", template=None)
        try:
            _bindings_from_profile_template(headless)
            _check(False, "missing template raises (got a value instead)")
        except RuntimeError:
            _check(True, "missing profile template raises (harness error, not {})")

        # (d) End-to-end: the gate process really exits 2, not just the helper.
        # REPO_ROOT/_DEFAULT_BINDINGS are module constants, so point them at a
        # tree with neither bindings nor provenance and drive `main` itself.
        empty = Path(tmpdir) / "empty"
        empty.mkdir()
        saved_root, saved_bindings = wint.REPO_ROOT, wint._DEFAULT_BINDINGS
        try:
            wint.REPO_ROOT = empty
            wint._DEFAULT_BINDINGS = empty / "profile" / "config" / "service_bindings.json"
            rc = wint.main(["--skip-call-sites", "--skip-scheduling"])
        finally:
            wint.REPO_ROOT, wint._DEFAULT_BINDINGS = saved_root, saved_bindings
        _check(rc == 2, f"live gate exits 2 when bindings are unresolvable (got {rc})")


def main() -> int:
    print("Whole-tree integration gate meta-smoke (W-INT Cycle 1)")
    print("=" * 60)
    _case_c1_5_decorator_without_json()
    _case_c1_5_disabled_decorator_without_json()
    _case_c1_7_orphan_kb_json()
    _case_c1_6_wrong_process_key_in_json()
    _case_c2_2_plugin_decorator_without_json()
    _case_c2_4_orphan_plugin_json()
    _case_c2_3_wrong_plugin_process_key()
    _case_c3_1_nonexistent_call_site()
    _case_c3_2_bound_plugin_namespace()
    _case_c5_1_scheduling_rpk_missing()
    _case_c5_1_scheduling_rpk_present_is_clean()
    _case_allowlist_wildcard_and_exact()
    _case_allowlist_file_round_trip()
    _case_negative_fixture_marker()
    _case_c3_executable_context_only()
    _case_bindings_fail_loud()
    _case_c6_1_untracked_root_entry_does_not_block()
    _case_e2e_live_gate_clean()

    print("\n" + "-" * 60)
    print(f"PASSED: {_passed}")
    print(f"FAILED: {len(_failed)}")
    if _failed:
        print("\nFailures:")
        for label in _failed:
            print(f"  - {label}")
        return 1
    print("\nAll checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
