"""Regression smoke for OpenAI-compatible served-model substitution."""

from __future__ import annotations

import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from socketserver import TCPServer
from typing import cast

_PLUGIN_ROOT = Path(__file__).resolve().parents[1]
_SRC = _PLUGIN_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from github_midwife_plugin.installation_model_doctor import inference_qualification  # noqa: E402
from github_midwife_plugin.setup_adapter_contract import AdapterRequest, JsonObject  # noqa: E402
from github_midwife_plugin.setup_adapter_runtime import SystemRuntime  # noqa: E402


class _LoopbackServer(HTTPServer):
    """Avoid reverse-DNS lookup while binding the fixture server."""

    def server_bind(self) -> None:
        TCPServer.server_bind(self)
        host, port = cast(tuple[str, int], self.server_address)
        self.server_name = host
        self.server_port = port


class _Handler(BaseHTTPRequestHandler):
    served_model = "requested-X"
    requests: list[JsonObject] = []

    def do_POST(self) -> None:  # noqa: N802 - stdlib handler protocol
        size = int(self.headers.get("Content-Length", "0"))
        payload = json.loads(self.rfile.read(size))
        if isinstance(payload, dict):
            type(self).requests.append(cast(JsonObject, payload))
        response = json.dumps(
            {
                "model": type(self).served_model,
                "choices": [{"message": {"content": '{"action":"qualify"}'}}],
            }
        ).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(response)))
        self.end_headers()
        self.wfile.write(response)

    def log_message(self, format: str, *args: object) -> None:
        del format, args


class _TimeoutHonouringRuntime:
    """Return a valid response only when the probe supplies enough time."""

    def __init__(self, response: JsonObject, minimum_timeout_seconds: int) -> None:
        self.response = response
        self.minimum_timeout_seconds = minimum_timeout_seconds
        self.requested_timeout_seconds: int | None = None

    def http_json(
        self,
        url: str,
        *,
        timeout_seconds: int,
        payload: JsonObject | None = None,
    ) -> tuple[int, JsonObject | None]:
        del url, payload
        self.requested_timeout_seconds = timeout_seconds
        if timeout_seconds < self.minimum_timeout_seconds:
            raise TimeoutError("fixture response exceeded the supplied timeout")
        return 200, self.response

    def run(self, *args: object, **kwargs: object) -> object:
        del args, kwargs
        raise AssertionError("inference qualification must not invoke a subprocess")

    def atomic_write(self, path: Path, content: str, *, mode: int) -> None:
        del path, content, mode
        raise AssertionError("inference qualification must not write")


def _request(base_url: str) -> AdapterRequest:
    return AdapterRequest(
        request_id="00000000-0000-4000-8000-000000000006",
        operation_id="setup.models.qualify_structured_actions",
        operation_ref="setup::models.qualify_structured_actions",
        phase="probe",
        probe_purpose="decision_qualification",
        attempt=1,
        name="served-model-identity",
        target=Path("/private/tmp/served-model-identity"),
        flow_source_revision="0" * 40,
        answers_fingerprint="sha256:" + "0" * 64,
        approval_fingerprint=None,
        dry_run=True,
        timeout_seconds=20,
        public_inputs={"candidate_id": "requested-X", "lm_studio_base_url": base_url},
    )


def _qualified_response(model: str) -> JsonObject:
    return {
        "model": model,
        "system_fingerprint": "fp-qualification",
        "choices": [
            {
                "message": {"content": "", "reasoning_content": "reasoning preamble"},
                "finish_reason": "length",
            }
        ],
        "usage": {"reasoning_tokens": 186},
    }


def _evidence_observed(result: JsonObject, evidence_id: str) -> object:
    evidence = result.get("evidence")
    if not isinstance(evidence, list):
        return None
    for item in evidence:
        if isinstance(item, dict) and item.get("id") == evidence_id:
            return item.get("observed")
    return None


def _timeout_and_evidence_controls(request: AdapterRequest) -> bool:
    delayed_runtime = _TimeoutHonouringRuntime(
        _qualified_response("requested-X"), minimum_timeout_seconds=21
    )
    delayed = inference_qualification(request, delayed_runtime)
    timed_out = inference_qualification(
        request,
        _TimeoutHonouringRuntime(_qualified_response("requested-X"), minimum_timeout_seconds=181),
    )
    identity_only = inference_qualification(
        request,
        _TimeoutHonouringRuntime(
            {
                "model": "requested-X",
                "choices": [],
            },
            minimum_timeout_seconds=1,
        ),
    )
    return all(
        (
            delayed.get("checkpoint_status") == "verified",
            delayed_runtime.requested_timeout_seconds == 180,
            delayed.get("duration_ms") is not None,
            _evidence_observed(delayed, "inference_qualification_elapsed_seconds") is not None,
            _evidence_observed(delayed, "inference_qualification_reasoning_tokens") == 186,
            timed_out.get("checkpoint_status") != "verified",
            "timed out" in str(timed_out.get("repair")),
            _evidence_observed(timed_out, "inference_qualification_elapsed_seconds") is not None,
            identity_only.get("checkpoint_status") == "verified",
        )
    )


def _qualification_request_controls() -> bool:
    return all(
        payload.get("messages")
        == [{"role": "user", "content": "What model is this?"}]
        and payload.get("max_tokens") == 8
        and "response_format" not in payload
        for payload in _Handler.requests
    )


def main() -> int:
    server = _LoopbackServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = cast(tuple[str, int], server.server_address)
    try:
        request = _request(f"http://{host}:{port}/v1")
        _Handler.served_model = "requested-X"
        identity = inference_qualification(request, SystemRuntime())
        _Handler.served_model = "served-Y"
        substituted = inference_qualification(request, SystemRuntime())
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
    repair = str(substituted.get("repair"))
    passed = (
        identity.get("checkpoint_status") == "verified"
        and substituted.get("checkpoint_status") != "verified"
        and "requested-X" in repair
        and "served-Y" in repair
        and [payload.get("model") for payload in _Handler.requests] == ["requested-X", "requested-X"]
        and _qualification_request_controls()
        and _timeout_and_evidence_controls(request)
    )
    print(
        "PASS served-model identity, timeout budget, and substitution refusal"
        if passed
        else "FAIL served-model identity"
    )
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
