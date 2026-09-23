"""Bounded markdown rendering for a technology fingerprint."""

from __future__ import annotations

import html
from collections.abc import Mapping
from dataclasses import dataclass

from ._technology_fingerprint_probes import MAX_BOUNDED_EXAMPLES

MAX_REPORT_COMPONENTS = 24
MAX_REPORT_CAPABILITIES = 24
MAX_REPORT_ROUTE_INVENTORIES = 24


def _markdown_text(value: object) -> str:
    text = str(value).replace("\r", "\\r").replace("\n", "\\n")
    escaped = html.escape(text, quote=True).replace("|", "&#124;").replace("`", "&#96;")
    for char in ("\\", "*", "_", "[", "]", "!"):
        escaped = escaped.replace(char, f"\\{char}")
    return escaped


def _version_text(value: object) -> str:
    if not isinstance(value, Mapping):
        raise TypeError("component version must be an object")
    version = value.get("value")
    if version is None:
        reason = value.get("reason")
        return "unknown" if reason is None else f"unknown ({_markdown_text(reason)})"
    return _markdown_text(version)


def _role_text(value: object) -> str:
    if not isinstance(value, Mapping):
        raise TypeError("source_role must be an object")
    if value.get("status") != "confirmed":
        return "unconfirmed"
    return f"confirmed:{_markdown_text(value.get('role'))}"


def _bounded_evidence_text(value: object) -> str:
    if not isinstance(value, list):
        raise TypeError("trigger_evidence must be a list")
    shown = value[:MAX_BOUNDED_EXAMPLES]
    rendered: list[str] = []
    for item in shown:
        if not isinstance(item, Mapping):
            raise TypeError("trigger evidence must be an object")
        rendered.append(f"{_markdown_text(item.get('path'))}#{_markdown_text(item.get('pointer'))}")
    omitted = len(value) - len(shown)
    if omitted:
        rendered.append(f"… +{omitted} omitted")
    return ", ".join(rendered) if rendered else "none"


@dataclass(frozen=True, slots=True)
class _ReportFields:
    probes: list[object]
    components: list[object]
    capabilities: list[object]
    deno: list[object]
    supabase: list[object]
    routes: list[object]
    unmodeled: list[object]
    reconciliation: Mapping[str, object]
    source_role_policy: Mapping[str, object]


def _list_field(fingerprint: Mapping[str, object], key: str) -> list[object]:
    value = fingerprint.get(key)
    if not isinstance(value, list):
        raise TypeError(f"technology_fingerprint.{key} must be a list")
    return value


def _mapping_field(source: Mapping[str, object], key: str) -> Mapping[str, object]:
    value = source.get(key)
    if not isinstance(value, Mapping):
        raise TypeError(f"technology_fingerprint field {key!r} must be an object")
    return value


def _report_fields(fingerprint: Mapping[str, object]) -> _ReportFields:
    reconciliation = _mapping_field(fingerprint, "reconciliation")
    source_role_policy = _mapping_field(fingerprint, "source_role_policy")
    _mapping_field(fingerprint, "execution_disclosure")
    return _ReportFields(
        probes=_list_field(fingerprint, "probes"),
        components=_list_field(fingerprint, "components"),
        capabilities=_list_field(fingerprint, "capability_needs"),
        deno=_list_field(fingerprint, "deno_runtime_scopes"),
        supabase=_list_field(fingerprint, "supabase_topology"),
        routes=_list_field(fingerprint, "route_inventories"),
        unmodeled=_list_field(fingerprint, "recognized_but_unmodeled_configs"),
        reconciliation=reconciliation,
        source_role_policy=source_role_policy,
    )


def _intro_lines(
    reconciliation: Mapping[str, object],
    source_role_policy: Mapping[str, object],
) -> list[str]:
    probe_counts = _mapping_field(reconciliation, "probe_roster")
    candidate_counts = _mapping_field(reconciliation, "probe_candidates")
    by_status = _mapping_field(probe_counts, "by_status")
    bootstrap_routing = source_role_policy.get(
        "unconfirmed_facts_capability_routing_eligible_on_first_touch"
    )
    product_claims = source_role_policy.get("unconfirmed_facts_support_product_claims")
    findings = source_role_policy.get("unconfirmed_facts_support_findings")
    if bootstrap_routing is not True or product_claims is not False or findings is not False:
        raise ValueError("technology fingerprint source-role bootstrap policy is inconsistent")
    return [
        "## Technology fingerprint and capability routing",
        "",
        (
            "**Evidence boundary:** mutable worktree; no atomic snapshot. This fingerprint "
            "did not execute target code or JavaScript config, use the network, enter a "
            "sandbox, install dependencies, start a subprocess, follow symlinks, or perform "
            "a second crawl. Routing is not execution."
        ),
        (
            "**Bootstrap source-role policy:** on first-touch scans, unconfirmed facts "
            "are routing-eligible for capability needs, but they never support product "
            "claims or findings."
        ),
        (
            f"Probe reconciliation: **{_markdown_text(probe_counts.get('total'))}** fixed probes "
            f"= matched {_markdown_text(by_status.get('matched'))} + not_present "
            f"{_markdown_text(by_status.get('not_present'))} + unreadable "
            f"{_markdown_text(by_status.get('unreadable'))}; candidate paths "
            f"{_markdown_text(candidate_counts.get('total'))} = matched "
            f"{_markdown_text(candidate_counts.get('matched'))} + unreadable "
            f"{_markdown_text(candidate_counts.get('unreadable'))}."
        ),
    ]


def _probe_row(probe: object) -> str:
    if not isinstance(probe, Mapping):
        raise TypeError("technology probe must be an object")
    return (
        f"| {_markdown_text(probe.get('probe'))} | {_markdown_text(probe.get('status'))} | "
        f"{_markdown_text(probe.get('candidate_count'))} | "
        f"{_markdown_text(probe.get('matched_count'))} | "
        f"{_markdown_text(probe.get('unreadable_count'))} |"
    )


def _probe_lines(probes: list[object]) -> list[str]:
    return [
        "",
        "### Fixed probe roster",
        "",
        "| probe | status | candidates | matched | unreadable |",
        "| --- | --- | ---: | ---: | ---: |",
        *(_probe_row(probe) for probe in probes),
    ]


def _component_row(component: object) -> str:
    if not isinstance(component, Mapping):
        raise TypeError("technology component must be an object")
    return (
        f"| {_markdown_text(component.get('scope'))} | "
        f"{_markdown_text(component.get('component_key'))} | "
        f"{_markdown_text(component.get('relationship'))} / "
        f"{_markdown_text(component.get('status'))} | "
        f"{_version_text(component.get('declared_version'))} | "
        f"{_version_text(component.get('resolved_version'))} | "
        f"{_role_text(component.get('source_role'))} | "
        f"{_markdown_text(component.get('source_usage_status'))} |"
    )


def _component_lines(components: list[object]) -> list[str]:
    lines = ["", "### Scoped component facts", ""]
    if not components:
        return [
            *lines,
            "_No modeled component declarations or lock-only resolutions matched._",
        ]
    shown = components[:MAX_REPORT_COMPONENTS]
    lines.extend(
        (
            "| scope | component | relationship/status | declared | resolved | source role | source usage |",
            "| --- | --- | --- | --- | --- | --- | --- |",
            *(_component_row(component) for component in shown),
        )
    )
    omitted = len(components) - len(shown)
    if omitted:
        lines.append(
            f"\n_{omitted} additional component facts omitted from markdown; "
            "all remain in target.technology_fingerprint.components._"
        )
    return lines


def _route_count(grouped: Mapping[str, object], bucket: str) -> object:
    value = grouped.get(bucket)
    if not isinstance(value, Mapping):
        raise TypeError("route bucket must be an object")
    return value.get("count")


def _route_row(inventory: object) -> str:
    if not isinstance(inventory, Mapping):
        raise TypeError("route inventory must be an object")
    grouped = _mapping_field(inventory, "routes")
    counts = [_route_count(grouped, bucket) for bucket in ("product", "non_product", "unconfirmed")]
    return (
        f"| {_markdown_text(inventory.get('scope'))} | "
        f"{_markdown_text(counts[0])} | {_markdown_text(counts[1])} | "
        f"{_markdown_text(counts[2])} | no |"
    )


def _unmodeled_line(unmodeled: list[object]) -> str | None:
    if not unmodeled:
        return None
    shown = [
        _markdown_text(item.get("path"))
        for item in unmodeled[:MAX_BOUNDED_EXAMPLES]
        if isinstance(item, Mapping)
    ]
    omitted = len(unmodeled) - len(shown)
    suffix = "" if not omitted else f"; +{omitted} omitted"
    return (
        "\nRecognized but unmodeled executable configs (presence only, never executed): "
        f"{', '.join(shown)}{suffix}."
    )


def _runtime_lines(fields: _ReportFields) -> list[str]:
    lines = [
        "",
        "### Runtime and topology facts",
        "",
        (
            f"- Deno runtime scopes: **{len(fields.deno)}**; Supabase topology scopes: "
            f"**{len(fields.supabase)}**; Expo Router scoped route inventories: "
            f"**{len(fields.routes)}**."
        ),
    ]
    if fields.routes:
        shown = fields.routes[:MAX_REPORT_ROUTE_INVENTORIES]
        lines.extend(
            (
                "",
                "| route scope | product | non-product | unconfirmed | content read |",
                "| --- | ---: | ---: | ---: | --- |",
                *(_route_row(inventory) for inventory in shown),
            )
        )
        omitted = len(fields.routes) - len(shown)
        if omitted:
            lines.append(
                f"\n_{omitted} additional route inventories omitted from markdown; "
                "all remain in target.technology_fingerprint.route_inventories._"
            )
    unmodeled = _unmodeled_line(fields.unmodeled)
    if unmodeled is not None:
        lines.append(unmodeled)
    return lines


def _capability_row(capability: object) -> str:
    if not isinstance(capability, Mapping):
        raise TypeError("capability need must be an object")
    return (
        f"| {_markdown_text(capability.get('scope'))} | "
        f"{_markdown_text(capability.get('capability_key'))} | "
        f"{_markdown_text(capability.get('status'))} | "
        f"{_markdown_text(capability.get('execution_status'))} | "
        f"{_bounded_evidence_text(capability.get('trigger_evidence'))} |"
    )


def _capability_lines(capabilities: list[object]) -> list[str]:
    lines = ["", "### Deterministic capability needs", ""]
    if not capabilities:
        lines.append(
            "_No capability need was selected from the confirmed/unconfirmed eligible facts._"
        )
        return lines
    shown = capabilities[:MAX_REPORT_CAPABILITIES]
    lines.extend(
        (
            "| scope | capability_key | status | execution_status | exact trigger evidence |",
            "| --- | --- | --- | --- | --- |",
            *(_capability_row(capability) for capability in shown),
        )
    )
    omitted = len(capabilities) - len(shown)
    if omitted:
        lines.append(
            f"\n_{omitted} additional capability needs omitted from markdown; "
            "all remain in target.technology_fingerprint.capability_needs._"
        )
    return lines


def render_technology_fingerprint_section(
    fingerprint: Mapping[str, object],
) -> str:
    """Render the bounded fingerprint/capability block directly after the header."""

    fields = _report_fields(fingerprint)
    lines = [
        *_intro_lines(fields.reconciliation, fields.source_role_policy),
        *_probe_lines(fields.probes),
        *_component_lines(fields.components),
        *_runtime_lines(fields),
        *_capability_lines(fields.capabilities),
        "",
        (
            "Every capability row has `execution_status=not_run_by_fingerprint`; "
            "only the scanner coverage ledger can say whether an existing opt-in "
            "toolchain adapter ran. No row means clean or pass."
        ),
    ]
    return "\n".join(lines)
