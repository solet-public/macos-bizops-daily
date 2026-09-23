#!/usr/bin/env python3
"""Run an installed stage resume and retain its selected real-adapter evidence.

The host owns VM lifecycle and mounts.  This guest-only program owns neither:
it receives immutable per-adapter exchanges from the read-only stage share,
runs the installed manager once, and accepts a receipt only when the manager's
captured request/result envelopes prove every selected declared probe.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import uuid
from pathlib import Path
from typing import cast

from tart_stage_resume_contract import ContractError, selected_adapters, sha256
from tart_stage_resume_exchange import read_exchange, validate_guest_receipt

_CAPTURE_ENV = "SOLET_ADAPTER_ENVELOPE_CAPTURE_ROOT"
_ADAPTERS = ("homebrew", "lm_studio", "postgresql", "launchd")
_TOOLS = {
    "homebrew": "brew",
    "lm_studio": "lms",
    "postgresql": "psql",
    "launchd": "launchctl",
}


class GuestContractError(RuntimeError):
    """The guest cannot prove the requested adapter contract."""


def invoke_installed_resume(
    manager: Path,
    *,
    name: str,
    target: Path,
    resume_stage: str,
    capture_root: Path,
    decisions: tuple[str, ...],
    approval_fingerprint: str,
) -> subprocess.CompletedProcess[str]:
    """Run the installed manager and enable only its explicit receipt seam."""

    resolved = manager.resolve(strict=True)
    if not resolved.is_file() or not resolved.is_absolute():
        raise GuestContractError("installed manager must be one absolute regular file")
    if capture_root.exists():
        raise GuestContractError("adapter capture root must be absent before resume")
    capture_root.mkdir(parents=True)
    environment = dict(os.environ)
    environment[_CAPTURE_ENV] = str(capture_root)
    argv = [
        str(resolved),
        "create",
        name,
        "--target",
        str(target),
        "--resume-stage",
        resume_stage,
    ]
    for decision in decisions:
        argv.extend(("--decision", decision))
    argv.extend(("--yes", "--approval-fingerprint", approval_fingerprint, "--json"))
    return subprocess.run(argv, check=False, capture_output=True, text=True, env=environment)


def assemble_receipts(
    *,
    exchange_root: Path,
    receipt_root: Path,
    capture_root: Path,
    selector: str,
    target: Path,
) -> None:
    """Write one validated receipt for each selected real adapter contract."""

    pairs = _captured_pairs(capture_root)
    for adapter in selected_adapters(selector):
        if adapter not in _ADAPTERS:
            raise GuestContractError("adapter selector is invalid")
        exchange_path = exchange_root / f"{adapter}.json"
        exchange = read_exchange(exchange_path)
        probes = _probe_results(exchange, pairs)
        tool_version = _tool_version(adapter, target)
        receipt = {
            "schema_version": 1,
            "adapter": adapter,
            "exchange_sha256": sha256(exchange_path),
            "flow_sha256": exchange["flow_sha256"],
            "tool": exchange["tool"],
            "tool_version": tool_version,
            "probes": probes,
        }
        receipt_path = receipt_root / f"{adapter}.json"
        _write_json(receipt_path, receipt)
        try:
            validate_guest_receipt(exchange_path, receipt_path)
        except ContractError as exc:
            raise GuestContractError(f"{adapter} receipt does not validate: {exc}") from exc
        _write_selected_exchanges(receipt_root / f"{adapter}.exchanges.json", exchange, pairs)


def _captured_pairs(root: Path) -> dict[str, tuple[dict[str, object], dict[str, object]]]:
    requests = _envelope_paths(root, "request")
    results = _envelope_paths(root, "result")
    if not requests or set(requests) != set(results):
        raise GuestContractError("captured adapter envelopes are missing or unpaired")
    return {
        request_id: _captured_pair(request_id, requests[request_id], results[request_id])
        for request_id in sorted(requests)
    }


def _envelope_paths(root: Path, kind: str) -> dict[str, Path]:
    suffix = f"-{kind}.json"
    return {path.name.removesuffix(suffix): path for path in root.glob(f"*{suffix}")}


def _captured_pair(
    request_id: str, request_path: Path, result_path: Path
) -> tuple[dict[str, object], dict[str, object]]:
    request, result = _read_object(request_path), _read_object(result_path)
    _validate_captured_pair(request_id, request, result)
    return request, result


def _validate_captured_pair(
    request_id: str, request: dict[str, object], result: dict[str, object]
) -> None:
    if request.get("kind") != "operation_request" or result.get("kind") != "operation_result":
        raise GuestContractError("captured adapter envelope kind is invalid")
    if request.get("request_id") != request_id or result.get("request_id") != request_id:
        raise GuestContractError("captured adapter envelope identity is invalid")
    if request.get("operation_id") != result.get("operation_id"):
        raise GuestContractError("captured result operation identity differs from request")


def _probe_results(
    exchange: dict[str, object], pairs: dict[str, tuple[dict[str, object], dict[str, object]]]
) -> list[dict[str, str]]:
    raw_probes = exchange.get("probes")
    if not isinstance(raw_probes, list):
        raise GuestContractError("exchange probes are invalid")
    observed: list[dict[str, str]] = []
    used: set[str] = set()
    for raw_probe in raw_probes:
        identifier, reference, runner = _probe_identity(raw_probe)
        request_id, result = _matching_probe_pair(identifier, reference, pairs)
        if request_id in used:
            raise GuestContractError("one manager exchange was reused for multiple probes")
        used.add(request_id)
        if result.get("checkpoint_status") != "verified":
            raise GuestContractError(
                f"probe {identifier!r} was not verified by the installed manager"
            )
        observed.append(
            {
                "id": identifier,
                "runner": runner,
                "probe_ref": reference,
                "checkpoint_status": "verified",
            }
        )
    return observed


def _probe_identity(raw_probe: object) -> tuple[str, str, str]:
    if not isinstance(raw_probe, dict):
        raise GuestContractError("exchange probe is invalid")
    values = raw_probe.get("id"), raw_probe.get("probe_ref"), raw_probe.get("runner")
    if not all(isinstance(value, str) and value for value in values):
        raise GuestContractError("exchange probe identity is invalid")
    return cast(tuple[str, str, str], values)


def _matching_probe_pair(
    identifier: str,
    reference: str,
    pairs: dict[str, tuple[dict[str, object], dict[str, object]]],
) -> tuple[str, dict[str, object]]:
    matches = [
        (request_id, result)
        for request_id, (request, result) in pairs.items()
        if request.get("operation_id") == identifier
        and request.get("operation_ref") == reference
        and request.get("phase") == "probe"
    ]
    if len(matches) != 1:
        raise GuestContractError(f"probe {identifier!r} lacks one matching manager exchange")
    return matches[0]


def _tool_version(adapter: str, target: Path) -> str:
    target_python = target / ".venv" / "bin" / "python3"
    executable = _resolved_tool(adapter, target_python)
    return _tool_version_output(adapter, executable)


def _resolved_tool(adapter: str, target_python: Path) -> str:
    resolvers = {
        "homebrew": (
            "from github_midwife_plugin.credential_seed import "
            "_resolve_brew_executable; print(_resolve_brew_executable())"
        ),
        "lm_studio": (
            "from pathlib import Path; from github_midwife_plugin.lm_studio_models "
            "import cli_path; print(cli_path(Path.home()))"
        ),
        "postgresql": (
            "from github_midwife_plugin.credential_seed import "
            "_resolve_psql_binary; print(_resolve_psql_binary())"
        ),
        "launchd": "print('/bin/launchctl')",
    }
    resolver = resolvers.get(adapter)
    if resolver is None or not target_python.is_file():
        raise GuestContractError("target cannot resolve the selected adapter tool")
    resolved = subprocess.run(
        [str(target_python), "-c", resolver], check=False, capture_output=True, text=True
    )
    executable = resolved.stdout.strip()
    if resolved.returncode != 0 or not executable or not Path(executable).is_absolute():
        raise GuestContractError(f"target did not resolve {adapter} to one absolute tool")
    return executable


def _tool_version_output(adapter: str, executable: str) -> str:
    completed = subprocess.run(
        [executable, "version" if adapter == "launchd" else "--version"],
        check=False,
        capture_output=True,
        text=True,
    )
    output = (completed.stdout + completed.stderr).strip()
    if completed.returncode != 0 or not output or len(output) > 512 or "\x00" in output:
        raise GuestContractError(f"real {adapter} version command did not pass")
    return output


def _write_selected_exchanges(
    path: Path,
    exchange: dict[str, object],
    pairs: dict[str, tuple[dict[str, object], dict[str, object]]],
) -> None:
    selected = []
    probes = cast(list[object], exchange["probes"])
    for raw_probe in probes:
        probe = cast(dict[str, object], raw_probe)
        matches = [
            (request_id, request, result)
            for request_id, (request, result) in pairs.items()
            if request.get("operation_id") == probe["id"]
            and request.get("operation_ref") == probe["probe_ref"]
        ]
        if len(matches) != 1:
            raise GuestContractError("selected exchange disappeared during receipt assembly")
        request_id, request, result = matches[0]
        selected.append(
            {
                "request_id": request_id,
                "request": request,
                "result": result,
                "request_sha256": _object_sha256(request),
                "result_sha256": _object_sha256(result),
            }
        )
    _write_json(path, {"schema_version": 1, "exchange": exchange, "selected": selected})


def _object_sha256(value: object) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _read_object(path: Path) -> dict[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise GuestContractError(f"captured envelope cannot be read: {exc}") from exc
    if not isinstance(value, dict):
        raise GuestContractError("captured envelope must be an object")
    return cast(dict[str, object], value)


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--exchange-root", type=Path, required=True)
    parser.add_argument("--receipt-root", type=Path, required=True)
    parser.add_argument("--manager", type=Path, required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--adapter", choices=("all", *_ADAPTERS), required=True)
    parser.add_argument("--resume-stage", required=True)
    parser.add_argument("--instance-name", required=True)
    parser.add_argument("--target", type=Path, required=True)
    parser.add_argument("--approval-fingerprint", required=True)
    parser.add_argument("--decision", action="append", default=[])
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        uuid.UUID(args.run_id)
        if not args.exchange_root.is_absolute() or not args.receipt_root.is_absolute():
            raise GuestContractError("exchange and receipt roots must be absolute")
        if not args.target.is_absolute():
            raise GuestContractError("target must be absolute")
        capture_root = args.receipt_root / args.run_id / "adapter-envelopes"
        completed = invoke_installed_resume(
            args.manager,
            name=args.instance_name,
            target=args.target,
            resume_stage=args.resume_stage,
            capture_root=capture_root,
            decisions=tuple(args.decision),
            approval_fingerprint=args.approval_fingerprint,
        )
        _write_json(args.receipt_root / args.run_id / "manager-result.json", {
            "exit_code": completed.returncode,
            "stdout": completed.stdout,
            "stderr": completed.stderr,
        })
        if completed.returncode != 0:
            raise GuestContractError(f"installed manager resume exited {completed.returncode}")
        assemble_receipts(
            exchange_root=args.exchange_root,
            receipt_root=args.receipt_root / args.run_id,
            capture_root=capture_root,
            selector=args.adapter,
            target=args.target,
        )
    except (GuestContractError, ContractError, ValueError) as exc:
        print(f"stage-resume guest BLOCKED: {exc}", file=sys.stderr)
        return 30
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
