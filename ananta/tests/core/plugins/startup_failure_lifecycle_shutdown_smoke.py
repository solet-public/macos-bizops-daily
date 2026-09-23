#!/usr/bin/env python3
"""Regression pin: a late startup fatal cannot strand a non-daemon worker.

Runs the production ``InitializationManager`` failure seam in a child process.
The child must exit non-zero within the timeout after its synthetic later-step
failure.  Without lifecycle teardown, CPython waits forever for the worker at
interpreter finalization and this smoke times out.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[4]

_CHILD_SOURCE = r'''
import sys
import threading
from types import SimpleNamespace

from ananta.core.orchestration import initialization_manager as module
from ananta.core.orchestration.initialization_manager import InitializationManager
from ananta.core.orchestration.startup_sequence import StartupError


class WorkerPlugin:
    def __init__(self):
        self.stop_event = threading.Event()
        self.started = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=False)
        self.thread.start()
        if not self.started.wait(timeout=1):
            raise RuntimeError("worker did not start")

    def _run(self):
        self.started.set()
        self.stop_event.wait()

    def prepare_for_readiness(self):
        pass

    def start_services(self):
        pass

    async def stop_services(self):
        self.stop_event.set()
        self.thread.join(timeout=1)
        if self.thread.is_alive():
            raise RuntimeError("worker did not stop")

    def is_running(self):
        return self.thread.is_alive()

    def set_active(self, active):
        del active

    def get_readiness_error(self):
        return None

    def set_error(self, error_message):
        del error_message


class FailingRunner:
    def __init__(self, sequence):
        del sequence

    def run(self, orchestrator):
        del orchestrator
        raise StartupError("synthetic later startup failure", step_name="seed_identity_memories")


module.StartupSequenceRunner = FailingRunner
plugin = WorkerPlugin()
orchestrator = SimpleNamespace(plugin_manager=SimpleNamespace(plugins={"worker": plugin}))
try:
    InitializationManager()._run_startup_sequence(orchestrator)
except StartupError:
    if plugin.thread.is_alive():
        raise RuntimeError("startup fatal returned while worker was still alive")
    print("worker stopped before fatal propagation")
    sys.exit(1)
'''


def main() -> int:
    env = os.environ.copy()
    source_root = str(REPO_ROOT / "ananta" / "src")
    env["PYTHONPATH"] = source_root + os.pathsep + env.get("PYTHONPATH", "")
    try:
        result = subprocess.run(
            [sys.executable, "-c", _CHILD_SOURCE],
            capture_output=True,
            env=env,
            text=True,
            timeout=3,
        )
    except subprocess.TimeoutExpired:
        print("FAIL  startup fatal left a non-daemon worker alive (child timed out)")
        return 1

    if result.returncode != 1:
        print(f"FAIL  child exit code was {result.returncode}, expected 1")
        print(result.stderr, end="")
        return 1
    if "worker stopped before fatal propagation" not in result.stdout:
        print("FAIL  child did not prove lifecycle cleanup before fatal propagation")
        print(result.stdout, end="")
        print(result.stderr, end="")
        return 1

    print("PASS  late startup fatal stops a non-daemon lifecycle worker before exit")
    return 0


if __name__ == "__main__":
    sys.exit(main())
