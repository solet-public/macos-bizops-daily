"""Foreign-target technology fingerprint and capability-routing smoke.

Hermetic: temporary trees, no network, no subprocess, no target execution.  The
plugin end-to-end path uses a stub scanner and an in-memory state-service fake.
"""

from __future__ import annotations

# ruff: noqa: E402
import json
import sys
import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

_PLUGIN_ROOT = Path(__file__).resolve().parents[1]
_SRC = _PLUGIN_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

import code_vetting_plugin.plugin as plugin_module
from code_vetting_plugin.live_state import read_vetting_run
from code_vetting_plugin.models import ContextProfile
from code_vetting_plugin.report import ReportRenderer
from code_vetting_plugin.run_record import CoverageRecord, RunTarget
from code_vetting_plugin.runner import L1ReportData
from code_vetting_plugin.source_roles import SourceRole, parse_role_overrides
from code_vetting_plugin.targets import TargetTree
from code_vetting_plugin.technology_fingerprint import (
    MAX_BOUNDED_EXAMPLES,
    MAX_CONTENT_BYTES,
    MAX_REPORT_ROUTE_INVENTORIES,
    build_technology_fingerprint,
    render_technology_fingerprint_section,
)

_CHECKS_RUN: list[str] = []


class SmokeFailureError(AssertionError):
    """Raised when a technology-fingerprint invariant fails."""


def _check(label: str, condition: bool, detail: str = "") -> None:
    _CHECKS_RUN.append(label)
    if not condition:
        raise SmokeFailureError(f"{label}: {detail}")


def _write(root: Path, rel: str, content: str | bytes, tracked: list[str]) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, bytes):
        path.write_bytes(content)
    else:
        path.write_text(content, encoding="utf-8")
    tracked.append(rel)


def _json(root: Path, rel: str, payload: object, tracked: list[str]) -> None:
    _write(root, rel, json.dumps(payload, sort_keys=True), tracked)


def _override(prefix: str, role: SourceRole) -> dict[str, str]:
    return {
        "path_prefix": prefix,
        "role": role.value,
        "doctrine_locator": "docs/architecture.md#source-boundaries",
        "declaring_session": "technology-fingerprint-smoke",
    }


def _fixture(root: Path) -> tuple[TargetTree, tuple[object, ...]]:
    tracked: list[str] = []
    package = {
        "name": "cultivar-like",
        "dependencies": {
            "@supabase/supabase-js": "^2.110.1",
            "expo": "^56.0.0",
            "expo-router": "~56.2.14",
            "react": "19.2.3",
            "react-native": "0.85.3",
        },
        "devDependencies": {
            "eslint": "^9.0.0",
            "jest": "^30.0.0",
            "ts-jest": "^29.0.0",
            "typescript": "~6.0.3",
        },
    }
    lock_packages = {
        "": {"name": "cultivar-like"},
        "node_modules/@supabase/supabase-js": {"version": "2.110.4"},
        "node_modules/expo": {"version": "56.0.1"},
        "node_modules/expo-router": {"version": "56.2.14"},
        "node_modules/react": {"version": "19.2.3"},
        "node_modules/react-native": {"version": "0.85.3"},
        "node_modules/react-native-web": {"version": "0.21.1"},
        "node_modules/eslint": {"version": "9.4.0"},
        "node_modules/jest": {"version": "30.0.2"},
        "node_modules/ts-jest": {"version": "29.4.1"},
        "node_modules/typescript": {"version": "6.0.3"},
    }
    _json(root, "package.json", package, tracked)
    _json(
        root,
        "package-lock.json",
        {"lockfileVersion": 3, "packages": lock_packages},
        tracked,
    )
    _json(root, "app.json", {"expo": {"name": "Cultivar"}}, tracked)
    _json(root, "eas.json", {"build": {}}, tracked)
    _json(root, "tsconfig.json", {"compilerOptions": {"strict": True}}, tracked)
    _json(root, "deno.json", {"tasks": {"test": "deno test"}}, tracked)
    _json(root, "deno.lock", {"version": "4", "specifiers": {}}, tracked)
    _write(root, "eslint.config.js", "throw new Error('must not execute')\n", tracked)
    _write(root, "jest.config.ts", "throw new Error('must not execute')\n", tracked)
    _write(root, "babel.config.js", "throw new Error('must not execute')\n", tracked)

    for index in range(MAX_BOUNDED_EXAMPLES + 4):
        _write(root, f"app/route-{index}.tsx", "export default null\n", tracked)
    _write(root, "app/evil|name<script>.tsx", "export default null\n", tracked)

    _json(
        root,
        "examples/package.json",
        {
            "name": "reference-router",
            "dependencies": {"expo-router": "^9.9.9", "react": "^18.0.0"},
        },
        tracked,
    )
    _write(root, "examples/app/reference.tsx", "export default null\n", tracked)

    _json(
        root,
        "evil|scope<script>/package.json",
        {"dependencies": {"react": "^17.0.0"}},
        tracked,
    )
    for index in range(MAX_REPORT_ROUTE_INVENTORIES + 3):
        scope = f"many-route-scope-{index:02d}"
        _json(
            root,
            f"{scope}/package.json",
            {"dependencies": {"expo-router": "^56.0.0"}},
            tracked,
        )
        _write(root, f"{scope}/app/index.tsx", "export default null\n", tracked)
    _json(root, "supabase/functions/deno.json", {"lint": {"rules": {}}}, tracked)
    _json(root, "supabase/functions/deno.lock", {"version": "4"}, tracked)
    _write(root, "supabase/config.toml", 'project_id = "local"\n', tracked)
    _write(
        root,
        "supabase/functions/ingest-coa/index.ts",
        "Deno.serve(() => new Response('ok'))\n",
        tracked,
    )
    _write(
        root,
        "supabase/migrations/20260726000000_rls.sql",
        " ".join(
            ("alter", "table", "samples", "enable", "row", "level", "security;")
        )
        + "\n",
        tracked,
    )

    _write(root, "bad/package.json", "{", tracked)
    _write(root, "duplicate/app.json", '{"expo":{},"expo":{}}', tracked)
    _write(
        root,
        "comments/tsconfig.json",
        '{\n  // TypeScript accepts this JSONC comment.\n  "compilerOptions": {}\n}\n',
        tracked,
    )
    _write(
        root,
        "huge/package-lock.json",
        b"{" + (b" " * (MAX_CONTENT_BYTES + 1)),
        tracked,
    )
    _write(root, "supabase/functions/deno.jsonc", "{ // comment\n}\n", tracked)
    symlink = root / "linked" / "app.json"
    symlink.parent.mkdir(parents=True)
    symlink.symlink_to(root / "app.json")
    tracked.append("linked/app.json")

    tree = TargetTree(
        root=root,
        tracked=tuple(sorted(tracked)),
        enumeration="walk",
        foreign=True,
    )
    overrides = parse_role_overrides(
        [
            _override("app", SourceRole.PRODUCT),
            _override("examples", SourceRole.EXAMPLES_REFERENCE),
            _override("supabase/migrations", SourceRole.PRODUCT),
        ],
        tree,
        foreign=True,
    )
    return tree, overrides


def _mapping_by_key(items: object, key: str, value: str) -> list[dict[str, object]]:
    assert isinstance(items, list)
    return [dict(item) for item in items if isinstance(item, dict) and item.get(key) == value]


def _check_reconciliation(fingerprint: dict[str, object]) -> None:
    reconciliation = fingerprint["reconciliation"]
    assert isinstance(reconciliation, dict)
    _check(
        "probe/component/capability reconciliation is exact",
        reconciliation["reconciles"] is True
        and reconciliation["probe_roster"]["total"] == 13
        and reconciliation["probe_candidates"]["reconciles"] is True
        and reconciliation["components"]["reconciles"] is True
        and reconciliation["capability_needs"]["reconciles"] is True,
        str(reconciliation),
    )


def _check_component_versions(fingerprint: dict[str, object]) -> None:
    components = fingerprint["components"]
    supabase = _mapping_by_key(components, "component_key", "supabase_js")
    _check(
        "scoped package name resolves from the co-scoped npm v3 packages record",
        len(supabase) == 1
        and supabase[0]["declared_version"]["value"] == "^2.110.1"
        and supabase[0]["resolved_version"]["value"] == "2.110.4",
        str(supabase),
    )
    lock_only = _mapping_by_key(components, "component_key", "react_native_web")
    _check(
        "lock-only component is never represented as direct",
        len(lock_only) == 1
        and lock_only[0]["status"] == "lockfile_only"
        and lock_only[0]["relationship"] == "transitive_or_lock_only"
        and lock_only[0]["declared_version"]["status"] == "not_declared",
        str(lock_only),
    )


def _check_non_product_component_role(components: object) -> None:
    example_router = [
        item
        for item in _mapping_by_key(components, "component_key", "expo_router")
        if item["scope"] == "examples"
    ]
    _check(
        "confirmed non-product manifest stays visible but cannot support product claims",
        len(example_router) == 1
        and example_router[0]["source_role"]["role"] == "examples_reference"
        and example_router[0]["source_role"]["product_claim_support"] is False
        and example_router[0]["source_role"]["routing_eligible"] is False,
        str(example_router),
    )


def _check_unconfirmed_component_role(components: object) -> None:
    root_expo = [
        item
        for item in _mapping_by_key(components, "component_key", "expo")
        if item["scope"] == "."
    ]
    _check(
        "unconfirmed component facts are routing eligible but support no claims or findings",
        len(root_expo) == 1
        and root_expo[0]["source_role"]["status"] == "unconfirmed"
        and root_expo[0]["source_role"]["routing_eligible"] is True
        and root_expo[0]["source_role"]["product_claim_support"] is False
        and root_expo[0]["source_role"]["finding_support"] is False,
        str(root_expo),
    )


def _check_component_roles(fingerprint: dict[str, object]) -> None:
    components = fingerprint["components"]
    _check(
        "every component leaves source usage unconfirmed",
        all(item["source_usage_status"] == "unconfirmed" for item in components),
    )
    _check_non_product_component_role(components)
    _check_unconfirmed_component_role(components)


def _check_runtime_topology(fingerprint: dict[str, object]) -> None:
    deno_scopes = {item["scope"] for item in fingerprint["deno_runtime_scopes"]}
    _check(
        "nested Deno runtime scopes remain distinct",
        {".", "supabase/functions"} <= deno_scopes,
        str(sorted(deno_scopes)),
    )
    topology = fingerprint["supabase_topology"]
    _check(
        "Supabase topology is separate from component declarations",
        topology[0]["scope"] == "."
        and topology[0]["edge_functions"]["unconfirmed"]["count"] == 1
        and topology[0]["migrations"]["product"]["count"] == 1,
        str(topology),
    )


def _check_routes(fingerprint: dict[str, object]) -> None:
    route_by_scope = {item["scope"]: item for item in fingerprint["route_inventories"]}
    many_route_scopes = {
        scope: inventory
        for scope, inventory in route_by_scope.items()
        if scope.startswith("many-route-scope-")
    }
    _check(
        "route inventory is bounded and grouped only by confirmed overrides",
        route_by_scope["."]["routes"]["product"]["count"] == MAX_BOUNDED_EXAMPLES + 5
        and route_by_scope["."]["routes"]["product"]["omitted"] > 0
        and route_by_scope["examples"]["routes"]["non_product"]["count"] == 1,
        str(route_by_scope),
    )
    _check(
        "structured payload retains every bounded many-scope route inventory",
        len(many_route_scopes) == MAX_REPORT_ROUTE_INVENTORIES + 3
        and all(
            inventory["routes"]["unconfirmed"]
            == {"count": 1, "examples": [f"{scope}/app/index.tsx"], "omitted": 0}
            for scope, inventory in many_route_scopes.items()
        ),
        str(many_route_scopes),
    )


def _check_unreadable_gaps(fingerprint: dict[str, object]) -> None:
    gaps = fingerprint["unreadable_gaps"]
    gap_reasons = {(item["probe"], item.get("reason")) for item in gaps}
    _check(
        "malformed duplicate oversize symlink and JSONC are isolated visible gaps",
        {
            ("package_json", "invalid_strict_json"),
            ("expo_app_json", "duplicate_json_key"),
            ("package_lock", "content_too_large"),
            ("expo_app_json", "symlink_not_followed"),
            ("deno_manifest", "unsupported_jsonc"),
            ("tsconfig_json", "unsupported_jsonc"),
        }
        <= gap_reasons,
        str(sorted(gap_reasons)),
    )
    comment_gap = [
        item
        for item in gaps
        if item["probe"] == "tsconfig_json" and item.get("path") == "comments/tsconfig.json"
    ]
    _check(
        "comment-bearing tsconfig is explicitly unsupported_jsonc",
        len(comment_gap) == 1 and comment_gap[0]["reason"] == "unsupported_jsonc",
        str(comment_gap),
    )
    _check(
        "unreadable probes do not suppress facts from readable peers",
        bool(_mapping_by_key(fingerprint["components"], "component_key", "expo")),
    )


# The expected status of every capability selected at the root scope. A table rather than a
# chain of `and`s so adding a capability does not add a branch to the assertion.
_EXPECTED_ROOT_STATUSES: dict[str, str] = {
    "expo_project_health": "adapter_missing",
    "foreign_framework_architecture": "dynamic_guidance_required",
    "deno_check_lint_test": "adapter_missing",
    "supabase_db_rls_static_review": "adapter_missing",
    # Performance BENCHMARKING is out of scope by ruling — `adapter_missing` here would
    # assert an intent we do not have. See _technology_fingerprint_capabilities.
    "runtime_performance": "out_of_scope",
    "typescript_target_toolchain": "available_opt_in",
}


def _statuses_at(needs: Sequence[Mapping[str, object]], scope: str) -> dict[str, str]:
    return {str(item["capability_key"]): str(item["status"]) for item in needs if item["scope"] == scope}


def _check_capability_statuses(fingerprint: dict[str, object]) -> None:
    needs = fingerprint["capability_needs"]
    assert isinstance(needs, list)
    need_status = _statuses_at(needs, ".")
    _check(
        "minimum capability needs are selected with fixed statuses",
        all(need_status.get(key) == want for key, want in _EXPECTED_ROOT_STATUSES.items()),
        str(need_status),
    )
    _check_runtime_performance_at_both_sites(needs)


def _check_runtime_performance_at_both_sites(needs: Sequence[Mapping[str, object]]) -> None:
    """SCOPE-COSTUME GUARD (2026-07-31).

    `runtime_performance` is emitted at TWO sites — `_component_capability_rows` (expo) and
    `_deno_capability_rows` (deno). At scope "." BOTH emit, but `_ordered_capabilities` dedups
    on (scope, capability_key) first-row-wins and expo rows are appended first, so the deno
    row's STATUS is discarded there. The root-scope assertion therefore covers the EXPO site
    only: reverting the deno site alone left the whole suite GREEN (measured). `supabase/functions`
    is the one scope where the deno site's status is observable, so it is asserted by name.
    """
    deno_only = _statuses_at(needs, "supabase/functions")
    _check(
        "the deno emission site's runtime_performance status is out_of_scope (not the expo row)",
        deno_only.get("runtime_performance") == "out_of_scope",
        f"deno-only scope statuses: {deno_only}",
    )
    seen = {str(item["status"]) for item in needs if item["capability_key"] == "runtime_performance"}
    _check(
        "no capability row anywhere still claims runtime_performance needs an adapter",
        seen == {"out_of_scope"},
        str(sorted(seen)),
    )


def _check_capability_policy(fingerprint: dict[str, object]) -> None:
    needs = fingerprint["capability_needs"]
    _check(
        "routing never claims execution pass or clean",
        all(
            item["execution_status"] == "not_run_by_fingerprint"
            and item["routing"]["is_execution"] is False
            and item["trigger_evidence"]
            for item in needs
        ),
    )
    _check(
        "recognized executable configs are presence-only and unexecuted",
        all(
            item["status"] == "presence_only_unmodeled"
            and item["execution_status"] == "not_executed"
            for item in fingerprint["recognized_but_unmodeled_configs"]
        ),
    )
    _check(
        "fingerprint does not duplicate source-role platform-variant counts",
        "platform_variants" not in fingerprint,
    )
    policy = fingerprint["source_role_policy"]
    _check(
        "structured first-touch policy routes unconfirmed facts without claim authority",
        policy["unconfirmed_facts_capability_routing_eligible_on_first_touch"] is True
        and policy["unconfirmed_facts_support_product_claims"] is False
        and policy["unconfirmed_facts_support_findings"] is False
        and policy["confirmed_non_product_routing_eligible"] is False,
        str(policy),
    )


def _check_facts(tree: TargetTree, overrides: tuple[object, ...]) -> dict[str, object]:
    fingerprint = build_technology_fingerprint(
        tree=tree,
        overrides=overrides,  # type: ignore[arg-type] - parsed RoleOverride tuple
    )
    second = build_technology_fingerprint(
        tree=tree,
        overrides=overrides,  # type: ignore[arg-type]
    )
    _check(
        "payload is deterministic and carries no wall clock",
        fingerprint == second and "generated_at" not in json.dumps(fingerprint),
    )
    _check_reconciliation(fingerprint)
    _check_component_versions(fingerprint)
    _check_component_roles(fingerprint)
    _check_runtime_topology(fingerprint)
    _check_routes(fingerprint)
    _check_unreadable_gaps(fingerprint)
    _check_capability_statuses(fingerprint)
    _check_capability_policy(fingerprint)
    return fingerprint


def _check_rendered_evidence_separators(rendered: str) -> None:
    _check(
        "rendered trigger evidence separates path from JSON/file pointer",
        "package.json#/dependencies/expo" in rendered and "supabase/config.toml#$file" in rendered,
        rendered,
    )


def _check_route_report_bound(rendered: str, fingerprint: dict[str, object]) -> None:
    route_section = rendered.split(
        "| route scope | product | non-product | unconfirmed | content read |",
        maxsplit=1,
    )[1].split("### Deterministic capability needs", maxsplit=1)[0]
    rendered_rows = [
        line
        for line in route_section.splitlines()
        if line.startswith("| ") and not line.startswith("| ---")
    ]
    route_count = len(fingerprint["route_inventories"])
    omitted = route_count - MAX_REPORT_ROUTE_INVENTORIES
    _check(
        "route report rows are capped with exact omission disclosure",
        len(rendered_rows) == MAX_REPORT_ROUTE_INVENTORIES
        and omitted > 0
        and (
            f"{omitted} additional route inventories omitted from markdown; "
            "all remain in target.technology_fingerprint.route_inventories."
        )
        in route_section,
        route_section,
    )


def _check_report_disclosures(rendered: str) -> None:
    _check(
        "report explicitly discloses mutable/no-atomic/no-execution/no-network",
        "mutable worktree" in rendered
        and "no atomic snapshot" in rendered
        and "did not execute target code" in rendered
        and "use the network" in rendered
        and "Routing is not execution" in rendered,
    )
    _check(
        "report makes the ratified first-touch source-role policy prominent",
        "**Bootstrap source-role policy:**" in rendered
        and "unconfirmed facts are routing-eligible for capability needs" in rendered
        and "never support product claims or findings" in rendered,
        rendered[:2000],
    )


def _check_self_report() -> None:
    self_target = RunTarget(repo="example", ref="HEAD", scope="self")
    _check(
        "self target JSON omits fingerprint and preserves the three-key shape",
        self_target.to_dict() == {"repo": "example", "ref": "HEAD", "scope": "self"},
        str(self_target.to_dict()),
    )
    self_report = ReportRenderer().render(
        run_id="vr-self",
        target=self_target,
        context_profile=ContextProfile.PRODUCTION,
        generated_at="2026-07-26T00:00:00Z",
        findings=[],
        coverage=[],
    )
    _check(
        "self report remains fingerprint-free",
        "Technology fingerprint" not in self_report,
    )


def _check_report(fingerprint: dict[str, object]) -> None:
    target = RunTarget(
        repo="foreign",
        ref="deadbeef",
        scope="foreign",
        technology_fingerprint=fingerprint,
    )
    coverage = [CoverageRecord(scanner="stub", ran=False, files_examined=0, gap_reason="not run")]
    before = [record.to_dict() for record in coverage]
    report = ReportRenderer().render(
        run_id="vr-fingerprint",
        target=target,
        context_profile=ContextProfile.PRODUCTION,
        generated_at="2026-07-26T00:00:00Z",
        findings=[],
        coverage=coverage,
    )
    _check(
        "fingerprint rendering never mutates scanner coverage",
        [record.to_dict() for record in coverage] == before,
    )
    _check(
        "fingerprint section immediately follows header and precedes Summary and Findings",
        report.index("## Technology fingerprint and capability routing")
        < report.index("## Summary")
        < report.index("## Findings"),
    )
    _check(
        "hostile markdown from paths/scopes is escaped",
        "<script>" not in report and "evil|scope" not in report and "evil&#124;scope" in report,
        report[:4000],
    )
    rendered = render_technology_fingerprint_section(fingerprint)
    _check_rendered_evidence_separators(rendered)
    _check_route_report_bound(rendered, fingerprint)
    _check_report_disclosures(rendered)
    _check_self_report()


def _ok(data: dict[str, Any]) -> dict[str, Any]:
    return {
        "action_status": "completed",
        "data": data,
        "actions": [],
        "error": None,
        "timestamp": "",
    }


class _State:
    def __init__(self) -> None:
        self.rows: dict[str, dict[str, Any]] = {}

    def upsert_state(self, namespace: str, payload: dict[str, Any]) -> dict[str, Any]:
        del namespace
        row = dict(payload["record"])
        self.rows[str(row["run_id"])] = row
        return _ok({})

    def query_ordered(self, namespace: str, payload: dict[str, Any]) -> dict[str, Any]:
        del namespace
        rows = sorted(self.rows.values(), key=lambda row: (row["started"], row["run_id"]))
        return _ok({"records": rows[: int(payload["limit"])]})

    def query_state(self, namespace: str, payload: dict[str, Any]) -> dict[str, Any]:
        del namespace
        run_id = str(payload["filters"]["run_id"])
        return _ok({"records": [self.rows[run_id]] if run_id in self.rows else []})

    def delete_records(self, namespace: str, payload: dict[str, Any]) -> dict[str, Any]:
        del namespace
        run_id = str(payload["filters"]["run_id"])
        self.rows.pop(run_id, None)
        return _ok({"deleted": 1})


class _Plugin(plugin_module.CodeVettingPlugin):
    def __init__(self, state: _State, worktree: Path) -> None:
        super().__init__()
        self._fake_state = state
        self._worktree_root = worktree

    def _state_service(self) -> Any:  # noqa: ANN401
        return self._fake_state


def _stub_scan(
    tree: TargetTree, run_id: str
) -> tuple[list[Any], list[CoverageRecord], L1ReportData]:
    del run_id
    return (
        [],
        [CoverageRecord(scanner="stub", ran=True, files_examined=len(tree.all_files()))],
        L1ReportData(),
    )


def _check_plugin_round_trip(root: Path) -> None:
    state = _State()
    worktree = root.parent / "worktree"
    worktree.mkdir()
    plugin = _Plugin(state, worktree)
    payload = plugin._run(  # noqa: SLF001
        _stub_scan,
        scope="foreign technology smoke",
        tag="fingerprint",
        target_path=str(root),
        role_overrides=[
            _override("app", SourceRole.PRODUCT),
            _override("examples", SourceRole.EXAMPLES_REFERENCE),
            _override("supabase/migrations", SourceRole.PRODUCT),
        ],
        persist=True,
    )
    target = payload["target"]
    _check(
        "foreign inline target carries technology_fingerprint",
        isinstance(target, dict) and "technology_fingerprint" in target,
    )
    row = read_vetting_run(state, str(payload["run_id"]))
    assert row is not None
    _check(
        "persisted target JSON round-trips the exact inline fingerprint",
        row["target"]["technology_fingerprint"] == target["technology_fingerprint"],
    )
    report = str(payload["report"])
    _check(
        "end-to-end report keeps fingerprint before Summary after inline bounding",
        report.index("## Technology fingerprint and capability routing")
        < report.index("## Summary"),
    )


def _check_engine_invariant(root: Path) -> None:
    duplicate_tree = TargetTree(
        root=root,
        tracked=("package.json", "package.json"),
        enumeration="walk",
        foreign=True,
    )
    raised = False
    try:
        build_technology_fingerprint(tree=duplicate_tree, overrides=())
    except RuntimeError:
        raised = True
    _check("engine enumeration invariant defects fail loudly", raised)


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="technology-fingerprint-smoke-") as tmp:
        root = Path(tmp) / "target"
        root.mkdir()
        tree, overrides = _fixture(root)
        fingerprint = _check_facts(tree, overrides)
        _check_report(fingerprint)
        _check_plugin_round_trip(root)
        _check_engine_invariant(root)
    print(f"technology_fingerprint_smoke: PASS ({len(_CHECKS_RUN)} checks)")


if __name__ == "__main__":
    main()
