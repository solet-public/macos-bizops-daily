"""Torn Core AI compiled-cache recovery (iss_453f69d5, iss_2eefe356).

A scratch HOME and cache root hold a fake Core AI cache; a fake child process
stands in for the real canary and behaves as Core AI was measured to: it
compiles an absent entry, reuses an intact one, dies by SIGTRAP on a truncated
``bnns.ir`` and reports invalid output on a corrupted one. No real host cache is
read or purged.

``HostileImportPath`` spawns the real canary module through the real default
child command, with a stdlib-shadowing directory on the parent's ``sys.path``
(iss_831383f5). Its asset root does not exist, so the child stops at asset
verification and never loads Core AI.

Run::

    .venv/bin/python3 plugins/coreai_embeddings_plugin/tests/cache_canary_smoke.py
"""

import io
import json
import os
import platform
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "ananta" / "src"))
sys.path.insert(0, str(ROOT / "plugins" / "coreai_embeddings_plugin" / "src"))

from coreai_embeddings_plugin import cache_canary  # noqa: E402
from coreai_embeddings_plugin.cache_canary import CacheCanary, model_cache_key  # noqa: E402
from coreai_embeddings_plugin.contracts import EmbeddingError, ErrorCode  # noqa: E402
from coreai_embeddings_plugin.runtime import EmbeddingRuntime  # noqa: E402

GOOD = b"compiled-specialization-" * 64
OTHER_MODEL = "0" * 64
FAKE_CHILD = r'''
import json, os, signal, sys, time
from pathlib import Path

GOOD = b"compiled-specialization-" * 64
cache, key, mode = Path(os.environ["FAKE_COREAI_CACHE"]), os.environ["FAKE_COREAI_KEY"], os.environ["FAKE_COREAI_MODE"]
spec = "GPU-SPEC" if sys.argv[2] == "gpu" else "CPU-SPEC"
entry = cache / "26A428" / "org.python.python" / key / spec / "model.aimodelx" / "BNNS" / "bnns.ir"
log = Path(os.environ["FAKE_COREAI_LOG"])

def record(event):
    with log.open("a") as handle:
        handle.write(f"{sys.argv[2]}:{event}\n")

if mode == "persistent":
    record("trap"); os.kill(os.getpid(), signal.SIGTRAP)
if mode == "asset_missing":
    record("asset_missing")
    print(json.dumps({"code": "coreai_embeddings.asset_missing", "message": "asset absent"})); sys.exit(2)
if mode == "hang":
    record("hang"); time.sleep(30)
if mode.startswith("signal:"):
    record(mode); os.kill(os.getpid(), int(mode.split(":", 1)[1])); time.sleep(5)
if not entry.exists():
    time.sleep(float(os.environ.get("FAKE_COREAI_COMPILE_SECONDS", "0")))
    staging = entry.with_name(f"bnns.ir.{os.getpid()}.tmp")
    staging.parent.mkdir(parents=True, exist_ok=True)
    staging.write_bytes(GOOD); os.replace(staging, entry); record("compile"); sys.exit(0)
data = entry.read_bytes()
if data == GOOD:
    record("reuse"); sys.exit(0)
if len(data) < len(GOOD):
    record("trap"); os.kill(os.getpid(), signal.SIGTRAP)
record("invalid")
print(json.dumps({"code": "coreai_embeddings.invalid_output", "message": "Embedding is nonfinite or not normalized"}))
sys.exit(3)
'''


class _ScratchCache(unittest.TestCase):
    """Each test gets its own scratch HOME, cache root and fake child."""

    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        scratch = Path(self.directory.name)
        self.cache = scratch / "Library" / "Caches" / "coreai-cache"
        self.log = scratch / "child.log"
        self.child = scratch / "fake_child.py"
        self.child.write_text(FAKE_CHILD)
        self.environment = patch.dict(os.environ, {
            "HOME": str(scratch), "FAKE_COREAI_CACHE": str(self.cache),
            "FAKE_COREAI_KEY": model_cache_key(), "FAKE_COREAI_LOG": str(self.log),
            "FAKE_COREAI_MODE": "normal",
        })
        self.environment.start()
        self.other_model = self._entry("CPU-SPEC", model=OTHER_MODEL)
        self.other_model.parent.mkdir(parents=True)
        self.other_model.write_bytes(b"another model's cache")

    def tearDown(self) -> None:
        self.environment.stop()
        self.directory.cleanup()

    def _entry(self, spec: str, model: str | None = None) -> Path:
        key = model or model_cache_key()
        return self.cache / "26A428" / "org.python.python" / key / spec / "model.aimodelx" / "BNNS" / "bnns.ir"

    def _seed(self, spec: str, content: bytes) -> Path:
        entry = self._entry(spec)
        entry.parent.mkdir(parents=True, exist_ok=True)
        entry.write_bytes(content)
        return entry

    def _canary(self, preference: str = "cpu", timeout: float = 20.0) -> CacheCanary:
        return CacheCanary(
            Path("/fixture/assets"), preference, cache_root=self.cache,
            child_command=lambda: (sys.executable, str(self.child)), timeout_seconds=timeout,
        )

    def _events(self) -> list[str]:
        return self.log.read_text().splitlines() if self.log.exists() else []



class CacheScenario(_ScratchCache):
    """Healthy reuse, torn-cache recovery, and loud non-cache failures."""

    def test_default_cache_root_follows_home(self) -> None:
        self.assertEqual(cache_canary.default_cache_root(), self.cache)

    def test_healthy_cache_is_not_purged(self) -> None:
        entry = self._seed("CPU-SPEC", GOOD)
        report = self._canary().ensure_healthy()
        self.assertEqual(report.outcome, "healthy")
        self.assertEqual(report.purged, ())
        self.assertEqual(entry.read_bytes(), GOOD)
        self.assertEqual(self._events(), ["cpu:reuse"])

    def test_truncated_cache_signal_death_recovers(self) -> None:
        self._seed("CPU-SPEC", GOOD[: len(GOOD) // 2])
        sibling = self._seed("GPU-SPEC", GOOD)
        report = self._canary().ensure_healthy()
        self.assertEqual(report.outcome, "recovered")
        self.assertEqual(report.first_attempt, "canary killed by signal 5 (SIGTRAP)")
        self.assertEqual(len(report.purged), 1)
        self.assertTrue(report.purged[0].endswith(model_cache_key()))
        self.assertFalse(sibling.exists(), "every specialization of the pinned model is purged")
        self.assertTrue(self.other_model.exists(), "another model's cache is never purged")
        self.assertEqual(self._entry("CPU-SPEC").read_bytes(), GOOD)
        self.assertEqual(self._events(), ["cpu:trap", "cpu:trap", "cpu:compile"])

    def test_corrupted_cache_invalid_output_recovers(self) -> None:
        self._seed("CPU-SPEC", bytes(len(GOOD)))
        report = self._canary().ensure_healthy()
        self.assertEqual(report.outcome, "recovered")
        self.assertIn("invalid output", report.first_attempt)
        self.assertEqual(self._events(), ["cpu:invalid", "cpu:invalid", "cpu:compile"])

    def test_gpu_entry_is_covered(self) -> None:
        self._seed("GPU-SPEC", GOOD[:10])
        report = self._canary("gpu").ensure_healthy()
        self.assertEqual(report.outcome, "recovered")
        self.assertEqual(self._events(), ["gpu:trap", "gpu:trap", "gpu:compile"])
        self.assertEqual(self._entry("GPU-SPEC").read_bytes(), GOOD)

    def test_persistent_failure_fails_loudly_after_exactly_one_rebuild(self) -> None:
        self._seed("CPU-SPEC", GOOD)
        os.environ["FAKE_COREAI_MODE"] = "persistent"
        with self.assertLogs("coreai_embeddings_plugin.cache_canary", "ERROR") as logs, \
                self.assertRaises(EmbeddingError) as caught:
            self._canary().ensure_healthy()
        self.assertEqual(caught.exception.code, ErrorCode.UNAVAILABLE)
        self.assertIn("invalid after purge and recompile: canary killed by signal 5 (SIGTRAP)", str(caught.exception))
        self.assertEqual(self._events(), ["cpu:trap", "cpu:trap", "cpu:trap"], "one recheck, one purge, one rebuild")
        self.assertFalse(self._entry("CPU-SPEC").exists())
        self.assertTrue(any("after purge and recompile" in line for line in logs.output))

    def test_unrelated_failure_is_loud_and_never_purges(self) -> None:
        entry = self._seed("CPU-SPEC", GOOD)
        os.environ["FAKE_COREAI_MODE"] = "asset_missing"
        with self.assertRaises(EmbeddingError) as caught:
            self._canary().ensure_healthy()
        self.assertEqual(caught.exception.code, ErrorCode.ASSET_MISSING)
        self.assertTrue(entry.exists())
        self.assertEqual(self._events(), ["cpu:asset_missing"])

    def test_hung_child_is_loud_and_never_purges(self) -> None:
        entry = self._seed("CPU-SPEC", GOOD)
        os.environ["FAKE_COREAI_MODE"] = "hang"
        with self.assertRaises(EmbeddingError) as caught:
            self._canary(timeout=1.0).ensure_healthy()
        self.assertIn("exceeded 1 seconds", str(caught.exception))
        self.assertTrue(entry.exists())

    def test_absent_cache_purges_nothing(self) -> None:
        self.assertEqual(self._canary().purge(), [])

    def test_real_child_entry_reports_its_error_without_purging(self) -> None:
        entry = self._seed("CPU-SPEC", GOOD)
        missing = Path(self.directory.name) / "no-assets"
        with self.assertRaises(EmbeddingError) as caught:
            CacheCanary(missing, "cpu", cache_root=self.cache).ensure_healthy()
        self.assertIn(caught.exception.code, (ErrorCode.ASSET_MISSING, ErrorCode.UNAVAILABLE))
        self.assertNotIn("exited", str(caught.exception), "the real child must report a typed error")
        self.assertTrue(entry.exists())


class SignalClassification(_ScratchCache):
    """F3: only crash signals implicate the cache; a killed child never purges."""

    def test_crash_signals_are_cache_suspect(self) -> None:
        for number in sorted(cache_canary.CRASH_SIGNALS):
            with self.subTest(signal=signal.Signals(number).name):
                self.assertTrue(cache_canary._classify(-number, "", "").cache_suspect)
        for number in (signal.SIGTERM, signal.SIGKILL, signal.SIGINT, signal.SIGHUP, signal.SIGUSR1):
            with self.subTest(signal=signal.Signals(number).name):
                attempt = cache_canary._classify(-number, "", "")
                self.assertFalse(attempt.cache_suspect)
                self.assertIn("not a cache fault", attempt.reason)

    def test_segv_child_recovers_through_one_purge(self) -> None:
        self._seed("CPU-SPEC", GOOD)
        os.environ["FAKE_COREAI_MODE"] = f"signal:{int(signal.SIGSEGV)}"
        with self.assertRaises(EmbeddingError) as caught:
            self._canary().ensure_healthy()
        self.assertIn("invalid after purge and recompile", str(caught.exception))
        self.assertEqual(len(self._events()), 3, "first attempt, locked recheck, one rebuild")

    def test_killed_child_fails_loudly_and_never_purges(self) -> None:
        for number in (signal.SIGKILL, signal.SIGTERM, signal.SIGINT):
            with self.subTest(signal=signal.Signals(number).name):
                entry = self._seed("CPU-SPEC", GOOD)
                self.log.unlink(missing_ok=True)
                os.environ["FAKE_COREAI_MODE"] = f"signal:{int(number)}"
                with self.assertRaises(EmbeddingError) as caught:
                    self._canary().ensure_healthy()
                self.assertIn("not a cache fault", str(caught.exception))
                self.assertEqual(entry.read_bytes(), GOOD)
                self.assertEqual(len(self._events()), 1)


class SymlinkSafety(_ScratchCache):
    """F2: the purge never follows a symlink out of the cache root, and says so loudly."""

    def setUp(self) -> None:
        super().setUp()
        self.outside = Path(self.directory.name) / "OUTSIDE"
        self.precious = self.outside / "bundle" / model_cache_key() / "precious.txt"
        self.precious.parent.mkdir(parents=True)
        self.precious.write_text("must survive")

    def test_symlinked_build_directory_is_refused(self) -> None:
        torn = self._seed("CPU-SPEC", GOOD[:5])
        (self.cache / "27B999").symlink_to(self.outside, target_is_directory=True)
        with self.assertRaises(EmbeddingError) as caught:
            self._canary().purge()
        self.assertIn("symlink", str(caught.exception))
        self.assertTrue(self.precious.exists(), "nothing outside the root is deleted")
        self.assertTrue(torn.exists(), "a refusal deletes nothing at all")

    def test_symlinked_model_entry_is_refused(self) -> None:
        bundle = self.cache / "26A428" / "org.python.python"
        bundle.mkdir(parents=True, exist_ok=True)
        (bundle / model_cache_key()).symlink_to(self.precious.parent, target_is_directory=True)
        with self.assertRaises(EmbeddingError) as caught:
            self._canary().purge()
        self.assertIn("symlink", str(caught.exception))
        self.assertTrue(self.precious.exists())

    def test_torn_cache_with_escaping_symlink_fails_typed_during_recovery(self) -> None:
        self._seed("CPU-SPEC", GOOD[:5])
        (self.cache / "27B999").symlink_to(self.outside, target_is_directory=True)
        with self.assertRaises(EmbeddingError):
            self._canary().ensure_healthy()
        self.assertTrue(self.precious.exists())


CONCURRENT_PREPARE = r'''
import json, site, sys
from pathlib import Path
stdlib = list(sys.path)
site.main()
sys.path[:] = stdlib + [entry for entry in json.loads(sys.argv[1]) if entry not in stdlib]
from coreai_embeddings_plugin.cache_canary import CacheCanary
from coreai_embeddings_plugin.contracts import EmbeddingError
canary = CacheCanary(Path("/fixture/assets"), "cpu", cache_root=Path(sys.argv[2]),
                     child_command=lambda: (sys.executable, sys.argv[3]), timeout_seconds=20)
try:
    print(json.dumps(canary.ensure_healthy().as_dict()))
except EmbeddingError as exc:
    print(json.dumps({"outcome": "error", "message": str(exc), "purged": []}))
'''


class ConcurrentPrepare(_ScratchCache):
    """F1: solets preparing together on one torn cache purge it once and all end ready."""

    def test_two_concurrent_prepares_purge_once_and_both_end_ready(self) -> None:
        script = Path(self.directory.name) / "prepare.py"
        script.write_text(CONCURRENT_PREPARE)
        os.environ["FAKE_COREAI_COMPILE_SECONDS"] = "0.6"
        for offset in (0.0, 0.3, 0.5):
            with self.subTest(offset=offset):
                self._seed("CPU-SPEC", GOOD[: len(GOOD) // 2])
                self.log.unlink(missing_ok=True)
                argv = [sys.executable, "-I", "-S", str(script), json.dumps(sys.path), str(self.cache), str(self.child)]
                first = subprocess.Popen(argv, stdout=subprocess.PIPE, text=True)
                time.sleep(offset)
                second = subprocess.Popen(argv, stdout=subprocess.PIPE, text=True)
                reports = [json.loads(process.communicate(timeout=60)[0]) for process in (first, second)]
                outcomes = sorted(str(report["outcome"]) for report in reports)
                self.assertNotIn("error", outcomes, reports)
                self.assertEqual(sum(len(report["purged"]) for report in reports), 1, reports)
                self.assertEqual(self._entry("CPU-SPEC").read_bytes(), GOOD)
                self.assertEqual(self._events().count("cpu:compile"), 1, self._events())


# Mirrors ananta/src/ananta/types: importing it needs enum, and enum imports types.
HOSTILE_TYPES = "from enum import StrEnum\n"
HOSTILE_PLATFORM = "raise ImportError('hostile platform package shadowed the stdlib')\n"


class HostileImportPath(_ScratchCache):
    """iss_831383f5: a stdlib-shadowing sys.path entry never reaches the child ahead of the stdlib."""

    def setUp(self) -> None:
        super().setUp()
        self.hostile = Path(self.directory.name) / "shadows_stdlib"
        (self.hostile / "platform").mkdir(parents=True)
        (self.hostile / "types.py").write_text(HOSTILE_TYPES)
        (self.hostile / "platform" / "__init__.py").write_text(HOSTILE_PLATFORM)
        self.saved_path = list(sys.path)

    def tearDown(self) -> None:
        sys.path[:] = self.saved_path
        super().tearDown()

    def _expected_report(self) -> ErrorCode:
        on_macos_27 = platform.system() == "Darwin" and platform.mac_ver()[0].split(".")[0] == "27"
        return ErrorCode.ASSET_MISSING if on_macos_27 else ErrorCode.UNAVAILABLE

    def _assert_child_reached_its_own_report(self) -> None:
        canary = CacheCanary(
            Path(self.directory.name) / "absent-assets", "cpu", cache_root=self.cache, timeout_seconds=40,
        )
        with self.assertRaises(EmbeddingError) as caught:
            canary.ensure_healthy()
        message = str(caught.exception)
        self.assertNotIn("canary exited", message)
        self.assertEqual(caught.exception.code, self._expected_report(), message)
        self.assertEqual(self._events(), [])

    def test_control_shadowing_entry_after_the_stdlib(self) -> None:
        """Control: r49 passes this too; the live break needs the entry ahead of the stdlib."""
        sys.path.append(str(self.hostile))
        self._assert_child_reached_its_own_report()

    def test_shadowing_entry_at_index_zero_as_lizard_inserts_it(self) -> None:
        """The live shape: red on r49 (enum's circular import) and on an exact-order child (platform)."""
        sys.path.insert(0, str(self.hostile))
        self._assert_child_reached_its_own_report()

    def test_parent_order_is_kept_after_the_stdlib(self) -> None:
        first, second = Path(self.directory.name) / "first", Path(self.directory.name) / "second"
        for directory, marker in ((first, "first"), (second, "second")):
            (directory / "canary_order_probe").mkdir(parents=True)
            (directory / "canary_order_probe" / "__init__.py").write_text(f"WHO = {marker!r}\n")
        sys.path[:0] = [str(first), str(second)]
        command = cache_canary.default_child_command()
        self.assertEqual(command[:4], (sys.executable, "-I", "-S", "-c"))
        probe = subprocess.run(
            [*command[:6], "json.tool"], input='{"ok": 1}', capture_output=True, text=True, check=True,
        )
        self.assertEqual(json.loads(probe.stdout), {"ok": 1})
        inspect = command[4].replace(
            "runpy.run_module(sys.argv.pop(1), run_name=\"__main__\", alter_sys=True)",
            "import canary_order_probe, types; print(canary_order_probe.WHO, types.__file__)",
        )
        self.assertNotEqual(inspect, command[4])
        completed = subprocess.run(
            [*command[:4], inspect, command[5], "unused"], capture_output=True, text=True, check=True,
        )
        who, types_file = completed.stdout.split()
        self.assertEqual(who, "first")
        self.assertTrue(Path(types_file).is_relative_to(Path(json.__file__).parent.parent), types_file)


class RuntimeWiring(unittest.TestCase):
    """prepare() runs the canary between asset verification and in-process load."""

    def test_prepare_runs_canary_before_load_and_child_does_not(self) -> None:
        calls: list[str] = []
        runtime = EmbeddingRuntime(Path("/fixture"), "gpu")
        self.assertEqual((runtime.canary.asset_root, runtime.canary.preference), (Path("/fixture"), "gpu"))
        report = cache_canary.CanaryReport("healthy", 0.4, "ok")
        with patch.object(EmbeddingRuntime, "_verified_asset", lambda _self: calls.append("verify")), \
                patch.object(EmbeddingRuntime, "_load", lambda _self, _asset: calls.append("load")), \
                patch.object(CacheCanary, "ensure_healthy", lambda _self: calls.append("canary") or report):
            try:
                runtime.prepare()
                self.assertEqual(calls, ["verify", "canary", "load"])
                self.assertEqual(runtime.diagnostics()["cache_canary"], report.as_dict())
                calls.clear()
                runtime.prepare_in_process()
                self.assertEqual(calls, ["verify", "load"])
            finally:
                runtime.close()

    def test_failed_canary_blocks_in_process_load(self) -> None:
        loads: list[str] = []
        runtime = EmbeddingRuntime(Path("/fixture"), "cpu")

        def fail(_self: CacheCanary) -> cache_canary.CanaryReport:
            raise EmbeddingError(ErrorCode.UNAVAILABLE, "Core AI compiled cache invalid after purge and recompile: x")

        with patch.object(EmbeddingRuntime, "_verified_asset", lambda _self: None), \
                patch.object(EmbeddingRuntime, "_load", lambda _self, _asset: loads.append("load")), \
                patch.object(CacheCanary, "ensure_healthy", fail):
            try:
                with self.assertRaises(EmbeddingError):
                    runtime.prepare()
                self.assertEqual(loads, [])
            finally:
                runtime.close()

    def test_child_entry_exit_codes(self) -> None:
        for code, expected in ((ErrorCode.INVALID_OUTPUT, 3), (ErrorCode.ASSET_MISSING, 2)):
            def fail(_self: EmbeddingRuntime, code: ErrorCode = code) -> None:
                raise EmbeddingError(code, "reported")

            output = io.StringIO()
            with self.subTest(code=code), patch.object(EmbeddingRuntime, "prepare_in_process", fail), \
                    redirect_stdout(output):
                self.assertEqual(cache_canary.main(["/fixture", "cpu"]), expected)
                self.assertEqual(json.loads(output.getvalue())["code"], code.value)
        with patch.object(EmbeddingRuntime, "prepare_in_process", lambda _self: None):
            self.assertEqual(cache_canary.main(["/fixture", "cpu"]), 0)


if __name__ == "__main__":
    unittest.main()
