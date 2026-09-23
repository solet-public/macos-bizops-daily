"""Foreign-target source-role and platform-variant attribution.

This module is deliberately a report-stage policy, not a scanner filter.  Every
scanner still sees the same target and every finding remains in the run's
``counts_by_dimension``.  A foreign run gains two additive kinds of evidence:

* validated, per-run prefix overrides can confirm which source role a path has;
* path conventions produce bounded *candidate annotations* only.

Only surface-quality dimensions are partitioned by role.  Repository risks
(secrets, dependencies, licences, and similar findings) and the deferred L2
dimensions remain repository-wide, so an override can never suppress a secret
or dependency finding merely because it lives in reference, test, or vendored
material.

The target is a mutable worktree.  Enumeration and scanner reads are not an
atomic snapshot, so this wedge intentionally makes no git-blob identity claim.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import PurePosixPath

from .models import Dimension, Finding
from .targets import TargetTree

MAX_ROLE_OVERRIDES = 32
MAX_EXAMPLES_PER_BUCKET = 8
MAX_PREFIX_LENGTH = 512
MAX_PROVENANCE_LENGTH = 512


class SourceRole(StrEnum):
    """A confirmed role for a foreign target path."""

    PRODUCT = "product"
    TEST = "test"
    DOCS = "docs"
    EXAMPLES_REFERENCE = "examples_reference"
    VENDORED = "vendored"
    BUILD_TOOLING = "build_tooling"


class PlatformVariant(StrEnum):
    """A React-Native-style platform suffix carried separately from source role."""

    WEB = "web"
    IOS = "ios"
    ANDROID = "android"
    NATIVE = "native"


# These three sets are an exhaustive, disjoint policy over Dimension.  Adding a
# Dimension without deciding its partition posture fails at import time.
REPO_SCOPE_DIMENSIONS: frozenset[Dimension] = frozenset(
    {
        Dimension.SECRETS,
        Dimension.DEPS,
        Dimension.LICENSE,
        Dimension.IDENTITY_LEAK,
        Dimension.HIDDEN_UNICODE,
        Dimension.NETWORK_BIND,
        Dimension.SECURITY,
        Dimension.ORPHAN,
    }
)
SURFACE_SCOPE_DIMENSIONS: frozenset[Dimension] = frozenset(
    {
        Dimension.COMPLEXITY,
        Dimension.DUP,
        Dimension.DEAD_CODE,
        Dimension.CODE_QUALITY,
        Dimension.TEST_REACH,
        Dimension.TYPE_COVERAGE,
    }
)
DEFERRED_L2_DIMENSIONS: frozenset[Dimension] = frozenset(
    {
        Dimension.CORRECTNESS,
        Dimension.ARCHITECTURE,
        Dimension.AI_SLOP,
        Dimension.TEST_ADEQUACY,
        Dimension.KB_DOC_FIDELITY,
    }
)

_DIMENSION_POLICY_GROUPS = (
    REPO_SCOPE_DIMENSIONS,
    SURFACE_SCOPE_DIMENSIONS,
    DEFERRED_L2_DIMENSIONS,
)
if any(
    left & right
    for index, left in enumerate(_DIMENSION_POLICY_GROUPS)
    for right in _DIMENSION_POLICY_GROUPS[index + 1 :]
):
    raise RuntimeError("source-role dimension policy groups must be disjoint")
if frozenset().union(*_DIMENSION_POLICY_GROUPS) != frozenset(Dimension):
    raise RuntimeError("every Dimension must have exactly one source-role partition policy")


class RoleOverrideValidationError(ValueError):
    """Caller-supplied role overrides failed the typed fail-loud contract."""


@dataclass(frozen=True, slots=True)
class RoleOverride:
    """One bounded, doctrine-backed, per-run prefix declaration."""

    path_prefix: str
    role: SourceRole
    doctrine_locator: str
    declaring_session: str

    def to_dict(self, *, match_count: int) -> dict[str, object]:
        return {
            "path_prefix": self.path_prefix,
            "role": self.role.value,
            "doctrine_locator": self.doctrine_locator,
            "declaring_session": self.declaring_session,
            "match_count": match_count,
        }


_GLOB_CHARS = frozenset("*?[]{}")
_PLATFORM_SUFFIX = re.compile(r"\.(web|ios|android|native)(?=\.[^.]+$)", re.IGNORECASE)
_TEST_FILE = re.compile(r"(^|[._-])(test|tests|spec)([._-]|$)", re.IGNORECASE)

_CANDIDATE_DIRS: tuple[tuple[SourceRole, frozenset[str]], ...] = (
    (SourceRole.VENDORED, frozenset({"vendor", "vendored", "third_party", "third-party"})),
    (
        SourceRole.EXAMPLES_REFERENCE,
        frozenset({"example", "examples", "reference", "references", "fixture", "fixtures", "sample", "samples"}),
    ),
    (SourceRole.TEST, frozenset({"test", "tests", "__tests__", "spec", "specs"})),
    (SourceRole.DOCS, frozenset({"doc", "docs", "documentation"})),
    (SourceRole.BUILD_TOOLING, frozenset({"script", "scripts", "tool", "tools", "build_tooling"})),
    (SourceRole.PRODUCT, frozenset({"src", "app", "lib", "components"})),
)
_BUILD_FILENAMES = frozenset(
    {
        "babel.config.js",
        "eslint.config.js",
        "metro.config.js",
        "vite.config.js",
        "webpack.config.js",
        "tsconfig.json",
    }
)


def _require_bounded_string(item: Mapping[str, object], key: str, *, limit: int) -> str:
    value = item.get(key)
    if not isinstance(value, str) or not value.strip():
        raise RoleOverrideValidationError(f"role override field {key!r} must be a non-empty string")
    clean = value.strip()
    if len(clean) > limit:
        raise RoleOverrideValidationError(
            f"role override field {key!r} exceeds the {limit}-character limit"
        )
    return clean


def _normalize_trailing_slash(raw: str) -> str:
    # A single directory-style trailing slash is harmless and normalizes to the
    # same public path prefix. Multiple trailing slashes stay invalid.
    if raw.endswith("/") and not raw.endswith("//"):
        return raw[:-1]
    return raw


def _invalid_basic_prefix_shape(raw: str) -> bool:
    return any(
        (
            raw in {"", "."},
            raw.startswith("/"),
            raw.endswith("/"),
            "\\" in raw,
        )
    )


def _invalid_prefix_parts(raw: str) -> bool:
    parts = PurePosixPath(raw).parts
    return (
        not parts
        or any(part in {"", ".", ".."} for part in parts)
        or "/".join(parts) != raw
    )


def _validate_prefix(raw: str) -> str:
    if len(raw) > MAX_PREFIX_LENGTH:
        raise RoleOverrideValidationError(
            f"role override prefix exceeds the {MAX_PREFIX_LENGTH}-character limit"
        )
    raw = _normalize_trailing_slash(raw)
    if _invalid_basic_prefix_shape(raw):
        raise RoleOverrideValidationError(
            f"role override prefix must be a normalized, tree-relative POSIX path: {raw!r}"
        )
    if any(char in raw for char in _GLOB_CHARS):
        raise RoleOverrideValidationError(
            f"role override prefix must be literal and glob-free: {raw!r}"
        )
    if _invalid_prefix_parts(raw):
        raise RoleOverrideValidationError(
            f"role override prefix must be a normalized, tree-relative POSIX path: {raw!r}"
        )
    return raw


def prefix_matches(path: str, prefix: str) -> bool:
    """Boundary-aware prefix membership (``ref`` never matches ``reference``)."""

    return path == prefix or path.startswith(f"{prefix}/")


def _parse_source_role(raw: str) -> SourceRole:
    try:
        return SourceRole(raw)
    except ValueError as exc:
        choices = ", ".join(role.value for role in SourceRole)
        raise RoleOverrideValidationError(
            f"invalid source role {raw!r}; expected one of: {choices}"
        ) from exc


def _parse_override_item(
    candidate: object,
    *,
    index: int,
    tracked: Sequence[str],
    seen: set[str],
) -> RoleOverride:
    if not isinstance(candidate, Mapping):
        raise RoleOverrideValidationError(f"role_overrides[{index}] must be an object")
    allowed_keys = frozenset(
        {"path_prefix", "role", "doctrine_locator", "declaring_session"}
    )
    extras = set(candidate) - allowed_keys
    if extras:
        raise RoleOverrideValidationError(
            f"role_overrides[{index}] has unsupported fields: "
            f"{', '.join(sorted(str(key) for key in extras))}"
        )
    path_prefix = _validate_prefix(
        _require_bounded_string(candidate, "path_prefix", limit=MAX_PREFIX_LENGTH)
    )
    if path_prefix in seen:
        raise RoleOverrideValidationError(
            f"duplicate role override prefix: {path_prefix!r}"
        )
    if not any(prefix_matches(path, path_prefix) for path in tracked):
        raise RoleOverrideValidationError(
            f"role override prefix matches no enumerated target files: {path_prefix!r}"
        )
    seen.add(path_prefix)
    return RoleOverride(
        path_prefix=path_prefix,
        role=_parse_source_role(
            _require_bounded_string(candidate, "role", limit=64)
        ),
        doctrine_locator=_require_bounded_string(
            candidate, "doctrine_locator", limit=MAX_PROVENANCE_LENGTH
        ),
        declaring_session=_require_bounded_string(
            candidate, "declaring_session", limit=MAX_PROVENANCE_LENGTH
        ),
    )


def parse_role_overrides(
    raw: object,
    tree: TargetTree,
    *,
    foreign: bool,
    provided: bool = True,
) -> tuple[RoleOverride, ...]:
    """Validate the bounded foreign-only override list against the enumerated tree."""

    if not provided:
        return ()
    if not foreign:
        raise RoleOverrideValidationError(
            "role_overrides are foreign-target-only; self-vet source roles are platform-owned"
        )
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes, bytearray)):
        raise RoleOverrideValidationError("role_overrides must be a list of objects")
    if len(raw) > MAX_ROLE_OVERRIDES:
        raise RoleOverrideValidationError(
            f"role_overrides accepts at most {MAX_ROLE_OVERRIDES} entries"
        )
    tracked = tree.all_files()
    seen: set[str] = set()
    return tuple(
        _parse_override_item(
            candidate,
            index=index,
            tracked=tracked,
            seen=seen,
        )
        for index, candidate in enumerate(raw)
    )


def confirmed_role_for_path(path: str, overrides: Sequence[RoleOverride]) -> SourceRole | None:
    """Return the longest boundary-aware confirmed override, else ``None``."""

    matches = [
        override
        for override in overrides
        if prefix_matches(path, override.path_prefix)
    ]
    if not matches:
        return None
    return max(
        matches,
        key=lambda override: (
            len(PurePosixPath(override.path_prefix).parts),
            len(override.path_prefix),
        ),
    ).role


def candidate_role_for_path(path: str) -> SourceRole | None:
    """Return one convention-derived annotation; never a confirmed classification."""

    pure = PurePosixPath(path.lower())
    parts = frozenset(pure.parts[:-1])
    for role, names in _CANDIDATE_DIRS:
        if parts & names:
            return role
    if _TEST_FILE.search(pure.name):
        return SourceRole.TEST
    if pure.name in _BUILD_FILENAMES:
        return SourceRole.BUILD_TOOLING
    if pure.suffix in {".md", ".mdx", ".rst"}:
        return SourceRole.DOCS
    return None


def platform_variant_for_path(path: str) -> PlatformVariant | None:
    """Extract a platform suffix; an ordinary/bare file returns ``None``."""

    match = _PLATFORM_SUFFIX.search(PurePosixPath(path).name)
    return PlatformVariant(match.group(1).lower()) if match else None


def _stable_dimension_counts(
    findings: Sequence[Finding], dimensions: frozenset[Dimension]
) -> dict[str, int]:
    counts = {dimension.value: 0 for dimension in sorted(dimensions, key=lambda item: item.value)}
    for finding in findings:
        if finding.dimension in dimensions:
            counts[finding.dimension.value] += 1
    return counts


def _surface_bucket() -> dict[str, object]:
    return {
        "total": 0,
        "by_dimension": {
            dimension.value: 0
            for dimension in sorted(SURFACE_SCOPE_DIMENSIONS, key=lambda item: item.value)
        },
        "_examples": [],
    }


def _append_example(bucket: dict[str, object], finding: Finding, role: SourceRole | None) -> None:
    examples = bucket.get("_examples")
    if not isinstance(examples, list):
        raise RuntimeError("surface bucket examples must be a list")
    variant = platform_variant_for_path(finding.file)
    examples.append(
        {
            "file": finding.file,
            "dimension": finding.dimension.value,
            "role": None if role is None else role.value,
            "platform_variant": None if variant is None else variant.value,
        }
    )


def _bounded_strings(paths: Sequence[str]) -> dict[str, object]:
    ordered = sorted(paths)
    shown = ordered[:MAX_EXAMPLES_PER_BUCKET]
    return {
        "count": len(ordered),
        "examples": shown,
        "omitted": len(ordered) - len(shown),
    }


def _finalize_surface_bucket(bucket: dict[str, object]) -> None:
    raw_examples = bucket.pop("_examples", None)
    if not isinstance(raw_examples, list):
        raise RuntimeError("surface bucket examples must be a list")
    ordered = sorted(
        raw_examples,
        key=lambda example: (
            str(example["file"]),
            str(example["dimension"]),
            str(example["role"]),
            str(example["platform_variant"]),
        ),
    )
    shown = ordered[:MAX_EXAMPLES_PER_BUCKET]
    bucket["evidence"] = {
        "count": len(ordered),
        "examples": shown,
        "omitted": len(ordered) - len(shown),
    }


def _candidate_annotations(tree: TargetTree) -> dict[str, object]:
    paths_by_role: dict[str, list[str]] = {role.value: [] for role in SourceRole}
    for path in sorted(tree.all_files()):
        role = candidate_role_for_path(path)
        if role is None:
            continue
        paths_by_role[role.value].append(path)
    return {
        "annotation_only": True,
        "by_role": {
            role.value: _bounded_strings(paths_by_role[role.value])
            for role in SourceRole
        },
        "note": (
            "Path conventions are discovery hints only. They do not classify files, "
            "move findings, suppress risk, or change any headline count."
        ),
    }


def _platform_variant_evidence(tree: TargetTree) -> dict[str, object]:
    paths_by_variant: dict[str, list[str]] = {
        variant.value: [] for variant in PlatformVariant
    }
    bare_files = 0
    for path in sorted(tree.all_files()):
        variant = platform_variant_for_path(path)
        if variant is None:
            bare_files += 1
            continue
        paths_by_variant[variant.value].append(path)
    return {
        "by_variant": {
            variant.value: _bounded_strings(paths_by_variant[variant.value])
            for variant in PlatformVariant
        },
        "bare_file_count": bare_files,
        "note": "Platform suffix is independent evidence; a bare file has platform_variant=null.",
    }


def _count_field(source: Mapping[str, object], key: str) -> int:
    value = source.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise RuntimeError(f"source-role count field {key!r} must be a non-negative int")
    return value


def _validate_overall_by_dimension(overall_by_dimension: Mapping[str, int]) -> None:
    expected_keys = {dimension.value for dimension in Dimension}
    if set(overall_by_dimension) != expected_keys:
        raise ValueError(
            "overall_by_dimension must contain exactly every Dimension key"
        )
    if any(value < 0 for value in overall_by_dimension.values()):
        raise ValueError(
            "overall_by_dimension values must be non-negative integers"
        )


def _surface_bucket_name(role: SourceRole | None) -> str:
    if role is None:
        return "unconfirmed"
    if role is SourceRole.PRODUCT:
        return "product"
    return "non_product"


def _partition_surface_findings(
    findings: Sequence[Finding],
    overrides: Sequence[RoleOverride],
) -> dict[str, dict[str, object]]:
    surface = {
        "product": _surface_bucket(),
        "non_product": _surface_bucket(),
        "unconfirmed": _surface_bucket(),
    }
    for finding in findings:
        if finding.dimension not in SURFACE_SCOPE_DIMENSIONS:
            continue
        role = confirmed_role_for_path(finding.file, overrides)
        bucket = surface[_surface_bucket_name(role)]
        bucket["total"] = _count_field(bucket, "total") + 1
        by_dimension = bucket.get("by_dimension")
        if not isinstance(by_dimension, dict):
            raise RuntimeError("surface bucket by_dimension must be a dict")
        by_dimension[finding.dimension.value] = (
            _count_field(by_dimension, finding.dimension.value) + 1
        )
        _append_example(bucket, finding, role)
    return surface


def _dimension_subset_from_overall(
    overall_by_dimension: Mapping[str, int],
    dimensions: frozenset[Dimension],
) -> dict[str, int]:
    return {
        dimension.value: overall_by_dimension[dimension.value]
        for dimension in sorted(dimensions, key=lambda item: item.value)
    }


def _observed_surface_counts(
    surface: Mapping[str, Mapping[str, object]],
) -> dict[str, int]:
    observed: dict[str, int] = {}
    for dimension in sorted(SURFACE_SCOPE_DIMENSIONS, key=lambda item: item.value):
        observed[dimension.value] = sum(
            _surface_dimension_count(bucket, dimension)
            for bucket in surface.values()
        )
    return observed


def _surface_dimension_count(
    bucket: Mapping[str, object], dimension: Dimension
) -> int:
    by_dimension = bucket.get("by_dimension")
    if not isinstance(by_dimension, Mapping):
        raise RuntimeError("surface bucket by_dimension must be an object")
    return _count_field(by_dimension, dimension.value)


def _validate_loop_counts(
    *,
    findings: Sequence[Finding],
    surface: Mapping[str, Mapping[str, object]],
    repo_counts: Mapping[str, int],
    deferred_counts: Mapping[str, int],
    expected_surface_counts: Mapping[str, int],
) -> None:
    if _stable_dimension_counts(findings, REPO_SCOPE_DIMENSIONS) != repo_counts:
        raise ValueError(
            "repo-scope finding loop does not match shared overall_by_dimension"
        )
    if _stable_dimension_counts(findings, DEFERRED_L2_DIMENSIONS) != deferred_counts:
        raise ValueError(
            "deferred-L2 finding loop does not match shared overall_by_dimension"
        )
    if _observed_surface_counts(surface) != expected_surface_counts:
        raise ValueError(
            "surface-scope finding loop does not match shared overall_by_dimension"
        )


def _finalize_surface(surface: Mapping[str, dict[str, object]]) -> None:
    for bucket in surface.values():
        _finalize_surface_bucket(bucket)


def _applied_override_evidence(
    tree: TargetTree, overrides: Sequence[RoleOverride]
) -> list[dict[str, object]]:
    return [
        override.to_dict(
            match_count=sum(
                1
                for path in tree.all_files()
                if prefix_matches(path, override.path_prefix)
            )
        )
        for override in overrides
    ]


def _reconciliation(
    *,
    findings: Sequence[Finding],
    overall_by_dimension: Mapping[str, int],
    repo_counts: Mapping[str, int],
    deferred_counts: Mapping[str, int],
    surface: Mapping[str, Mapping[str, object]],
) -> tuple[int, int, int, int]:
    repo_total = sum(repo_counts.values())
    deferred_total = sum(deferred_counts.values())
    surface_total = sum(
        _count_field(bucket, "total") for bucket in surface.values()
    )
    emitted = sum(overall_by_dimension.values())
    if emitted != len(findings):
        raise ValueError(
            "shared overall_by_dimension total does not match the explicit finding loop"
        )
    if emitted != repo_total + deferred_total + surface_total:
        raise RuntimeError(
            "source-role dimension groups failed exact reconciliation"
        )
    return emitted, repo_total, deferred_total, surface_total


def build_source_role_partition(
    *,
    tree: TargetTree,
    findings: Sequence[Finding],
    overrides: Sequence[RoleOverride],
    overall_by_dimension: dict[str, int],
) -> dict[str, object]:
    """Build the additive, exactly reconciled partition for one foreign run."""

    _validate_overall_by_dimension(overall_by_dimension)
    surface = _partition_surface_findings(findings, overrides)
    repo_counts = _dimension_subset_from_overall(
        overall_by_dimension, REPO_SCOPE_DIMENSIONS
    )
    deferred_counts = _dimension_subset_from_overall(
        overall_by_dimension, DEFERRED_L2_DIMENSIONS
    )
    expected_surface_counts = _dimension_subset_from_overall(
        overall_by_dimension, SURFACE_SCOPE_DIMENSIONS
    )
    _validate_loop_counts(
        findings=findings,
        surface=surface,
        repo_counts=repo_counts,
        deferred_counts=deferred_counts,
        expected_surface_counts=expected_surface_counts,
    )
    emitted, repo_total, deferred_total, surface_total = _reconciliation(
        findings=findings,
        overall_by_dimension=overall_by_dimension,
        repo_counts=repo_counts,
        deferred_counts=deferred_counts,
        surface=surface,
    )
    _finalize_surface(surface)

    return {
        "policy_version": "source-role-v1",
        # Keep the exact dict object used by the public counts_by_dimension field;
        # callers can therefore neither drift nor independently recompute it.
        "overall_by_dimension": overall_by_dimension,
        "repo_risk": {"total": repo_total, "by_dimension": repo_counts},
        "deferred_l2": {"total": deferred_total, "by_dimension": deferred_counts},
        "surface_quality": surface,
        "reconciliation": {
            "emitted_findings": emitted,
            "repo_risk": repo_total,
            "deferred_l2": deferred_total,
            "surface_quality": surface_total,
            "reconciles": emitted == repo_total + deferred_total + surface_total,
        },
        "overrides_applied": _applied_override_evidence(tree, overrides),
        "candidate_annotations": _candidate_annotations(tree),
        "platform_variants": _platform_variant_evidence(tree),
        "content_identity": {
            "mode": "mutable_worktree",
            "atomic_snapshot": False,
            "blob_identity_claimed": False,
            "disclosure": (
                "Enumeration and scanner reads are not atomic. The run records the target HEAD "
                "when available but makes no claim that git blobs identify the mutable bytes read."
            ),
        },
    }


def render_source_role_section(source_roles: Mapping[str, object]) -> str:
    """Render the bounded foreign-target attribution and reconciliation section."""

    repo = _mapping_field(source_roles, "repo_risk")
    deferred = _mapping_field(source_roles, "deferred_l2")
    surface = _mapping_field(source_roles, "surface_quality")
    reconciliation = _mapping_field(source_roles, "reconciliation")
    identity = _mapping_field(source_roles, "content_identity")
    overall = _mapping_field(source_roles, "overall_by_dimension")
    candidates = _mapping_field(source_roles, "candidate_annotations")
    candidate_by_role = _mapping_field(candidates, "by_role")
    platforms = _mapping_field(source_roles, "platform_variants")
    platform_by_variant = _mapping_field(platforms, "by_variant")
    overrides = source_roles.get("overrides_applied")
    if not isinstance(overrides, list):
        raise TypeError("source_role_partition.overrides_applied must be a list")

    lines = [
        "## Foreign target source-role attribution",
        "",
        (
            "**Repository risk is never partitioned or suppressed by source role.** "
            f"Repo-wide findings: **{repo['total']}**; deferred L2 findings: **{deferred['total']}**."
        ),
        (
            f"Overall by dimension: {_dimension_counts_text(overall)}. "
            f"Repo risk: {_dimension_counts_text(_mapping_field(repo, 'by_dimension'))}. "
            f"Deferred L2: {_dimension_counts_text(_mapping_field(deferred, 'by_dimension'))}."
        ),
        "",
        "| surface-quality bucket | findings | dimensions | bounded examples |",
        "| --- | ---: | --- | --- |",
    ]
    for name in ("product", "non_product", "unconfirmed"):
        bucket = _mapping_field(surface, name)
        evidence = _mapping_field(bucket, "evidence")
        lines.append(
            f"| {name} | {bucket['total']} | "
            f"{_markdown_cell(_dimension_counts_text(_mapping_field(bucket, 'by_dimension')))} | "
            f"{_markdown_cell(_examples_text(evidence))} |"
        )
    lines.extend(
        (
            "",
            (
                f"Reconciliation: **{reconciliation['emitted_findings']} emitted** = "
                f"{reconciliation['repo_risk']} repo risk + "
                f"{reconciliation['deferred_l2']} deferred L2 + "
                f"{reconciliation['surface_quality']} surface quality."
            ),
            "",
            "### Confirmed role overrides",
            "",
        )
    )
    if overrides:
        lines.extend(
            (
                "| path prefix | role | matched files | doctrine | declaring session |",
                "| --- | --- | ---: | --- | --- |",
            )
        )
        for item in overrides:
            if not isinstance(item, Mapping):
                raise TypeError("source-role override evidence must be an object")
            lines.append(
                f"| {_markdown_cell(item['path_prefix'])} | {_markdown_cell(item['role'])} | "
                f"{item['match_count']} | {_markdown_cell(item['doctrine_locator'])} | "
                f"{_markdown_cell(item['declaring_session'])} |"
            )
    else:
        lines.append("_None supplied; all surface findings are unconfirmed by role._")

    lines.extend(
        (
            "",
            "### Candidate path-role annotations (hints only)",
            "",
            "| candidate role | files | examples |",
            "| --- | ---: | --- |",
        )
    )
    for role in SourceRole:
        evidence = _mapping_field(candidate_by_role, role.value)
        lines.append(
            f"| {role.value} | {evidence['count']} | "
            f"{_markdown_cell(_examples_text(evidence))} |"
        )

    lines.extend(
        (
            "",
            "### Platform-variant evidence (separate from role)",
            "",
            "| variant | files | examples |",
            "| --- | ---: | --- |",
        )
    )
    for variant in PlatformVariant:
        evidence = _mapping_field(platform_by_variant, variant.value)
        lines.append(
            f"| {variant.value} | {evidence['count']} | "
            f"{_markdown_cell(_examples_text(evidence))} |"
        )
    lines.extend(
        (
            f"| bare (`platform_variant=null`) | {platforms['bare_file_count']} | — |",
            "",
            (
                "Candidate annotations do not classify files or move findings. "
                "Platform suffix evidence is independent of source role."
            ),
            "",
            f"> **Content identity:** {identity['disclosure']}",
        )
    )
    return "\n".join(lines)


def _mapping_field(source: Mapping[str, object], key: str) -> Mapping[str, object]:
    value = source.get(key)
    if not isinstance(value, Mapping):
        raise TypeError(f"source_role_partition.{key} must be an object")
    return value


def _dimension_counts_text(counts: Mapping[str, object]) -> str:
    nonzero = [
        f"{key}={value}"
        for key, value in sorted(counts.items())
        if isinstance(value, int) and value
    ]
    return ", ".join(nonzero) if nonzero else "none"


def _examples_text(evidence: Mapping[str, object]) -> str:
    examples = evidence.get("examples")
    omitted = evidence.get("omitted")
    if not isinstance(examples, list) or not isinstance(omitted, int):
        raise TypeError("bounded evidence must carry examples[] and omitted")
    shown = ", ".join(str(example) for example in examples) if examples else "—"
    return f"{shown} (+{omitted} omitted)" if omitted else shown


def _markdown_cell(value: object) -> str:
    return str(value).replace("|", "\\|").replace("\r", " ").replace("\n", " ")
