"""Transition bundle, registry, and validator closure (design section 11, "Bundle and registry").

Legs:

- the shipped ``existing_install_flow.json`` parses, validates against its
  schema, digests identically from bytes and from a directory, and the
  stager's digest for it equals both;
- the parser refuses: an unknown ``operation_ref``, a Manager-only ref, a
  non-monotonic stage order, ``database_forward_only`` without
  ``forward_only``/``manual``, a preserved-never destination (``profile/config``,
  ``profile/data``, a session-history path, ``AGENTS.md``/``CLAUDE.md``), a
  missing postcondition probe, and any extra key;
- Manager and seed ``existing::`` enumerations (seed-side subset) are byte-equal;
- static reachable-call analysis from the Step-5 Manager modules finds no edge
  to ``AdapterRegistry``, ``genesis::``, ``bootstrap::postgres``, credential or
  vault seeding, ``router_install``, ``StateManagementInterface``, ``psycopg``,
  ``asyncpg``, Keychain value APIs, or ``psql``; the seed handler module's
  import graph honours the section-3.2 denylist;
- all THREE flow-id validators (Manager ``adapter_protocol``, seed
  ``setup_adapter_contract``, bootstrap ``protocol``) accept exactly the closed
  two-member set with the ref cross-check, and every probe purpose Step 5 sends
  is in ``_PROBE_PURPOSES``;
- the Manager's ``REQUIRED_DISTRIBUTIONS`` copy equals the bootstrap adapter's.
"""

from __future__ import annotations

import ast
import copy
import hashlib
import json
import re
import sys
import uuid
from dataclasses import asdict
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, cast

import jsonschema

_ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(_ROOT / "solet_cli" / "src"), str(_ROOT / "solet_setup_contracts" / "src"), str(_ROOT / "plugins" / "github_midwife_plugin" / "src"), str(_ROOT), str(Path(__file__).resolve().parent)]
from _step5_support import bundle_digest, bundle_document, bundle_files, seal, git  # noqa: E402, I001
from github_midwife_plugin import existing_install_operations as seed_ops  # noqa: E402
from github_midwife_plugin import setup_adapter_contract as seed_contract  # noqa: E402
from solet_manager import adapter_protocol  # noqa: E402
from solet_manager.adapter_protocol import OperationRequest  # noqa: E402
from solet_manager.contracts import contract_digest, contract_digest_from_bytes, transition_bundle_filenames  # noqa: E402
from solet_manager.errors import AdapterProtocolError, ContractError, STEP5_REASON_CODES  # noqa: E402
from solet_manager.existing_install_bundle import (  # noqa: E402
    DECLARABLE_OPERATION_REFS,
    EXISTING_OPERATIONS,
    SEED_SIDE_OPERATION_REFS,
    parse_transition_bundle,
    transition_bundle_digest,
)
from solet_manager.update_runtime_plan import (  # noqa: E402
    REQUIRED_DISTRIBUTIONS,
    STEP5_CAPABILITIES,
    STEP5_MANAGED_SUB_SURFACES,
    STEP5_NON_TOUCH_SURFACES,
)

from bootstrap_adapter import dependency as bootstrap_dependency  # noqa: E402
from bootstrap_adapter import protocol as bootstrap_protocol  # noqa: E402

_KB = _ROOT / "plugins" / "github_midwife_plugin" / "knowledge_base"
_PUBLIC_R43_COMMIT = "8207c151255d5b969221886767c0d6d26684f253"
_PUBLIC_R43_TREE = "6fac8f34f7eb7a4d22cb3b108f915d8d89fb7fff"
_PUBLIC_R43_PREDECESSOR: dict[str, str | None] = {
    "repository": "https://github.com/solet-public/macos-bizops.git",
    "commit": _PUBLIC_R43_COMMIT,
    "tree": _PUBLIC_R43_TREE,
    "provenance_sha256": "f683c6c34f501d3983f55d1b255defd39e1c3bffad8faf0ef5f8f494e6004641",
    "seed_id": "72586d56-7826-5706-a210-68827676ffb0",
    "origin_id": "31bfa93c-fe20-4988-b019-f8186684e88e",
    "manifest_sha256": "6af4e689b3f1519b5a31eb854172aca354ecb79892acc24c84cd4c08d1c348c7",
    "legacy_anchor_id": None,
}
_MANAGER = _ROOT / "solet_cli" / "src" / "solet_manager"
_STEP5_MODULES = (
    "update_runtime_execution",
    "update_runtime_plan",
    "existing_install_adapters",
    "existing_install_bundle",
    "managed_artifact_backup",
    "reconciliation_request",
    "launch_topology",
)
_FORBIDDEN_NAMES = (
    "AdapterRegistry",
    "genesis::",
    "bootstrap::postgres",
    "credential_seed",
    "vault_passphrase",
    "router_install",
    "StateManagementInterface",
    "psycopg",
    "asyncpg",
    "PostgresProvider",
    "execute_sql",
    "psql",
    "security find-generic-password",
    "SecKeychain",
)
_SEED_DENYLIST = (
    "genesis",
    "credential_seed",
    "vault_passphrase_seed",
    "router_install",
    "git_init",
    "profile_install",
    "psycopg",
    "asyncpg",
    "ananta.interfaces.state_management_interface",
    "StateManagementInterface",
    "psql",
)
_checks = 0


def _check(condition: object, label: str) -> None:
    global _checks
    _checks += 1
    if not condition:
        raise AssertionError(label)


def _expect_contract_error(document: dict[str, Any], label: str) -> None:
    try:
        parse_transition_bundle(json.dumps(document).encode())
    except ContractError:
        _check(True, label)
        return
    raise AssertionError(f"accepted: {label}")


def _shipped_document() -> dict[str, Any]:
    return cast(dict[str, Any], json.loads((_KB / "existing_install_flow.json").read_text()))


def _directory_digest(files: dict[str, bytes]) -> str:
    """The name/NUL/bytes/NUL discipline recomputed over a materialised directory."""
    with TemporaryDirectory() as temporary:
        root = Path(temporary)
        for name, content in files.items():
            (root / name).write_bytes(content)
        digest = hashlib.sha256()
        for name in sorted(files):
            digest.update(name.encode())
            digest.update(b"\0")
            digest.update((root / name).read_bytes())
            digest.update(b"\0")
    return f"sha256:{digest.hexdigest()}"


def _check_shipped_bundle() -> None:
    document = _shipped_document()
    schema = json.loads((_KB / "existing_install_flow.schema.json").read_text())
    jsonschema.Draft7Validator(schema).validate(document)
    bundle = parse_transition_bundle((_KB / "existing_install_flow.json").read_bytes())
    _check(bundle.flow_id == "existing-install" and bundle.schema_version == 1, "shipped bundle identity")
    _check(
        bundle.predecessor_for(
            "fab22b6f2c832a5b86176f6916166f6c6bc7677b",
            "13461a6db6829e5d6546852fe49574adfaf40874",
        )
        is not None,
        "synthetic predecessor remains supported",
    )
    _check(
        bundle.predecessor_for("0" * 40, "0" * 40) is None,
        "unknown predecessor remains refused",
    )
    _check(
        bundle.predecessor_for(_PUBLIC_R43_COMMIT, "0" * 40) is None,
        "public r43 commit with wrong tree remains refused",
    )
    public_r43 = bundle.predecessor_for(_PUBLIC_R43_COMMIT, _PUBLIC_R43_TREE)
    _check(public_r43 is not None, "published public r43 predecessor is supported")
    assert public_r43 is not None
    _check(asdict(public_r43) == _PUBLIC_R43_PREDECESSOR, "public r43 descriptor matches published artefacts")
    _check(all(not artifact.in_target for artifact in bundle.managed_artifacts), "the current release declares no in-target artifact (section 6.3)")
    _check({item.artifact_id for item in bundle.managed_artifacts} == {"instance_launchagent_plist", "shell_startup_block", "user_claude_md_section"}, "shipped artifacts")
    _check(bundle.lifecycle.strategy == "router_preferred", "shipped lifecycle strategy")
    for artifact in bundle.managed_artifacts:
        template = _ROOT / artifact.template_ref
        _check(template.is_file(), f"template exists: {artifact.template_ref}")
        _check("sha256:" + hashlib.sha256(template.read_bytes()).hexdigest() == artifact.template_digest, f"template digest is exact: {artifact.artifact_id}")
    files = {name: (_KB / name).read_bytes() for name in transition_bundle_filenames()}
    from_bytes = transition_bundle_digest(files)
    _check(from_bytes == _directory_digest(files), "bytes-keyed digest equals the directory digest")
    _check(from_bytes != contract_digest(_KB), "transition digest is distinct from the create digest")
    _check(contract_digest_from_bytes({name: (_KB / name).read_bytes() for name in ("macos_setup_flow.json", "setup_flow.schema.json", "setup_answers.schema.json", "setup_journal.schema.json", "setup_adapter_envelope.schema.json")}) == contract_digest(_KB), "contract_digest is a thin wrapper over the bytes-keyed digest")
    _check(_stager_transition_digest() == from_bytes, "stage_release.py computes the same transition digest from the committed blobs")
    try:
        transition_bundle_digest({**files, "macos_setup_flow.json": b"{}"})
    except ContractError:
        _check(True, "transition digest refuses the create flow file")
    else:
        raise AssertionError("transition digest accepted macos_setup_flow.json")


def _stager_transition_digest() -> str:
    import importlib.util  # noqa: PLC0415

    spec = importlib.util.spec_from_file_location("_stage_release_under_test", _ROOT / "solet_cli" / "homebrew" / "scripts" / "stage_release.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    with TemporaryDirectory() as temporary:
        repo = Path(temporary) / "seed"
        repo.mkdir()
        git(repo, "init", "--quiet", "-b", "main")
        git(repo, "config", "user.name", "Fixture")
        git(repo, "config", "user.email", "fixture@example.invalid")
        files = {f"plugins/github_midwife_plugin/knowledge_base/{name}": (_KB / name).read_bytes() for name in transition_bundle_filenames()}
        for name in ("macos_setup_flow.json", "setup_flow.schema.json", "setup_answers.schema.json", "setup_journal.schema.json"):
            files[f"plugins/github_midwife_plugin/knowledge_base/{name}"] = (_KB / name).read_bytes()
        release = seal(repo, "a" * 40, "b" * 64, "r1", cast(dict[str, str | bytes], files))
        return cast(str, module._require_transition_bundle(repo, release.commit))  # noqa: SLF001


def _check_parser_refusals() -> None:
    with TemporaryDirectory() as temporary:
        repo = Path(temporary) / "seed"
        repo.mkdir()
        git(repo, "init", "--quiet", "-b", "main")
        git(repo, "config", "user.name", "Fixture")
        git(repo, "config", "user.email", "fixture@example.invalid")
        baseline = seal(repo, "a" * 40, "b" * 64, "r1", {"README.md": "n\n"})
    good = bundle_document(baseline)
    parse_transition_bundle(json.dumps(good).encode())
    _check(bundle_digest(bundle_files(good)) == transition_bundle_digest(bundle_files(good)), "support digest equals the Manager digest")

    def mutate(edit: Any) -> dict[str, Any]:
        document = copy.deepcopy(good)
        edit(document)
        return document

    _expect_contract_error(mutate(lambda d: d["runtime_operations"][0].__setitem__("operation_ref", "existing::not.a.member")), "unknown operation_ref refused")
    _expect_contract_error(mutate(lambda d: d["runtime_operations"][0].__setitem__("operation_ref", "existing::lifecycle.cutover")), "Manager-only ref not declarable")
    _expect_contract_error(mutate(lambda d: d["runtime_operations"][0].__setitem__("operation_ref", "genesis::solet.run")), "create vocabulary refused")
    _expect_contract_error(mutate(lambda d: d["runtime_operations"].reverse()), "non-monotonic stage order refused")
    _expect_contract_error(mutate(lambda d: d["runtime_operations"][0].__setitem__("mutation_class", "database_forward_only")), "database_forward_only without forward_only/manual refused")
    _expect_contract_error(mutate(lambda d: d["runtime_operations"][0].__setitem__("postcondition_probe_refs", [])), "missing postcondition probe refused")
    _expect_contract_error(mutate(lambda d: d.__setitem__("extra", 1)), "extra top-level key refused")
    _expect_contract_error(mutate(lambda d: d["managed_artifacts"][0].__setitem__("extra", 1)), "extra artifact key refused")
    _expect_contract_error(mutate(lambda d: d["runtime_operations"][0].__setitem__("idempotency_key", "sha256(anything)")), "non-template idempotency key refused")
    _expect_contract_error(mutate(lambda d: d["runtime_operations"][0].__setitem__("runner", "target_adapter")), "runner disagreeing with the registry refused")
    _expect_contract_error(mutate(lambda d: d["runtime_operations"][0].__setitem__("applies_when", {"predecessor_commits": ["9" * 40]})), "applies_when outside supported predecessors refused")
    _expect_contract_error(mutate(lambda d: d["lifecycle"].__setitem__("verification_modules", ["b.mod", "a.mod"])), "unsorted verification modules refused")
    for destination in (
        "{TARGET}/profile/config/service_bindings.json",
        "{TARGET}/profile/data/state.json",
        "{HOME}/.codex/sessions/x.jsonl",
        "{HOME}/.claude/projects/x/y.jsonl",
        "{TARGET}/AGENTS.md",
        "{TARGET}/CLAUDE.md",
        "{PROFILE_HOME}/config/x.json",
    ):
        _expect_contract_error(mutate(lambda d, destination=destination: d["managed_artifacts"][1].__setitem__("logical_destination", destination)), f"preserved-never destination refused: {destination}")
    _expect_contract_error(mutate(lambda d: d["managed_artifacts"][1].__setitem__("marker", {"begin": "# BEGIN SOLET {NAME}", "end": "# END SOLET {NAME}"})), "unversioned marker refused")
    _expect_contract_error(mutate(lambda d: d["managed_artifacts"][0].__setitem__("stamp", None)), "whole-file artifact without a stamp refused")
    _expect_contract_error(mutate(lambda d: d["supported_predecessors"].clear()), "no predecessors refused")


def _check_enumerations() -> None:
    _check(tuple(seed_ops.SEED_OPERATION_REFS) == SEED_SIDE_OPERATION_REFS, "Manager and seed existing:: enumerations (seed-side subset) are byte-equal")
    _check(set(seed_ops.operation_handlers()) | {"existing::dependencies.reconcile"} == set(SEED_SIDE_OPERATION_REFS), "every seed-side ref has a handler or the bootstrap route")
    _check(set(seed_ops.EXISTING_ALLOWED_PUBLIC_INPUTS) == set(seed_ops.operation_handlers()), "every seed handler declares its allowed public inputs")
    _check(bootstrap_dependency.EXISTING_DEPENDENCIES_REF == "existing::dependencies.reconcile", "bootstrap route name")
    _check(len({item.operation_ref for item in EXISTING_OPERATIONS}) == len(EXISTING_OPERATIONS) == 13, "thirteen unique closed members")
    _check(set(DECLARABLE_OPERATION_REFS) == {"existing::dependencies.reconcile", "existing::migration.solet_rename", "existing::migration.export_root_containment", "existing::migration.plugin_transition", "existing::hydration.reconcile", "existing::autostart.reconcile", "existing::runtime.platform_migration", "existing::runtime.plugin_cache_refresh"}, "declarable subset")
    _check(REQUIRED_DISTRIBUTIONS == bootstrap_dependency.REQUIRED_DISTRIBUTIONS, "Manager REQUIRED_DISTRIBUTIONS equals the bootstrap adapter's")
    _check(len(STEP5_NON_TOUCH_SURFACES) == 13 and len(STEP5_MANAGED_SUB_SURFACES) == 11 and len(STEP5_CAPABILITIES) == 11, "closed Step-5 surface constants")
    _check("dependencies_and_venv" in STEP5_MANAGED_SUB_SURFACES and "dependencies_and_venv" not in set[str](STEP5_NON_TOUCH_SURFACES), "the venv moved from non-touch to managed")
    _check(len(STEP5_REASON_CODES) == len(set(STEP5_REASON_CODES)) and "in_target_destination_not_ignored" in STEP5_REASON_CODES, "closed reason codes")


def _collected_names(tree: ast.AST) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            names.add(node.id)
        elif isinstance(node, ast.Attribute):
            names.add(node.attr)
        elif isinstance(node, ast.ImportFrom | ast.Import):
            names.update(_import_names(node))
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            names.add(node.value)
    return names


def _import_names(node: ast.ImportFrom | ast.Import) -> set[str]:
    names = {alias.name for alias in node.names}
    if isinstance(node, ast.ImportFrom) and node.module:
        names.add(node.module)
    return names


def _check_module_reach(module_name: str) -> None:
    source = (_MANAGER / f"{module_name}.py").read_text(encoding="utf-8")
    names = _collected_names(ast.parse(source))
    for forbidden in _FORBIDDEN_NAMES:
        # Identifier-shaped members match whole names (``ExistingInstallAdapterRegistry``
        # is not ``AdapterRegistry``); vocabulary prefixes and command names match anywhere.
        exact = forbidden.isidentifier()
        hits = [name for name in names if (name == forbidden if exact else forbidden in name)]
        _check(not hits, f"{module_name} reaches forbidden {forbidden!r}: {hits}")
    _check("from .adapters import" not in source, f"{module_name} never imports the create adapter module")


def _check_static_reachability() -> None:
    for module_name in _STEP5_MODULES:
        _check_module_reach(module_name)
    execution_source = (_MANAGER / "update_execution.py").read_text(encoding="utf-8")
    _check(re.search(r"\bAdapterRegistry\b", execution_source) is None, "update_execution never names the create registry")
    _check_seed_imports()


#: The dispatch table module and every handler module it imports (the plugin transition added two, iss_6d26db73).
_SEED_HANDLER_MODULES = (
    "existing_install_operations",
    "existing_install_migrations",
    "existing_install_plugin_transitions",
    "plugin_transition_declaration",
    "apple_setup_adapter",
)


def _check_seed_imports() -> None:
    for module_name in _SEED_HANDLER_MODULES:
        seed_source = (_ROOT / "plugins" / "github_midwife_plugin" / "src" / "github_midwife_plugin" / f"{module_name}.py").read_text(encoding="utf-8")
        imported: set[str] = set()
        for node in ast.walk(ast.parse(seed_source)):
            if isinstance(node, ast.ImportFrom | ast.Import):
                imported.update(_import_names(node))
        for forbidden in _SEED_DENYLIST:
            hits = [name for name in imported if forbidden in name]
            _check(not hits, f"seed handler module {module_name} imports denylisted {forbidden!r}: {hits}")
        _check("psql" not in seed_source, f"seed handler module {module_name} never names psql")


def _manager_request(flow_id: str, ref: str, purpose: str | None = "preview") -> OperationRequest:
    return OperationRequest(str(uuid.uuid4()), "op_fixture", ref, "probe", purpose, 1, "fixture", "/tmp/fixture", flow_id, "a" * 40, "sha256:" + "b" * 64, None, True, 30, {})


def _seed_request(flow_id: str, ref: str) -> dict[str, object]:
    return {
        "protocol_version": 1,
        "kind": "operation_request",
        "request_id": str(uuid.uuid4()),
        "operation_id": "op_fixture",
        "operation_ref": ref,
        "phase": "probe",
        "probe_purpose": "preview",
        "attempt": 1,
        "name": "fixture",
        "target": "/tmp/fixture",
        "flow_id": flow_id,
        "flow_source_revision": "a" * 40,
        "answers_fingerprint": "sha256:" + "b" * 64,
        "approval_fingerprint": None,
        "dry_run": True,
        "timeout_seconds": 30,
        "public_inputs": {},
    }


def _check_three_validators() -> None:
    pairs = (("macos.repository_setup", "setup::tmux.install", True), ("macos.repository_setup", "existing::hydration.reconcile", False), ("existing-install", "existing::hydration.reconcile", True), ("existing-install", "setup::tmux.install", False), ("existing-install", "genesis::solet.run", False), ("existing-install", "hydration::shell.install", False), ("macos.target_reconciliation", "existing::hydration.reconcile", False), ("other-flow", "existing::hydration.reconcile", False))
    for flow_id, ref, accepted in pairs:
        manager_ok = True
        try:
            _manager_request(flow_id, ref).validate()
        except AdapterProtocolError:
            manager_ok = False
        seed_ok = True
        try:
            seed_contract.AdapterRequest.from_dict(_seed_request(flow_id, ref))
        except seed_contract.AdapterInputError:
            seed_ok = False
        bootstrap_ok = True
        try:
            bootstrap_protocol.validate_request(_seed_request(flow_id, ref))
        except bootstrap_protocol.AdapterRequestError:
            bootstrap_ok = False
        _check(manager_ok is accepted, f"Manager validator: {flow_id} + {ref} -> {accepted}")
        _check(seed_ok is accepted, f"seed validator: {flow_id} + {ref} -> {accepted}")
        _check(bootstrap_ok is accepted, f"bootstrap validator: {flow_id} + {ref} -> {accepted}")
    _check(adapter_protocol.FLOW_OPERATION_PAIRING == seed_contract.FLOW_OPERATION_PAIRING == bootstrap_protocol.FLOW_OPERATION_PAIRING == {"macos.repository_setup": False, "existing-install": True}, "all three pairing tables are the same closed two-member set")
    for purpose in ("preview", "pre_apply", "post_apply", "stage_entry", "stage_exit"):
        _manager_request("existing-install", "existing::hydration.reconcile", purpose).validate()
        _check(purpose in adapter_protocol._PROBE_PURPOSES, f"probe purpose {purpose} is in the closed set")  # noqa: SLF001
    try:
        _manager_request("existing-install", "existing::hydration.reconcile", "runtime_preview").validate()
    except AdapterProtocolError:
        _check(True, "an invented probe purpose is refused")
    else:
        raise AssertionError("invented probe purpose accepted")


def main() -> int:
    _check_shipped_bundle()
    _check_parser_refusals()
    _check_enumerations()
    _check_static_reachability()
    _check_three_validators()
    print(f"existing_install_bundle_smoke OK: {_checks} checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
