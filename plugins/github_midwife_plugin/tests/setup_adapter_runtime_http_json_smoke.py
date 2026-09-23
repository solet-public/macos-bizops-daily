"""Regression smoke for HTTP JSON bodies exceeding the evidence cap."""

from __future__ import annotations

import json
import math
import os
import shutil
import sys
import tempfile
import threading
import urllib.request
from dataclasses import dataclass, replace
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from socketserver import TCPServer
from typing import cast
from unittest.mock import patch

_PLUGIN_ROOT = Path(__file__).resolve().parents[1]
_SRC = _PLUGIN_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from github_midwife_plugin import setup_adapter  # noqa: E402
from github_midwife_plugin.installation_model_doctor import (  # noqa: E402
    _platform_embedding_dimension,
    embedding_qualification,
    inference_qualification,
)
from github_midwife_plugin.setup_adapter import dispatch_request  # noqa: E402
from github_midwife_plugin.setup_adapter_contract import (  # noqa: E402
    AdapterRequest,
    JsonObject,
    JsonValue,
)
from github_midwife_plugin.setup_adapter_runtime import (  # noqa: E402
    _HTTP_JSON_LIMIT,
    SystemRuntime,
    _HttpBody,
    _read_bounded_http_body,
)

_passed = 0
_failed: list[str] = []
_NUMBER = "0.12345678901234567890123456789"
_VECTOR_JSON = ",".join([_NUMBER] * 768)
_RESPONSE = (
    '{"model":"fixture-embedding","data":[{"embedding":['
    + _VECTOR_JSON
    + ']},{"embedding":['
    + _VECTOR_JSON
    + "]}]}"
).encode("utf-8")


def _check(condition: object, label: str) -> None:
    global _passed
    if condition:
        _passed += 1
        print(f"  PASS  {label}")
    else:
        _failed.append(label)
        print(f"  FAIL  {label}")


class _EmbeddingHandler(BaseHTTPRequestHandler):
    """Serve one deterministic embedding response over a real HTTP boundary."""

    request_payload: object = None

    def do_POST(self) -> None:  # noqa: N802 - stdlib handler protocol
        length = int(self.headers.get("Content-Length", "0"))
        type(self).request_payload = json.loads(self.rfile.read(length))
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(_RESPONSE)))
        self.end_headers()
        self.wfile.write(_RESPONSE)

    def log_message(self, format: str, *args: object) -> None:
        del format, args


class _LoopbackHTTPServer(HTTPServer):
    """HTTP server that avoids macOS reverse-DNS lookup during fixture bind."""

    def server_bind(self) -> None:
        TCPServer.server_bind(self)
        host, port = cast(tuple[str, int], self.server_address)
        self.server_name = host
        self.server_port = port


class _BoundaryHandler(BaseHTTPRequestHandler):
    """Serve byte-exact boundary cases through the real HTTP transport."""

    multibyte_text = "é" * ((_HTTP_JSON_LIMIT // 2) + 1)
    routes = {
        "/declared-oversize": (_HTTP_JSON_LIMIT + 1, b""),
        "/no-length-oversize": (None, b"x" * (_HTTP_JSON_LIMIT + 1)),
        "/multibyte-oversize": (None, multibyte_text.encode("utf-8")),
        "/truncated": (3, b"{}"),
    }
    requested_paths: list[str] = []

    def do_GET(self) -> None:  # noqa: N802 - stdlib handler protocol
        declared, body = self.routes[self.path]
        type(self).requested_paths.append(self.path)
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        if declared is not None:
            self.send_header("Content-Length", str(declared))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        del format, args


@dataclass(frozen=True, slots=True)
class _TransportObservation:
    content_length: str | None
    body: bytes | None
    error: ValueError | None


def _read_transport_case(base_url: str, path: str, *, limit: int) -> _TransportObservation:
    with urllib.request.urlopen(f"{base_url}{path}", timeout=5) as response:  # noqa: S310
        content_length = response.headers.get("Content-Length")
        try:
            body = _read_bounded_http_body(cast(_HttpBody, response), limit=limit)
        except ValueError as exc:
            return _TransportObservation(content_length, None, exc)
    return _TransportObservation(content_length, body, None)


def _qualification_request(
    base_url: str,
    *,
    target: Path = Path("/private/tmp/setup-adapter-http-cap-regression"),
) -> AdapterRequest:
    return AdapterRequest(
        request_id="00000000-0000-4000-8000-000000000001",
        operation_id="setup.models.qualify_embedding",
        operation_ref="setup::models.qualify_embedding",
        phase="probe",
        probe_purpose="decision_qualification",
        attempt=1,
        name="cap-regression",
        target=target,
        flow_source_revision="0" * 40,
        answers_fingerprint="sha256:" + "0" * 64,
        approval_fingerprint=None,
        dry_run=True,
        timeout_seconds=15,
        public_inputs={
            "candidate_id": "fixture-embedding",
            "lm_studio_base_url": base_url,
        },
    )


def _discovery_request() -> AdapterRequest:
    return AdapterRequest(
        request_id="00000000-0000-4000-8000-000000000004",
        operation_id="setup.models.discover_lm_studio",
        operation_ref="setup::models.discover_lm_studio",
        phase="probe",
        probe_purpose="decision_discovery",
        attempt=1,
        name="adapter-runtime-regression",
        target=Path("/private/tmp/setup-adapter-runtime-regression"),
        flow_source_revision="0" * 40,
        answers_fingerprint="sha256:" + "0" * 64,
        approval_fingerprint=None,
        dry_run=True,
        timeout_seconds=10,
        public_inputs={
            "decision_id": "embedding_model",
            "lm_studio_base_url": "http://fixture.invalid/v1",
        },
    )


def _cli_probe_request(cli: str) -> AdapterRequest:
    return AdapterRequest(
        request_id="00000000-0000-4000-8000-000000000002",
        operation_id=f"hydration.{cli}.probe_cli",
        operation_ref=f"hydration::{cli}.probe_cli",
        phase="probe",
        probe_purpose="decision_qualification",
        attempt=1,
        name="cap-regression",
        target=Path("/private/tmp/setup-adapter-runtime-regression"),
        flow_source_revision="0" * 40,
        answers_fingerprint="sha256:" + "0" * 64,
        approval_fingerprint=None,
        dry_run=True,
        timeout_seconds=15,
        public_inputs={},
    )


def _inference_request() -> AdapterRequest:
    return AdapterRequest(
        request_id="00000000-0000-4000-8000-000000000003",
        operation_id="setup.models.qualify_structured_actions",
        operation_ref="setup::models.qualify_structured_actions",
        phase="probe",
        probe_purpose="decision_qualification",
        attempt=1,
        name="inference-qualification",
        target=Path("/private/tmp/setup-adapter-inference-qualification"),
        flow_source_revision="0" * 40,
        answers_fingerprint="sha256:" + "0" * 64,
        approval_fingerprint=None,
        dry_run=True,
        timeout_seconds=20,
        public_inputs={
            "candidate_id": "fixture-inference",
            "lm_studio_base_url": "http://fixture.invalid/v1",
        },
    )


@dataclass(slots=True)
class _StaticEmbeddingRuntime:
    """Return one exact embedding payload without crossing the network."""

    payload: JsonValue
    home: Path = Path("/private/tmp/setup-adapter-dimension-regression")

    def http_json(
        self,
        url: str,
        *,
        timeout_seconds: int,
        payload: dict[str, JsonValue] | None = None,
    ) -> tuple[int, JsonValue]:
        del url, timeout_seconds, payload
        return 200, self.payload

    def run(self, *args: object, **kwargs: object) -> object:
        del args, kwargs
        raise AssertionError("dimension qualification must not invoke a subprocess")

    def atomic_write(self, path: Path, content: str, *, mode: int) -> None:
        del path, content, mode
        raise AssertionError("dimension qualification must not write")


@dataclass(slots=True)
class _StaticInferenceRuntime:
    """Capture one structured qualification request without a live model."""

    response: JsonValue
    request_payload: JsonObject | None = None

    def http_json(
        self,
        url: str,
        *,
        timeout_seconds: int,
        payload: JsonObject | None = None,
    ) -> tuple[int, JsonValue]:
        del url, timeout_seconds
        self.request_payload = payload
        return 200, self.response

    def run(self, *args: object, **kwargs: object) -> object:
        del args, kwargs
        raise AssertionError("inference qualification must not invoke a subprocess")

    def atomic_write(self, path: Path, content: str, *, mode: int) -> None:
        del path, content, mode
        raise AssertionError("inference qualification must not write")


def _embedding_vectors(payload: JsonValue) -> list[object]:
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, list):
        return []
    return [item.get("embedding") for item in data if isinstance(item, dict)]


def _finite_number(value: object) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
    )


def _finite_vector(value: object) -> bool:
    return isinstance(value, list) and len(value) == 768 and all(_finite_number(item) for item in value)


def _finite_identical_pair(payload: JsonValue) -> bool:
    vectors = _embedding_vectors(payload)
    return len(vectors) == 2 and all(_finite_vector(vector) for vector in vectors) and vectors[0] == vectors[1]


def _qualified_inference_response(model: str) -> JsonObject:
    return {
        "model": model,
        "system_fingerprint": "fp-qualification",
        "choices": [
            {
                "message": {"content": "", "reasoning_content": "reasoning preamble"},
                "finish_reason": "length",
            }
        ],
    }


def _exercise_http_json() -> None:
    _check(46_000 <= len(_RESPONSE) <= 50_176, f"fixture is exactly {len(_RESPONSE)} UTF-8 bytes")
    fixture_value: object = json.loads(_RESPONSE)
    fixture_payload = cast(JsonValue, fixture_value)
    _check(
        _finite_identical_pair(fixture_payload),
        "serialized fixture is two finite identical 768-dimensional vectors before client invocation",
    )
    server = _LoopbackHTTPServer(("127.0.0.1", 0), _EmbeddingHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = cast(tuple[str, int], server.server_address)
    base_url = f"http://{host}:{port}/v1"
    status: int | None = None
    payload: JsonValue = None
    qualification: JsonValue = None
    error: Exception | None = None
    try:
        status, payload = SystemRuntime().http_json(
            f"{base_url}/embeddings",
            timeout_seconds=5,
            payload={"model": "fixture-embedding", "input": ["qualification", "qualification"]},
        )
        with tempfile.TemporaryDirectory(prefix="setup-adapter-http-binding-") as raw_target:
            target = Path(raw_target)
            config = target / "profile/config"
            config.mkdir(parents=True)
            (config / "service_bindings.json").write_text(
                json.dumps({"embedding_service": "openai_embeddings_plugin"}),
                encoding="utf-8",
            )
            qualification = embedding_qualification(
                _qualification_request(base_url, target=target),
                SystemRuntime(),
            )
    except Exception as exc:  # the regression was a fatal ValueError at this boundary
        error = exc
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    _check(error is None, "large embedding response does not hit evidence cap")
    _check(status == 200, "HTTP status survives large JSON response")
    _check(
        _finite_identical_pair(payload),
        "two finite identical 768-dimensional vectors are parsed",
    )
    _check(
        isinstance(qualification, dict) and qualification.get("checkpoint_status") == "verified",
        "parsed adapter qualification verifies the candidate",
    )
    _check(
        _EmbeddingHandler.request_payload
        == {"model": "fixture-embedding", "input": ["qualification", "qualification"]},
        "qualification POST payload reaches the model endpoint",
    )


def _boundary_controls() -> None:
    limit = _HTTP_JSON_LIMIT
    _BoundaryHandler.requested_paths = []
    server = _LoopbackHTTPServer(("127.0.0.1", 0), _BoundaryHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = cast(tuple[str, int], server.server_address)
    base_url = f"http://{host}:{port}"
    try:
        declared = _read_transport_case(base_url, "/declared-oversize", limit=limit)
        streamed = _read_transport_case(base_url, "/no-length-oversize", limit=limit)
        multibyte = _read_transport_case(base_url, "/multibyte-oversize", limit=limit)
        truncated = _read_transport_case(base_url, "/truncated", limit=limit)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    _check(
        declared.content_length == str(limit + 1)
        and str(declared.error)
        == (
            "HTTP JSON response body is too large: "
            f"observed {limit + 1} bytes from Content-Length; limit is {limit} bytes"
        ),
        f"declared Content-Length {limit + 1} is refused before body buffering at bound {limit}",
    )
    _check(
        streamed.content_length is None
        and str(streamed.error)
        == (
            "HTTP JSON response body is too large: "
            f"observed at least {limit + 1} bytes; limit is {limit} bytes"
        ),
        f"no-length transport is refused by a bounded {limit + 1}-byte read at bound {limit}",
    )
    _check(
        len(_BoundaryHandler.multibyte_text) == (limit // 2) + 1
        and len(_BoundaryHandler.multibyte_text) < limit
        and len(_BoundaryHandler.multibyte_text.encode("utf-8")) == limit + 2
        and multibyte.content_length is None
        and str(multibyte.error)
        == (
            "HTTP JSON response body is too large: "
            f"observed at least {limit + 1} bytes; limit is {limit} bytes"
        ),
        (
            f"{len(_BoundaryHandler.multibyte_text)}-character / "
            f"{len(_BoundaryHandler.multibyte_text.encode('utf-8'))}-byte UTF-8 body "
            f"is refused by its byte count at bound {limit}"
        ),
    )
    _check(
        truncated.content_length == "3"
        and str(truncated.error)
        == "HTTP JSON response body is truncated: observed 2 bytes; Content-Length declares 3 bytes",
        "declared 3-byte transport truncated at 2 reports expected and observed bytes",
    )
    _check(
        _BoundaryHandler.requested_paths == list(_BoundaryHandler.routes),
        "all four named loopback transport routes were exercised in order",
    )


def _dimension_controls() -> None:
    with tempfile.TemporaryDirectory(prefix="setup-adapter-dimension-") as raw_root:
        fixture_root = Path(raw_root)
        bound_target = fixture_root / "bound"
        config = bound_target / "profile/config"
        config.mkdir(parents=True)
        (config / "service_bindings.json").write_text(
            json.dumps({"embedding_service": "openai_embeddings_plugin"}),
            encoding="utf-8",
        )
        unbound_target = fixture_root / "unbound"
        with patch.dict(os.environ, {"ANANTA_EMBEDDING_SERVICE": ""}):
            _check(
                _platform_embedding_dimension(bound_target) == 768,
                "platform expectation resolves from a fixture-owned provider binding",
            )
            _check(
                _platform_embedding_dimension(unbound_target) is None,
                "unavailable bound-provider expectation has no fallback dimension",
            )
            request = _qualification_request(
                "http://fixture.invalid/v1",
                target=bound_target,
            )
            _check(
                "expected_dimension" not in request.public_inputs,
                "embedding qualification does not accept a caller-supplied expected dimension",
            )
            injected = dispatch_request(
                replace(
                    request,
                    public_inputs={**request.public_inputs, "expected_dimension": 768},
                ),
                _StaticEmbeddingRuntime({}),
            )
            _check(
                injected.get("error_kind") == "adapter_protocol_error",
                "closed adapter input validation rejects a stale expected-dimension injection",
            )
            vector = [0.125] * 768
            verified = embedding_qualification(
                request,
                _StaticEmbeddingRuntime(
                    {
                        "model": "fixture-embedding",
                        "data": [{"embedding": vector}, {"embedding": vector.copy()}],
                    }
                ),
            )
            _check(
                verified.get("checkpoint_status") == "verified",
                "adapter qualification sources 768 from its target binding without candidate metadata",
            )
            for observed_dimension in (1, 769):
                vector = [0.125] * observed_dimension
                payload: JsonValue = {
                    "model": "fixture-embedding",
                    "data": [{"embedding": vector}, {"embedding": vector.copy()}]
                }
                qualification = embedding_qualification(
                    _qualification_request(
                        "http://fixture.invalid/v1",
                        target=bound_target,
                    ),
                    _StaticEmbeddingRuntime(payload),
                )
                repair = str(qualification.get("repair"))
                _check(
                    qualification.get("checkpoint_status") != "verified",
                    f"observed {observed_dimension}-dimensional pair is rejected against target binding expectation 768",
                )
                _check(
                    "expected 768" in repair and f"observed {observed_dimension}" in repair,
                    f"dimension-{observed_dimension} refusal names target expectation 768 and observed {observed_dimension}",
                )

            vector = [0.125] * 768
            substituted = embedding_qualification(
                request,
                _StaticEmbeddingRuntime(
                    {
                        "model": "fixture-substituted-embedding",
                        "data": [{"embedding": vector}, {"embedding": vector.copy()}],
                    }
                ),
            )
            substituted_repair = str(substituted.get("repair"))
            _check(
                substituted.get("checkpoint_status") != "verified",
                "response-model substitution red mutation is non-green",
            )
            _check(
                "fixture-embedding" in substituted_repair
                and "fixture-substituted-embedding" in substituted_repair,
                "response-model substitution refusal names requested and returned public IDs",
            )

            missing_model = embedding_qualification(
                request,
                _StaticEmbeddingRuntime(
                    {"data": [{"embedding": vector}, {"embedding": vector.copy()}]}
                ),
            )
            _check(
                missing_model.get("checkpoint_status") != "verified",
                "missing response-model red mutation is non-green",
            )

            unavailable = embedding_qualification(
                _qualification_request(
                    "http://fixture.invalid/v1",
                    target=unbound_target,
                ),
                _StaticEmbeddingRuntime(
                    {
                        "model": "fixture-embedding",
                        "data": [{"embedding": vector}, {"embedding": vector.copy()}],
                    }
                ),
            )
            unavailable_repair = str(unavailable.get("repair"))
            _check(
                unavailable.get("checkpoint_status") != "verified",
                "qualification fails closed when target binding expectation is unavailable",
            )
            _check(
                "expected dimension is unavailable" in unavailable_repair
                and "observed 768" in unavailable_repair
                and "get_default_dimensions" in unavailable_repair,
                "unavailable target-binding refusal names expected state, observed 768, and repair source",
            )


def _inference_qualification_controls() -> None:
    runtime = _StaticInferenceRuntime(_qualified_inference_response("fixture-inference"))
    qualification = inference_qualification(_inference_request(), runtime)
    _check(
        qualification.get("checkpoint_status") == "verified",
        "response model identity verifies the inference candidate",
    )
    substituted = inference_qualification(
        _inference_request(),
        _StaticInferenceRuntime(_qualified_inference_response("fixture-substituted-inference")),
    )
    _check(
        substituted.get("checkpoint_status") != "verified"
        and "fixture-inference" in str(substituted.get("repair"))
        and "fixture-substituted-inference" in str(substituted.get("repair")),
        "response-model substitution is refused and names requested and returned IDs",
    )
    _check(
        _plain_identity_request_matches(runtime.request_payload),
        "inference qualification sends the minimal plain identity request shape",
    )
    with tempfile.TemporaryDirectory(prefix="inference-qualification-fallback-") as raw_target:
        target = Path(raw_target)
        config_path = target / "profile/config/plugins/default_inference_plugin.json"
        config_path.parent.mkdir(parents=True)
        config_path.write_text(
            json.dumps({"model": "materialized-inference-model"}), encoding="utf-8"
        )

        flow_path = target / "plugins/github_midwife_plugin/knowledge_base/macos_setup_flow.json"
        flow_path.parent.mkdir(parents=True)
        shutil.copyfile(_PLUGIN_ROOT / "knowledge_base/macos_setup_flow.json", flow_path)
        fallback_runtime = _StaticInferenceRuntime(
            _qualified_inference_response("materialized-inference-model")
        )
        fallback = dispatch_request(
            replace(_inference_request(), target=target, public_inputs={}),
            fallback_runtime,
        )
        _check(
            fallback.get("checkpoint_status") == "verified"
            and isinstance(fallback_runtime.request_payload, dict)
            and fallback_runtime.request_payload.get("model") == "materialized-inference-model",
            "empty inference probe inputs qualify the materialized configured model",
        )
        config_path.unlink()
        missing_runtime = _StaticInferenceRuntime(
            _qualified_inference_response("materialized-inference-model")
        )
        missing = dispatch_request(
            replace(_inference_request(), target=target, public_inputs={}),
            missing_runtime,
        )
        _check(
            missing.get("checkpoint_status") == "blocked"
            and missing.get("error_kind") == "operation_input_missing"
            and missing.get("repair") == "candidate_id is required."
            and missing_runtime.request_payload is None,
            "missing materialized inference selection fails closed with the precise repair",
        )


def _plain_identity_request_matches(payload: object) -> bool:
    return (
        isinstance(payload, dict)
        and payload.get("temperature") == 0
        and payload.get("messages") == [{"role": "user", "content": "What model is this?"}]
        and payload.get("max_tokens") == 8
        and "response_format" not in payload
    )


def _evidence_output_control() -> None:
    outcome = SystemRuntime().run(
        ("/usr/bin/printf", "%5000s", ""),
        timeout_seconds=5,
    )
    _check(outcome.ok, "command evidence control executes")
    _check(len(outcome.stdout) == 4096, "public command evidence remains capped")


def _missing_executable_control() -> None:
    outcome = SystemRuntime().run(
        ("definitely-not-installed-setup-adapter-command", "--version"),
        timeout_seconds=5,
        extra_env={"PATH": "/usr/bin:/bin:/usr/sbin:/sbin"},
    )
    _check(
        outcome.returncode is None
        and not outcome.timed_out
        and outcome.executable_missing
        and outcome.launch_error is not None,
        "missing executable returns a distinct command outcome under an explicit PATH",
    )


def _command_outcome_state_controls() -> None:
    success = SystemRuntime().run(("/usr/bin/true",), timeout_seconds=5)
    failed = SystemRuntime().run(("/usr/bin/false",), timeout_seconds=5)
    timed_out = SystemRuntime().run(("/bin/sleep", "1"), timeout_seconds=0)
    with tempfile.TemporaryDirectory() as directory:
        non_executable = Path(directory) / "non-executable"
        non_executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        non_executable.chmod(0o600)
        launch_failed = SystemRuntime().run((str(non_executable),), timeout_seconds=5)

    _check(success.ok and not success.executable_missing, "exit 0 remains successful")
    _check(
        success.returncode == 0 and not failed.ok and failed.returncode != 0 and not failed.executable_missing,
        "non-zero exit remains distinct from a missing executable",
    )
    _check(
        timed_out.returncode is None and timed_out.timed_out and not timed_out.executable_missing,
        "timeout remains distinct from a missing executable",
    )
    _check(
        launch_failed.returncode is None
        and not launch_failed.timed_out
        and not launch_failed.executable_missing
        and launch_failed.launch_error is not None,
        "other OS launch errors are not reclassified as missing executables",
    )


def _missing_cli_probe_controls() -> None:
    """PATH restriction alone no longer proves a CLI absent: resolve_executable's
    Homebrew-bin fallback checks fixed absolute paths regardless of PATH (by
    design, iss_e1d5285b), so a real dev host with codex/claude genuinely
    installed there would find them anyway. Redirect the fallback directories
    to guaranteed-nonexistent paths so this stays a true "genuinely absent"
    simulation independent of the host's actual installs."""
    with (
        patch.dict(os.environ, {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin"}, clear=True),
        patch(
            "github_midwife_plugin.setup_adapter_runtime._HOMEBREW_BIN_DIRECTORIES",
            (
                "/private/tmp/setup-adapter-runtime-regression/nonexistent-brew-bin-a",
                "/private/tmp/setup-adapter-runtime-regression/nonexistent-brew-bin-b",
            ),
        ),
    ):
        claude = dispatch_request(_cli_probe_request("claude"), SystemRuntime())
        codex = dispatch_request(_cli_probe_request("codex"), SystemRuntime())
    for cli, response in (("claude", claude), ("codex", codex)):
        _check(
            response.get("checkpoint_status") == "blocked"
            and response.get("error_kind") == f"{cli}_cli_failed"
            and f"Install the selected {cli} CLI through its provisioning operation."
            == response.get("repair"),
            f"missing {cli} CLI becomes a clean probe-specific blocked result",
        )


def _dispatch_exception_controls() -> None:
    request = _discovery_request()

    def runtime_failure(_request: AdapterRequest, _runtime: object) -> JsonObject:
        raise OSError("fixture runtime boundary failed")

    with patch.object(
        setup_adapter,
        "operation_handlers",
        return_value={request.operation_ref: runtime_failure},
    ):
        response = dispatch_request(request, SystemRuntime())
    evidence_items = response.get("evidence")
    _check(
        response.get("checkpoint_status") == "failed"
        and response.get("error_kind") == "adapter_runtime_error"
        and response.get("retry_safe") is False
        and isinstance(evidence_items, list)
        and len(evidence_items) == 1
        and evidence_items[0].get("observed")
        == [
            'exception_summary="fixture runtime boundary failed"',
            'exception_type="OSError"',
        ],
        "generic runtime fallback preserves a bounded exception diagnosis in evidence",
    )

    def programmer_failure(_request: AdapterRequest, _runtime: object) -> JsonObject:
        raise TypeError("fixture programmer defect")

    try:
        with patch.object(
            setup_adapter,
            "operation_handlers",
            return_value={request.operation_ref: programmer_failure},
        ):
            dispatch_request(request, SystemRuntime())
    except TypeError as exc:
        _check(
            str(exc) == "fixture programmer defect",
            "programmer TypeError remains visible rather than an adapter runtime result",
        )
    else:
        _check(False, "programmer TypeError is not converted into adapter runtime failure")


def main() -> int:
    _exercise_http_json()
    _boundary_controls()
    _dimension_controls()
    _inference_qualification_controls()
    _evidence_output_control()
    _missing_executable_control()
    _dispatch_exception_controls()
    _command_outcome_state_controls()
    _missing_cli_probe_controls()
    print(f"\n{_passed} passed, {len(_failed)} failed")
    return 1 if _failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
