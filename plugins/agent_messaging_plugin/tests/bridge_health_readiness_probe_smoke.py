#!/usr/bin/env python3
"""Bridge ``/api/v1/bridge/health`` readiness-probe gating smoke.

Verifies the Task #11 platform-side fix shipped 2026-06-12 per
``workbench/2026-06-12_aws_swap_smoke_run_report.md`` §3 Bug 2:

  1. With NO ``readiness_probe`` (default / local-dev mode) the health
     endpoint returns 200 ``healthy`` unconditionally — backward-
     compatible with every existing caller that hits this route.

UPDATED 2026-08-15 (lane-ac-payload-guard, INCIDENT.md D5): the 200 body is
now a SUPERSET — ``{"status": ..., "action_path": {...}}`` — because a probe
that could not see a total action-queue freeze reported ``healthy`` for 3h20m.
The assertions below therefore pin the load-bearing fields (``status_code``
and ``status``) plus the presence of the new ``action_path`` verdict, rather
than exact dict equality, which would fail on any additive change. That is a
strengthening, not a loosening: this smoke now also proves the liveness block
is actually served, which nothing previously checked.
  2. With a ``readiness_probe`` callable that returns ``False`` (the
     "streamable uvicorn not yet bound" window during a cloud cold
     boot), the endpoint returns 503 ``starting``.
  3. After the same callable flips to ``True``, the next request
     returns 200 ``healthy``. No restart, no router rebuild.

Test fixture uses stub collaborators because the health route does not
reach into any collaborator — it only consults the optional
``readiness_probe`` closure. The other sub-route registrations are
exercised in their own smokes (peer_registry_*, oauth_*, etc.).

Run:

    SOLET_NAME=<name> .venv/bin/python3 \
        plugins/agent_messaging_plugin/tests/bridge_health_readiness_probe_smoke.py

Exits 0 on success, 1 on first failure with a labeled message.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))
sys.path.insert(
    0,
    str(REPO_ROOT / "plugins" / "agent_messaging_plugin" / "src"),
)

from agent_messaging_plugin.http_routes import register_routes  # noqa: E402


def _fail(label: str, detail: str) -> None:
    print(f"FAIL: {label}: {detail}", file=sys.stderr)
    sys.exit(1)


def _ok(label: str) -> None:
    print(f"  OK: {label}")


def _build_app_with(readiness_probe: Any) -> FastAPI:
    """Build a FastAPI app + register routes with stub collaborators.

    The health route is closure-only over ``readiness_probe``; every
    other handler is registered but unused in this smoke.
    """
    app = FastAPI()
    stub = object()

    register_routes(
        app,
        bridge_manager=stub,  # type: ignore[arg-type]
        peer_registry=stub,  # type: ignore[arg-type]
        platform_surface=stub,  # type: ignore[arg-type]
        agent_messaging_service=stub,
        config={"long_poll_timeout_seconds": 25},
        readiness_probe=readiness_probe,
    )
    return app


def case_1_no_probe_returns_200() -> None:
    app = _build_app_with(readiness_probe=None)
    with TestClient(app) as client:
        response = client.get("/api/v1/bridge/health")
    if response.status_code != 200:
        _fail(
            "case 1 (no probe → 200)",
            f"expected 200, got {response.status_code}",
        )
    body = response.json()
    if body.get("status") != "healthy":
        _fail(
            "case 1 (no probe → 200 body)",
            f"expected status 'healthy', got {body!r}",
        )
    # D5: the liveness block must actually be served. Asserting its presence is
    # what stops this endpoint from silently regressing to the shape that
    # reported healthy through a 3h20m freeze.
    action_path = body.get("action_path")
    if not isinstance(action_path, dict) or "action_path_stalled" not in action_path:
        _fail(
            "case 1 (action_path liveness block)",
            f"expected an action_path block carrying action_path_stalled, got {body!r}",
        )
    _ok("case 1 — no readiness_probe → 200 healthy + action_path liveness block")


def case_2_probe_false_returns_503() -> None:
    app = _build_app_with(readiness_probe=lambda: False)
    with TestClient(app) as client:
        response = client.get("/api/v1/bridge/health")
    if response.status_code != 503:
        _fail(
            "case 2 (probe False → 503)",
            f"expected 503, got {response.status_code}",
        )
    if response.json() != {"status": "starting"}:
        _fail(
            "case 2 (probe False → 503 body)",
            f"expected {{'status': 'starting'}}, got {response.json()!r}",
        )
    _ok("case 2 — readiness_probe()==False → 503 starting (cloud-boot window)")


def case_3_probe_flip_returns_200_after_ready() -> None:
    state = {"ready": False}
    app = _build_app_with(readiness_probe=lambda: state["ready"])
    with TestClient(app) as client:
        first = client.get("/api/v1/bridge/health")
        if first.status_code != 503:
            _fail(
                "case 3 (pre-flip → 503)",
                f"expected 503 before flip, got {first.status_code}",
            )
        state["ready"] = True
        second = client.get("/api/v1/bridge/health")
    if second.status_code != 200:
        _fail(
            "case 3 (post-flip → 200)",
            f"expected 200 after flip, got {second.status_code}",
        )
    if second.json().get("status") != "healthy":
        _fail(
            "case 3 (post-flip body)",
            f"expected status 'healthy', got {second.json()!r}",
        )
    _ok("case 3 — probe flip False→True → 503→200 without router rebuild")


def main() -> int:
    print("=== Bridge /api/v1/bridge/health readiness-probe smoke ===")
    case_1_no_probe_returns_200()
    case_2_probe_false_returns_503()
    case_3_probe_flip_returns_200_after_ready()
    print("=== ALL SMOKE CASES PASSED ===")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
