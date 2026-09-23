#!/usr/bin/env python3
"""Smoke: tunnel supervisor restarts tunnel-client when ingress port changes.

Run:
    .venv/bin/python3 plugins/macos_self_deployment_plugin/tests/blue_green_router/tunnel_supervisor_smoke.py
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[4]


def _write_fake_tunnel_client(path: Path) -> None:
    path.write_text(
        """#!/usr/bin/env python3
import json
import os
import signal
import sys
import time

done = False

def stop(signum, frame):
    global done
    done = True

signal.signal(signal.SIGTERM, stop)
signal.signal(signal.SIGINT, stop)

with open(os.environ["FAKE_TUNNEL_LOG"], "a", encoding="utf-8") as fh:
    fh.write(json.dumps(sys.argv) + "\\n")
    fh.flush()

while not done:
    time.sleep(0.05)
""",
        encoding="utf-8",
    )
    path.chmod(0o755)


def _read_log(path: Path) -> list[list[str]]:
    if not path.exists():
        return []
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _wait_for_log_count(path: Path, count: int) -> list[list[str]]:
    deadline = time.monotonic() + 8.0
    while time.monotonic() < deadline:
        rows = _read_log(path)
        if len(rows) >= count:
            return rows
        time.sleep(0.05)
    raise AssertionError(f"timed out waiting for {count} fake tunnel starts")


def _argv_contains_port(argv: list[str], port: int) -> bool:
    needle = f"http://127.0.0.1:{port}/api/v1/mcp/streamable"
    return any(needle in part for part in argv)


def _argv_enables_local_oauth_harpoon(argv: list[str]) -> bool:
    return "--harpoon.allow-plaintext-http" in argv


def main() -> int:
    with tempfile.TemporaryDirectory() as temp:
        tempdir = Path(temp)
        fake_client = tempdir / "fake-tunnel-client"
        fake_log = tempdir / "fake.log"
        ingress_port_file = tempdir / "example.mcp_ingress.port"
        health_url_file = tempdir / "example.health.url"
        _write_fake_tunnel_client(fake_client)
        ingress_port_file.write_text("41111", encoding="utf-8")

        env = os.environ.copy()
        env["PYTHONPATH"] = (
            f"{REPO_ROOT / 'ananta' / 'src'}:"
            f"{REPO_ROOT / 'plugins' / 'macos_self_deployment_plugin' / 'src'}"
            f":{env.get('PYTHONPATH', '')}"
        )
        env["FAKE_TUNNEL_LOG"] = str(fake_log)
        proc = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "macos_self_deployment_plugin.blue_green_router.tunnel_supervisor",
                "--solet",
                "example",
                "--tunnel-client-path",
                str(fake_client),
                "--tunnel-id",
                "tunnel_test",
                "--control-plane-api-key",
                "file:/tmp/fake-key",
                "--ingress-port-file",
                str(ingress_port_file),
                "--health-url-file",
                str(health_url_file),
                "--poll-interval-seconds",
                "0.1",
            ],
            cwd=REPO_ROOT,
            env=env,
        )
        try:
            rows = _wait_for_log_count(fake_log, 1)
            if not _argv_contains_port(rows[0], 41111):
                raise AssertionError(rows[0])
            if not _argv_enables_local_oauth_harpoon(rows[0]):
                raise AssertionError(rows[0])
            ingress_port_file.write_text("42222", encoding="utf-8")
            rows = _wait_for_log_count(fake_log, 2)
            if not _argv_contains_port(rows[1], 42222):
                raise AssertionError(rows[1])
            if not _argv_enables_local_oauth_harpoon(rows[1]):
                raise AssertionError(rows[1])
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)

    print("PASS: tunnel supervisor restarts on ingress port change")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
