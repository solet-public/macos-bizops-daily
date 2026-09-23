"""Real target-venv model-discovery controls over a loopback HTTP service."""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import cast

_PLUGIN_ROOT = Path(__file__).resolve().parents[1]
_REPOSITORY_ROOT = _PLUGIN_ROOT.parents[1]
_CHECKS = 0


def _check(condition: object, label: str) -> None:
    global _CHECKS
    _CHECKS += 1
    if not condition:
        raise AssertionError(label)


class _ModelsHandler(BaseHTTPRequestHandler):
    mode = "empty"
    nonce = ""
    requests: list[tuple[str, str, str]] = []

    def do_GET(self) -> None:  # noqa: N802 - stdlib handler protocol
        type(self).requests.append((self.command, self.path, type(self).nonce))
        if type(self).mode == "empty":
            body = b'{"data":[]}'
            status = 200
        elif type(self).mode == "models":
            body = (
                b'{"data":[{"id":"chat-only-model"},'
                b'{"id":"embedding-only-model"}]}'
            )
            status = 200
        elif type(self).mode == "unavailable":
            body = b'{"error":"unavailable"}'
            status = 503
        else:
            body = b'{"data":'
            status = 200
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Control-Nonce", type(self).nonce)
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        del format, args


def _request(
    target: Path,
    base_url: str,
    *,
    operation_ref: str = "setup::models.discover_lm_studio",
    decision_id: str = "embedding_model",
) -> dict[str, object]:
    public_inputs: dict[str, object] = {"decision_id": decision_id}
    if operation_ref == "setup::models.discover_lm_studio":
        public_inputs["lm_studio_base_url"] = base_url
    return {
        "protocol_version": 1,
        "kind": "operation_request",
        "request_id": "11111111-1111-4111-8111-111111111111",
        "operation_id": f"decision.{decision_id}.discover",
        "operation_ref": operation_ref,
        "phase": "probe",
        "probe_purpose": "decision_discovery",
        "attempt": 1,
        "name": "loopback-control",
        "target": str(target),
        "flow_id": "macos.repository_setup",
        "flow_source_revision": "a" * 40,
        "answers_fingerprint": "sha256:" + "b" * 64,
        "approval_fingerprint": None,
        "dry_run": True,
        "timeout_seconds": 10,
        "public_inputs": public_inputs,
    }


def _target_environment(target: Path) -> dict[str, str]:
    target_source = target / "plugins/github_midwife_plugin/src"
    shutil.copytree(_PLUGIN_ROOT / "src", target_source)
    subprocess.run((sys.executable, "-m", "venv", str(target / ".venv")), check=True)
    python = target / ".venv/bin/python3"
    _check(python.is_file(), "control invokes a real Python venv under the target")
    environment = dict(os.environ)
    environment["PYTHONPATH"] = os.pathsep.join(
        (str(target_source), str(_REPOSITORY_ROOT), *sys.path)
    )
    return environment


def _bind_target_embedding_service(target: Path) -> None:
    config = target / "profile/config"
    config.mkdir(parents=True)
    (config / "service_bindings.json").write_text(
        json.dumps({"embedding_service": "openai_embeddings_plugin"}),
        encoding="utf-8",
    )


def _write_target_lm_studio_default(target: Path, base_url: str) -> None:
    flow_path = target / "plugins/github_midwife_plugin/knowledge_base/macos_setup_flow.json"
    flow_path.parent.mkdir(parents=True)
    flow_path.write_text(
        json.dumps({"inputs": {"lm_studio_base_url": {"default": base_url}}}),
        encoding="utf-8",
    )


def _assert_target_module_resolution(target: Path, environment: dict[str, str]) -> None:
    completed = subprocess.run(
        (
            str(target / ".venv/bin/python3"),
            "-c",
            "import github_midwife_plugin.setup_adapter as module; print(module.__file__)",
        ),
        check=False,
        text=True,
        capture_output=True,
        env=environment,
    )
    resolved = Path(completed.stdout.strip()).resolve()
    _check(
        completed.returncode == 0 and resolved.is_relative_to(target.resolve()),
        "target venv imports the target-local setup adapter rather than the checkout: "
        f"{completed.stdout!r} {completed.stderr!r}",
    )


def _invoke(
    target: Path,
    environment: dict[str, str],
    base_url: str,
    *,
    operation_ref: str = "setup::models.discover_lm_studio",
    decision_id: str = "embedding_model",
) -> dict[str, object]:
    completed = subprocess.run(
        (str(target / ".venv/bin/python3"), "-m", "github_midwife_plugin.setup_adapter"),
        check=False,
        input=json.dumps(
            _request(
                target,
                base_url,
                operation_ref=operation_ref,
                decision_id=decision_id,
            )
        ),
        text=True,
        capture_output=True,
        env=environment,
    )
    _check(completed.returncode == 0, f"target adapter exits zero: {completed.stderr}")
    raw = cast(object, json.loads(completed.stdout))
    _check(isinstance(raw, dict), "target adapter returns one JSON object")
    return cast(dict[str, object], raw)


def _exercise_mode(
    target: Path,
    environment: dict[str, str],
    server: ThreadingHTTPServer,
    *,
    mode: str,
    nonce: str,
) -> dict[str, object]:
    _ModelsHandler.mode = mode
    _ModelsHandler.nonce = nonce
    _ModelsHandler.requests = []
    host, port = cast(tuple[str, int], server.server_address)
    _check(_ModelsHandler.requests == [], f"{mode} request ledger starts empty")
    result = _invoke(target, environment, f"http://{host}:{port}/v1")
    _check(
        _ModelsHandler.requests == [("GET", "/v1/models", nonce)],
        f"{mode} target adapter reaches the nonce-controlled loopback exactly once",
    )
    return result


def _exercise_ollama_gate(
    target: Path,
    environment: dict[str, str],
    server: ThreadingHTTPServer,
) -> str:
    _ModelsHandler.mode = "models"
    _ModelsHandler.nonce = "ollama-gate-nonce"
    _ModelsHandler.requests = []
    host, port = cast(tuple[str, int], server.server_address)
    base_url = f"http://{host}:{port}/v1"
    _write_target_lm_studio_default(target, base_url)
    ollama = _invoke(
        target,
        environment,
        base_url,
        operation_ref="setup::models.discover_ollama",
    )
    _check(
        _ModelsHandler.requests == [],
        "Ollama discovery must not contact the configured LM Studio endpoint: "
        f"{_ModelsHandler.requests!r}",
    )
    _check(
        ollama.get("checkpoint_status") == "blocked"
        and ollama.get("error_kind") == "ollama_discovery_unimplemented",
        "ungated Ollama discovery must not verify an LM Studio response: "
        f"{ollama!r}",
    )
    return base_url


def _unbound_loopback_url() -> str:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as reserved:
        reserved.bind(("127.0.0.1", 0))
        host, port = cast(tuple[str, int], reserved.getsockname())
    return f"http://{host}:{port}/v1"


def _assert_unobserved_metadata(target: Path, environment: dict[str, str], base_url: str) -> None:
    _ModelsHandler.nonce = "embedding-metadata-nonce"
    _ModelsHandler.requests = []
    embedding = _invoke(
        target,
        environment,
        base_url,
        operation_ref="setup::models.discover_embeddings",
    )
    _check(
        _ModelsHandler.requests == [("GET", "/v1/models", "embedding-metadata-nonce")],
        "embedding discovery contacts its configured LM Studio endpoint exactly once",
    )
    embedding_candidates = cast(list[dict[str, object]], embedding["discovered_candidates"])
    _check(
        all(
            candidate["metadata"].get("embedding_capable") is None
            and candidate["metadata"].get("dimensions") is None
            and candidate["metadata"].get("local_disk_bytes") is None
            for candidate in cast(list[dict[str, dict[str, object]]], embedding_candidates)
        ),
        "discovery must not stamp the platform expected dimension or embedding capability",
    )
    _ModelsHandler.nonce = "inference-metadata-nonce"
    _ModelsHandler.requests = []
    inference = _invoke(
        target,
        environment,
        base_url,
        operation_ref="setup::models.discover_inference",
        decision_id="inference_model",
    )
    _check(
        _ModelsHandler.requests == [("GET", "/v1/models", "inference-metadata-nonce")],
        "inference discovery contacts its configured LM Studio endpoint exactly once",
    )
    inference_candidates = cast(list[dict[str, object]], inference["discovered_candidates"])
    _check(
        "embedding-only-model" in [candidate["value"] for candidate in inference_candidates],
        "control supplies an embedding-only model to inference discovery",
    )
    _check(
        all(
            candidate["metadata"].get("context_tokens") is None
            and candidate["metadata"].get("structured_output_support") is None
            and candidate["metadata"].get("estimated_memory_bytes") is None
            for candidate in cast(list[dict[str, dict[str, object]]], inference_candidates)
        ),
        "an embedding-only model offered to inference must not claim structured output",
    )


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="model-discovery-loopback-") as raw_root:
        target = Path(raw_root) / "target"
        environment = _target_environment(target)
        _bind_target_embedding_service(target)
        _assert_target_module_resolution(target, environment)
        server = ThreadingHTTPServer(("127.0.0.1", 0), _ModelsHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            empty = _exercise_mode(
                target,
                environment,
                server,
                mode="empty",
                nonce="empty-nonce",
            )
            _check(
                empty.get("checkpoint_status") == "verified"
                and empty.get("discovered_candidates") == []
                and empty.get("error_kind") is None,
                "HTTP 200 with an empty data list is a verified empty candidate set",
            )
            unavailable = _exercise_mode(
                target,
                environment,
                server,
                mode="unavailable",
                nonce="unavailable-nonce",
            )
            _check(
                unavailable.get("checkpoint_status") == "blocked"
                and unavailable.get("error_kind") == "model_discovery_failed"
                and "Restore its service" in str(unavailable.get("repair")),
                "HTTP 503 remains an actionable transport/service failure",
            )
            malformed = _exercise_mode(
                target,
                environment,
                server,
                mode="malformed",
                nonce="malformed-nonce",
            )
            _check(
                malformed.get("checkpoint_status") == "blocked"
                and malformed.get("error_kind") == "model_discovery_invalid_response"
                and "Repair the selected model service response" in str(malformed.get("repair")),
                "malformed HTTP 200 JSON remains an actionable invalid-response result",
            )
            unavailable_endpoint = _invoke(target, environment, _unbound_loopback_url())
            _check(
                unavailable_endpoint.get("checkpoint_status") == "blocked"
                and unavailable_endpoint.get("error_kind") == "model_discovery_failed"
                and unavailable_endpoint.get("retry_safe") is True
                and "Restore its service" in str(unavailable_endpoint.get("repair")),
                "connection refusal remains an actionable retry-safe discovery failure",
            )
            base_url = _exercise_ollama_gate(target, environment, server)
            _assert_unobserved_metadata(target, environment, base_url)
        finally:
            server.shutdown()
            thread.join(timeout=5)
            server.server_close()
    print(f"{_CHECKS} passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
