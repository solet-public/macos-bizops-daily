"""Human and JSON renderers over one typed result."""

from __future__ import annotations

import json
from typing import cast

from .models import CommandResult, JsonValue

DOCTOR_KIND = "existing_install_doctor"


def render_json(result: CommandResult) -> str:
    return json.dumps(result.to_dict(), ensure_ascii=False, indent=2, sort_keys=True)


def render_human(result: CommandResult) -> str:
    lines = [result.message, f"Status: {result.status}"]
    if result.error_kind is not None:
        lines.append(f"Error: {result.error_kind}")
    if result.repair is not None:
        lines.append(f"Repair: {result.repair}")
    if result.kind == DOCTOR_KIND and isinstance(result.data.get("sections"), list):
        lines.extend(_doctor_lines(result.data))
        return "\n".join(lines)
    if result.data:
        lines.append("Details:")
        lines.append(json.dumps(result.data, ensure_ascii=False, indent=2, sort_keys=True))
    if result.evidence:
        lines.append("Evidence:")
        lines.append(
            json.dumps(
                [item.to_dict() for item in result.evidence],
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
        )
    return "\n".join(lines)


def _doctor_lines(data: dict[str, JsonValue]) -> list[str]:
    """One line per check, sections in the closed order of Step 6 design section 3.3; JSON parity is a smoke."""
    contract = cast(dict[str, JsonValue], data.get("contract", {}))
    lines = [f"Contract: {contract.get('kind')}", f"HEAD: {data.get('head_observed')}"]
    for raw in cast(list[JsonValue], data["sections"]):
        section = cast(dict[str, JsonValue], raw)
        lines.append(f"[{section['section']}]")
        for item in cast(list[JsonValue], section["checks"]):
            check = cast(dict[str, JsonValue], item)
            suffix = "" if check.get("reason_code") is None else f" ({check['reason_code']}; repair: {check.get('repair_code')})"
            lines.append(f"  {check['status']:<15} {check['check_id']}{suffix}")
    counts = cast(dict[str, JsonValue], data.get("counts", {}))
    lines.append(f"Counts: required={json.dumps(counts.get('required'), sort_keys=True)} advisory={json.dumps(counts.get('advisory'), sort_keys=True)}")
    lines.append(f"Preservation: {json.dumps(data.get('preservation'), sort_keys=True)}")
    return lines
