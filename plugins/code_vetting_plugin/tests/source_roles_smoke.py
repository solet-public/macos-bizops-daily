"""Foreign source-role/platform-variant attribution wedge smoke.

Pins the report-stage invariants: overrides never suppress repo risk, only
surface dimensions partition, path conventions remain annotations, platform
suffixes are independent evidence, persistence rides the existing target JSON,
and the self-vet shape/report remain unchanged.
"""

from __future__ import annotations

# ruff: noqa: E402
import sys
import tempfile
from pathlib import Path
from typing import Any

_PLUGIN_ROOT = Path(__file__).resolve().parents[1]
_SRC = _PLUGIN_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from code_vetting_plugin.live_state import read_vetting_run
from code_vetting_plugin.models import (
    ContextProfile,
    Dimension,
    Finding,
    Layer,
    Provenance,
    Severity,
)
from code_vetting_plugin.plugin import CodeVettingPlugin
from code_vetting_plugin.report import ReportRenderer
from code_vetting_plugin.run_record import CoverageRecord, RunTarget, counts_by_dimension
from code_vetting_plugin.runner import L1ReportData
from code_vetting_plugin.source_roles import (
    DEFERRED_L2_DIMENSIONS,
    MAX_EXAMPLES_PER_BUCKET,
    MAX_PREFIX_LENGTH,
    MAX_PROVENANCE_LENGTH,
    MAX_ROLE_OVERRIDES,
    REPO_SCOPE_DIMENSIONS,
    SURFACE_SCOPE_DIMENSIONS,
    PlatformVariant,
    RoleOverrideValidationError,
    SourceRole,
    build_source_role_partition,
    candidate_role_for_path,
    confirmed_role_for_path,
    parse_role_overrides,
    platform_variant_for_path,
    prefix_matches,
)
from code_vetting_plugin.targets import TargetTree

_CHECKS_RUN: list[str] = []


class SmokeFailureError(AssertionError):
    """Raised on any check failure."""


def _check(label: str, condition: bool, detail: str = "") -> None:
    _CHECKS_RUN.append(label)
    if not condition:
        raise SmokeFailureError(f"{label}: {detail}")


def _finding(
    run_id: str,
    *,
    file: str,
    dimension: Dimension,
    constraint: str,
) -> Finding:
    return Finding.build(
        run_id=run_id,
        layer=Layer.L1_DETERMINISTIC,
        dimension=dimension,
        severity=Severity.MEDIUM,
        file=file,
        line=1,
        constraint_violated=constraint,
        evidence=f"{file} demonstrates {constraint}",
        provenance=Provenance(source="source-role-smoke"),
        context_profile=ContextProfile.PRODUCTION,
    )


def _override(prefix: str, role: SourceRole) -> dict[str, str]:
    return {
        "path_prefix": prefix,
        "role": role.value,
        "doctrine_locator": "docs/architecture.md#source-boundaries",
        "declaring_session": "Reviewer-A/agt-source-role-smoke",
    }


def _tree(paths: tuple[str, ...]) -> TargetTree:
    return TargetTree(
        root=Path("/not-read-by-source-role-smoke"),
        tracked=paths,
        enumeration="walk",
        foreign=True,
    )


def _expect_override_error(label: str, raw: object, tree: TargetTree, *, foreign: bool = True) -> None:
    _CHECKS_RUN.append(label)
    try:
        parse_role_overrides(raw, tree, foreign=foreign)
    except RoleOverrideValidationError:
        return
    raise SmokeFailureError(f"{label}: expected RoleOverrideValidationError")


def _check_policy_and_validation() -> None:
    _check(
        "dimension policy is exhaustive",
        REPO_SCOPE_DIMENSIONS | SURFACE_SCOPE_DIMENSIONS | DEFERRED_L2_DIMENSIONS
        == frozenset(Dimension),
    )
    _check(
        "dimension policy is pairwise disjoint",
        not (REPO_SCOPE_DIMENSIONS & SURFACE_SCOPE_DIMENSIONS)
        and not (REPO_SCOPE_DIMENSIONS & DEFERRED_L2_DIMENSIONS)
        and not (SURFACE_SCOPE_DIMENSIONS & DEFERRED_L2_DIMENSIONS),
    )
    tree = _tree(
        (
            "reference/app.web.tsx",
            "reference/package-lock.json",
            "src/app.tsx",
            "src/generated/client.ts",
        )
    )
    overrides = parse_role_overrides(
        [
            _override("reference", SourceRole.EXAMPLES_REFERENCE),
            _override("src", SourceRole.PRODUCT),
            _override("src/generated", SourceRole.VENDORED),
        ],
        tree,
        foreign=True,
    )
    _check(
        "longest matching prefix wins",
        confirmed_role_for_path("src/generated/client.ts", overrides) is SourceRole.VENDORED,
    )
    trailing = parse_role_overrides(
        [_override("reference/", SourceRole.EXAMPLES_REFERENCE)],
        tree,
        foreign=True,
    )
    _check(
        "one trailing slash normalizes to the public path prefix",
        trailing[0].path_prefix == "reference",
        trailing[0].path_prefix,
    )
    _check("prefix matching is boundary-aware", not prefix_matches("reference/a.ts", "ref"))
    _expect_override_error(
        "prefix boundary mismatch fails as unmatched",
        [_override("ref", SourceRole.DOCS)],
        tree,
    )
    _expect_override_error(
        "duplicate prefixes fail loud",
        [
            _override("src", SourceRole.PRODUCT),
            _override("src", SourceRole.TEST),
        ],
        tree,
    )
    _expect_override_error(
        "override count is bounded",
        [_override(f"src/{index}", SourceRole.PRODUCT) for index in range(MAX_ROLE_OVERRIDES + 1)],
        tree,
    )
    boundary_tree = _tree(
        tuple(f"scope-{index}/file.ts" for index in range(MAX_ROLE_OVERRIDES))
    )
    boundary_overrides = parse_role_overrides(
        [
            _override(f"scope-{index}", SourceRole.PRODUCT)
            for index in range(MAX_ROLE_OVERRIDES)
        ],
        boundary_tree,
        foreign=True,
    )
    _check(
        "exact override-count boundary is accepted",
        len(boundary_overrides) == MAX_ROLE_OVERRIDES,
    )
    _expect_override_error(
        "glob prefixes fail loud",
        [_override("src/*", SourceRole.PRODUCT)],
        tree,
    )
    for label, prefix in (
        ("absolute prefixes fail loud", "/src"),
        ("parent traversal prefixes fail loud", "src/../reference"),
        ("backslash prefixes fail loud", "src\\generated"),
    ):
        _expect_override_error(
            label,
            [_override(prefix, SourceRole.PRODUCT)],
            tree,
        )
    max_prefix = "p" * MAX_PREFIX_LENGTH
    max_prefix_override = parse_role_overrides(
        [_override(max_prefix, SourceRole.PRODUCT)],
        _tree((max_prefix,)),
        foreign=True,
    )
    _check(
        "exact prefix-length boundary is accepted",
        max_prefix_override[0].path_prefix == max_prefix,
    )
    _expect_override_error(
        "overlong prefix fails loud",
        [_override(f"{max_prefix}x", SourceRole.PRODUCT)],
        _tree((f"{max_prefix}x",)),
    )
    max_provenance = _override("src", SourceRole.PRODUCT)
    max_provenance["doctrine_locator"] = "d" * MAX_PROVENANCE_LENGTH
    accepted_provenance = parse_role_overrides(
        [max_provenance],
        tree,
        foreign=True,
    )
    _check(
        "exact provenance-length boundary is accepted",
        accepted_provenance[0].doctrine_locator == "d" * MAX_PROVENANCE_LENGTH,
    )
    overlong_provenance = _override("src", SourceRole.PRODUCT)
    overlong_provenance["declaring_session"] = "s" * (MAX_PROVENANCE_LENGTH + 1)
    _expect_override_error(
        "overlong provenance fails loud",
        [overlong_provenance],
        tree,
    )
    bad_role = _override("src", SourceRole.PRODUCT)
    bad_role["role"] = "runtime_data"
    _expect_override_error("unknown roles fail loud", [bad_role], tree)
    no_session = _override("src", SourceRole.PRODUCT)
    del no_session["declaring_session"]
    _expect_override_error("session provenance is required", [no_session], tree)
    _expect_override_error(
        "self-vet rejects role_overrides key presence even when empty",
        [],
        tree,
        foreign=False,
    )
    _expect_override_error(
        "explicit null is malformed rather than silently treated as absent",
        None,
        tree,
    )


def _partition_fixture() -> tuple[dict[str, object], dict[str, int]]:
    paths = (
        "reference/app.web.tsx",
        "reference/package-lock.json",
        "src/app.tsx",
        "src/generated/client.native.ts",
        *(f"documentation/page-{index}.md" for index in range(20)),
    )
    tree = _tree(paths)
    overrides = parse_role_overrides(
        [
            _override("reference", SourceRole.EXAMPLES_REFERENCE),
            _override("src", SourceRole.PRODUCT),
            _override("src/generated", SourceRole.VENDORED),
        ],
        tree,
        foreign=True,
    )
    run_id = "vr-source-role"
    findings = [
        # Same overridden non-product file: repo risk must stay repo-wide while
        # complexity alone moves into the non-product surface bucket.
        _finding(
            run_id,
            file="reference/app.web.tsx",
            dimension=Dimension.SECRETS,
            constraint="secret-in-reference",
        ),
        _finding(
            run_id,
            file="reference/app.web.tsx",
            dimension=Dimension.COMPLEXITY,
            constraint="complex-reference-function",
        ),
        _finding(
            run_id,
            file="reference/package-lock.json",
            dimension=Dimension.DEPS,
            constraint="vulnerable-reference-dependency",
        ),
        _finding(
            run_id,
            file="src/app.tsx",
            dimension=Dimension.CODE_QUALITY,
            constraint="starter-chrome",
        ),
        _finding(
            run_id,
            file="src/generated/client.native.ts",
            dimension=Dimension.DUP,
            constraint="generated-duplication",
        ),
        _finding(
            run_id,
            file="src/app.tsx",
            dimension=Dimension.ARCHITECTURE,
            constraint="architecture-candidate",
        ),
    ]
    overall = counts_by_dimension(findings)
    partition = build_source_role_partition(
        tree=tree,
        findings=findings,
        overrides=overrides,
        overall_by_dimension=overall,
    )
    return partition, overall


def _check_applied_override_evidence(partition: dict[str, object]) -> None:
    override_evidence = partition["overrides_applied"]
    _check(
        "normalized applied overrides carry bounded match counts and frozen public names",
        override_evidence[0]["path_prefix"] == "reference"
        and override_evidence[0]["match_count"] == 2
        and set(override_evidence[0])
        == {
            "path_prefix",
            "role",
            "doctrine_locator",
            "declaring_session",
            "match_count",
        },
        str(override_evidence),
    )


def _check_partition_counts(
    partition: dict[str, object], overall: dict[str, int]
) -> None:
    _check(
        "partition uses the exact shared overall histogram object",
        partition["overall_by_dimension"] is overall,
    )
    repo = partition["repo_risk"]
    surface = partition["surface_quality"]
    deferred = partition["deferred_l2"]
    reconciliation = partition["reconciliation"]
    assert isinstance(repo, dict)
    assert isinstance(surface, dict)
    assert isinstance(deferred, dict)
    assert isinstance(reconciliation, dict)
    _check(
        "secret and dependency in overridden non-product paths remain repo risk",
        repo["total"] == 2
        and repo["by_dimension"]["secrets"] == 1
        and repo["by_dimension"]["deps"] == 1,
        str(repo),
    )
    _check(
        "only surface findings split product/non-product",
        surface["product"]["total"] == 1
        and surface["non_product"]["total"] == 2
        and surface["unconfirmed"]["total"] == 0,
        str(surface),
    )
    _check("deferred L2 stays unpartitioned", deferred["total"] == 1, str(deferred))
    _check(
        "every emitted finding reconciles exactly",
        reconciliation["reconciles"] is True
        and reconciliation["emitted_findings"]
        == reconciliation["repo_risk"]
        + reconciliation["deferred_l2"]
        + reconciliation["surface_quality"],
        str(reconciliation),
    )


def _check_bounded_partition_evidence(partition: dict[str, object]) -> None:
    surface = partition["surface_quality"]
    candidates = partition["candidate_annotations"]
    assert isinstance(candidates, dict)
    _check(
        "candidate annotations are explicitly non-classifying",
        candidates["annotation_only"] is True,
    )
    docs_evidence = candidates["by_role"]["docs"]
    _check(
        "candidate examples are bounded while counts remain complete",
        len(docs_evidence["examples"]) == MAX_EXAMPLES_PER_BUCKET
        and docs_evidence["count"] == 20
        and docs_evidence["omitted"] == 20 - MAX_EXAMPLES_PER_BUCKET,
        str(candidates),
    )
    product_examples = surface["product"]["evidence"]["examples"]
    _check(
        "surface examples carry separate platform evidence with bare file null",
        product_examples[0]["platform_variant"] is None,
        str(product_examples),
    )
    identity = partition["content_identity"]
    _check(
        "partition explicitly disclaims atomic/blob identity",
        identity["atomic_snapshot"] is False
        and identity["blob_identity_claimed"] is False
        and identity["mode"] == "mutable_worktree",
        str(identity),
    )


def _check_partition_and_evidence() -> dict[str, object]:
    partition, overall = _partition_fixture()
    _check_applied_override_evidence(partition)
    _check_partition_counts(partition, overall)
    _check_bounded_partition_evidence(partition)
    return partition


def _check_platform_and_candidates() -> None:
    _check(
        "web suffix is detected",
        platform_variant_for_path("src/app-tabs.web.tsx") is PlatformVariant.WEB,
    )
    _check(
        "native suffix is detected",
        platform_variant_for_path("src/client.native.ts") is PlatformVariant.NATIVE,
    )
    _check(
        "ios suffix is detected",
        platform_variant_for_path("src/client.ios.ts") is PlatformVariant.IOS,
    )
    _check(
        "android suffix is detected",
        platform_variant_for_path("src/client.android.ts") is PlatformVariant.ANDROID,
    )
    _check(
        "bare file has no platform variant",
        platform_variant_for_path("src/app-tabs.tsx") is None,
    )
    _check(
        "reference convention is an annotation candidate",
        candidate_role_for_path("reference/demo.tsx") is SourceRole.EXAMPLES_REFERENCE,
    )
    tree = _tree(("reference/demo.web.tsx",))
    findings = [
        _finding(
            "vr-annotation-only",
            file="reference/demo.web.tsx",
            dimension=Dimension.COMPLEXITY,
            constraint="candidate-must-not-classify",
        )
    ]
    partition = build_source_role_partition(
        tree=tree,
        findings=findings,
        overrides=(),
        overall_by_dimension=counts_by_dimension(findings),
    )
    surface = partition["surface_quality"]
    _check(
        "candidate reference path does not move a finding without an override",
        surface["unconfirmed"]["total"] == 1
        and surface["non_product"]["total"] == 0,
        str(surface),
    )


def _ok(data: dict[str, Any]) -> dict[str, Any]:
    return {
        "action_status": "completed",
        "data": data,
        "actions": [],
        "error": None,
        "timestamp": "",
    }


class _FakeStateService:
    def __init__(self) -> None:
        self.rows: dict[str, dict[str, Any]] = {}

    def upsert_state(self, namespace: str, payload: dict[str, Any]) -> dict[str, Any]:  # noqa: ARG002
        record = dict(payload["record"])
        self.rows[str(record["run_id"])] = record
        return _ok({})

    def query_ordered(self, namespace: str, payload: dict[str, Any]) -> dict[str, Any]:  # noqa: ARG002
        return _ok(
            {
                "records": sorted(
                    self.rows.values(),
                    key=lambda row: (str(row["started"]), str(row["run_id"])),
                )[: int(payload["limit"])]
            }
        )

    def query_state(self, namespace: str, payload: dict[str, Any]) -> dict[str, Any]:  # noqa: ARG002
        run_id = str(payload["filters"]["run_id"])
        return _ok({"records": [self.rows[run_id]] if run_id in self.rows else []})

    def delete_records(self, namespace: str, payload: dict[str, Any]) -> dict[str, Any]:  # noqa: ARG002
        self.rows.pop(str(payload["filters"]["run_id"]), None)
        return _ok({"deleted": 1})


class _StubPlugin(CodeVettingPlugin):
    def __init__(self, state_service: _FakeStateService, worktree: Path) -> None:
        super().__init__()
        self._stub_state = state_service
        self._worktree_root = worktree

    def _state_service(self) -> Any:  # noqa: ANN401
        return self._stub_state


def _stub_scan(
    tree: TargetTree, run_id: str
) -> tuple[list[Finding], list[CoverageRecord], L1ReportData]:
    return (
        [
            _finding(
                run_id,
                file="reference/app.web.tsx",
                dimension=Dimension.SECRETS,
                constraint="secret-risk",
            ),
            _finding(
                run_id,
                file="reference/app.web.tsx",
                dimension=Dimension.COMPLEXITY,
                constraint="surface-quality",
            ),
        ],
        [CoverageRecord(scanner="stub", ran=True, files_examined=len(tree.all_files()))],
        L1ReportData(),
    )


def _check_plugin_persistence_and_self_compatibility(partition: dict[str, object]) -> None:
    plain_target = RunTarget(repo="example", ref="HEAD", scope="self")
    _check(
        "RunTarget conditionally omits partition for self compatibility",
        plain_target.to_dict() == {"repo": "example", "ref": "HEAD", "scope": "self"},
        str(plain_target.to_dict()),
    )
    plain_report = ReportRenderer().render(
        run_id="vr-self",
        target=plain_target,
        context_profile=ContextProfile.PRODUCTION,
        generated_at="2026-07-27T00:00:00Z",
        findings=[],
        coverage=[],
    )
    _check(
        "self report has no foreign source-role section",
        "Foreign target source-role attribution" not in plain_report,
    )
    foreign_report = ReportRenderer().render(
        run_id="vr-foreign",
        target=RunTarget(
            repo="foreign",
            ref="deadbeef",
            scope="whole tree",
            source_role_partition=partition,
        ),
        context_profile=ContextProfile.PRODUCTION,
        generated_at="2026-07-27T00:00:00Z",
        findings=[],
        coverage=[],
    )
    _check(
        "foreign report adds exact reconciliation and mutable-worktree disclosure",
        "Foreign target source-role attribution" in foreign_report
        and "Reconciliation:" in foreign_report
        and "makes no claim that git blobs identify" in foreign_report,
    )

    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        worktree = base / "worktree"
        worktree.mkdir()
        target = base / "target"
        (target / "reference").mkdir(parents=True)
        (target / "reference" / "app.web.tsx").write_text(
            "export const app = 1;\n", encoding="utf-8"
        )
        state = _FakeStateService()
        plugin = _StubPlugin(state, worktree)
        payload = plugin._run(  # noqa: SLF001
            _stub_scan,
            scope="foreign",
            tag="roles",
            target_path=str(target),
            role_overrides=[
                _override("reference", SourceRole.EXAMPLES_REFERENCE)
            ],
            persist=True,
        )
        target_payload = payload["target"]
        _check(
            "every foreign _run payload carries the nested partition",
            isinstance(target_payload, dict)
            and "source_role_partition" in target_payload,
            str(target_payload),
        )
        nested = target_payload["source_role_partition"]
        _check(
            "public overall histogram and nested overall histogram are exact",
            payload["counts_by_dimension"] == nested["overall_by_dimension"],
        )
        row = read_vetting_run(state, str(payload["run_id"]))
        assert row is not None
        _check(
            "existing target JSON persistence/readback retains the partition",
            row["target"]["source_role_partition"] == nested,
        )

        error = plugin.vet_codebase(
            {"role_overrides": []},
            {},
        )
        _check(
            "invalid self override returns typed invalid_role_overrides",
            error["action_status"] == "error"
            and error["error"]["code"] == "invalid_role_overrides",
            str(error),
        )
        null_error = plugin.vet_codebase(
            {"role_overrides": None},
            {},
        )
        _check(
            "explicit null override returns typed invalid_role_overrides",
            null_error["action_status"] == "error"
            and null_error["error"]["code"] == "invalid_role_overrides",
            str(null_error),
        )


def main() -> int:
    try:
        _check_policy_and_validation()
        partition = _check_partition_and_evidence()
        _check_platform_and_candidates()
        _check_plugin_persistence_and_self_compatibility(partition)
    except SmokeFailureError as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        print(
            f"  ({len(_CHECKS_RUN)} checks attempted before failure)",
            file=sys.stderr,
        )
        return 1
    print(f"source_roles_smoke OK: {len(_CHECKS_RUN)} checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
