#!/usr/bin/env python3
"""Pin the preflight's skip contract — all three combinations, not just the happy one.

The change under test lets `_harness.preflight()` report a DISCLOSED SKIP (exit 77)
when node is absent, so an adopter who never uses Codex is not failed on a runtime
they never installed. The obvious way that goes wrong is a preflight that skips on
EVERY problem rather than only on node-absence: from a passing run the two are
indistinguishable, and the broken one hides real defects behind a green-ish skip.

So this drives the matrix:

    node present, harness healthy  -> 0   (proceed)
    node ABSENT,  harness healthy  -> 77  (disclosed skip)
    node ABSENT,  harness BROKEN   -> 1   (the real problem still fails LOUDLY)

The third row is the point. Without it, "skips when node is missing" and "skips
whenever anything is wrong" are the same test.

This smoke deliberately does NOT call `preflight()` itself — it drives it in
subprocesses with a controlled PATH, so it runs on a node-less machine (where it
is most needed) instead of skipping alongside the thing it is testing.

Run directly: ``.venv/bin/python3 plugins/github_midwife_plugin/codex_plugin/coordination-hooks/tests/preflight_skip_contract_smoke.py``.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.dont_write_bytecode = True

_TESTS_DIR = Path(__file__).resolve().parent
_SKIP_EXIT_CODE = 77
_MINIMAL_PATH = "/usr/bin:/bin"

_DRIVER = (
    "import sys\n"
    "sys.dont_write_bytecode = True\n"
    "sys.path.insert(0, {tests_dir!r})\n"
    "import _harness\n"
    "_harness.HOOKS_DIR = __import__('pathlib').Path({hooks_dir!r})\n"
    "_harness.preflight()\n"
    "print('PROCEEDED')\n"
)

_passed = 0
_failed: list[str] = []


def _check(condition: bool, label: str, detail: str = "") -> None:
    global _passed
    if condition:
        _passed += 1
        print(f"  PASS  {label}")
    else:
        _failed.append(label)
        print(f"  FAIL  {label}{(' — ' + detail) if detail else ''}")


def _run_preflight(*, node_on_path: bool, hooks_dir: Path) -> subprocess.CompletedProcess[str]:
    """Drive preflight in a subprocess with a controlled PATH and hooks dir."""
    env = {
        "HOME": os.environ.get("HOME", "/tmp"),
        "LANG": "en_US.UTF-8",
        "PYTHONDONTWRITEBYTECODE": "1",
        # Node presence is the ONLY variable between the first two rows.
        "PATH": os.environ.get("PATH", _MINIMAL_PATH) if node_on_path else _MINIMAL_PATH,
    }
    code = _DRIVER.format(tests_dir=str(_TESTS_DIR), hooks_dir=str(hooks_dir))
    return subprocess.run(
        [sys.executable, "-c", code],
        env=env, capture_output=True, text=True, timeout=60, check=False,
    )


def main() -> int:
    print("Codex preflight skip-contract smoke")
    print("=" * 62)

    real_hooks = _TESTS_DIR.parent / "hooks"
    node_available = shutil.which("node") is not None

    with tempfile.TemporaryDirectory() as tmp:
        missing_hooks = Path(tmp) / "no-such-hooks-dir"

        # Row 1 — node present, harness healthy: proceed.
        if node_available:
            res = _run_preflight(node_on_path=True, hooks_dir=real_hooks)
            _check(res.returncode == 0 and "PROCEEDED" in res.stdout,
                   "node present + healthy harness -> proceeds (exit 0)",
                   f"exit={res.returncode} out={res.stdout.strip()[:120]}")
        else:
            # Honest about not having run it, rather than quietly claiming the row.
            print("  ....  node present row NOT RUN — no node on this machine to test with")

        # Row 2 — node absent, harness healthy: disclosed skip.
        res = _run_preflight(node_on_path=False, hooks_dir=real_hooks)
        _check(res.returncode == _SKIP_EXIT_CODE,
               f"node absent + healthy harness -> disclosed skip (exit {_SKIP_EXIT_CODE})",
               f"exit={res.returncode} out={(res.stdout + res.stderr).strip()[:160]}")
        _check("SKIP" in res.stdout and "node" in res.stdout,
               "the skip SAYS why it skipped", res.stdout.strip()[:160])
        _check("PROCEEDED" not in res.stdout,
               "a skip does not fall through into the assertions it skipped")

        # Row 3 — THE ONE THAT MATTERS. node absent AND a real problem: fail loudly.
        res = _run_preflight(node_on_path=False, hooks_dir=missing_hooks)
        _check(res.returncode == 1,
               "node absent + BROKEN harness -> fails LOUDLY (exit 1), never masked by the skip",
               f"exit={res.returncode} out={(res.stdout + res.stderr).strip()[:160]}")
        _check("FAIL" in res.stdout and "hooks directory not found" in res.stdout,
               "the real problem is named, not swallowed by the skip path",
               res.stdout.strip()[:160])

        # Row 3b — same real problem WITH node present, so the failure is
        # attributable to the problem rather than to the environment.
        if node_available:
            res = _run_preflight(node_on_path=True, hooks_dir=missing_hooks)
            _check(res.returncode == 1,
                   "node present + BROKEN harness -> still fails loudly (exit 1)",
                   f"exit={res.returncode}")

    print("=" * 62)
    if _failed:
        print(f"FAIL: {_passed} passed, {len(_failed)} failed")
        return 1
    print(f"PASS: {_passed} preflight skip-contract checks")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
