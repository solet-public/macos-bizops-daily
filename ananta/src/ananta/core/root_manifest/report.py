"""Structured report formatting shared across consumers.

The three consumers (pre-commit, cutover, diagnostic) print the same
shape with only the ``severity`` line differing — see design memo §6.3.
"""

from __future__ import annotations

from .types import Classification, SunsetOverdue


def format_report(classification: Classification, *, severity: str) -> str:
    """Render a Classification into the §6.3 multi-section report."""
    lines: list[str] = [f"ROOT MANIFEST CHECK — {severity}"]
    _append_schema_section(lines, classification.schema_validation_error)
    _append_named_section(lines, "Unknown entries (not declared in manifest):",
                          classification.unknown_entries)
    _append_named_section(lines, "  of which untracked by git (no index entry; the "
                          "W-INT landing gate reports these without blocking):",
                          classification.untracked_entries)
    _append_named_section(lines, "Missing universal entries (declared but not present in tree):",
                          classification.missing_universal)
    _append_named_section(lines, "Sanctioned entries declared but absent (INFO):",
                          classification.missing_sanctioned)
    _append_sunset_section(lines, classification.sunset_overdue)
    _append_named_section(lines, "Cleanup overdue (sanctioned/overrides entry past sunset; path absent):",
                          classification.cleanup_overdue)
    _append_footer(lines, classification)
    return "\n".join(lines)


def _append_schema_section(lines: list[str], error: str | None) -> None:
    if error is None:
        return
    lines.append("")
    lines.append("Schema validation:")
    lines.append(f"  - {error}")


def _append_named_section(lines: list[str], heading: str, items: tuple[str, ...]) -> None:
    if not items:
        return
    lines.append("")
    lines.append(heading)
    for name in items:
        lines.append(f"  - {name}")


def _append_sunset_section(lines: list[str], entries: tuple[SunsetOverdue, ...]) -> None:
    if not entries:
        return
    lines.append("")
    lines.append("Sunset overdue (path still present):")
    for entry in entries:
        lines.append(
            f"  - {entry.path} (sunset_target: {entry.sunset_target}; "
            f"days overdue: {entry.days_overdue})"
        )


def _append_footer(lines: list[str], classification: Classification) -> None:
    lines.append("")
    lines.append(f"Manifest: {classification.manifest_path} @ schema_version 1")
    name = classification.solet_name or "<unknown>"
    lines.append(f"Solet: {name}")
