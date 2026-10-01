#!/usr/bin/env python3
"""Regression smoke for iss_a457a147: one Core AI native call over 120 s left embeddings dead until a reload.

Measured cause: the runtime ran a whole ``generate`` batch as ONE native operation under a 120-second bound. On CPU one
embedding takes 0.13 s (256-token bucket) to 6.4 s (2048), so the ledger drain's sixteen full chunks (about 100 s) passed
the bound under any host load, the runtime poisoned itself, and the plugin then stayed unavailable for every consumer
until ``reload_plugin_config``.

The fake native model sleeps a fixed time per embedding. The runtime's literal 120 s is scaled to one second by capping
``Future.result``, so base and fix run the same code path. Slicing: a batch longer than the bound completes, because each
input is its own operation, and an over-long input anywhere in a batch is refused before any embedding. Recovery: a call
that really hangs, in ``generate_embeddings`` or in a token count, is reported and the plugin prepares a new runtime once,
logs it, and serves the next call; a preparation that itself times out leaves the plugin unavailable and no later call
prepares again. Fail loud: a native failure, an unavailable error and an invalid output each leave the plugin unavailable
with no preparation. No ``_call`` is made from the worker. Offline; no Core AI runtime or model asset.

Run::

    .venv/bin/python3 plugins/coreai_embeddings_plugin/tests/timeout_recovery_smoke.py
"""

from __future__ import annotations

import asyncio
import sys
import threading
import time
import unittest
from collections.abc import Callable
from concurrent.futures import Future
from pathlib import Path
from types import SimpleNamespace
from typing import Any, TypeVar
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "ananta" / "src"))
sys.path.insert(0, str(ROOT / "plugins" / "coreai_embeddings_plugin" / "src"))

from coreai_embeddings_plugin import runtime as runtime_module  # noqa: E402
from coreai_embeddings_plugin.contracts import DIMENSION, EmbeddingError, ErrorCode  # noqa: E402
from coreai_embeddings_plugin.plugin import CoreAIEmbeddingsPlugin  # noqa: E402
from coreai_embeddings_plugin.runtime import EmbeddingRuntime  # noqa: E402
from coreai_embeddings_plugin.tokenization import EncodedInput  # noqa: E402

T = TypeVar("T")
BOUND_SECONDS = 1.0
EMBED_SECONDS = 0.3
HANG_SECONDS = 2.5
WORKER_PREFIX = "coreai-embedding"
TIMEOUT_CODE = "coreai_embeddings.operation_timeout"

_real_result = Future.result


def _scaled_result(self: Future[T], timeout: float | None = None) -> T:
    """The runtime's 120-second wait, scaled to ``BOUND_SECONDS``; an unbounded wait stays unbounded."""
    return _real_result(self, None if timeout is None else BOUND_SECONDS)


class FakeTokenizer:
    """The word count stands in for the token count; the first character tells the fakes what to do.

    ``Long...`` is over the ceiling; ``h...`` hangs a token count while ``count_hangs_left`` lasts.
    """

    count_hangs_left = 0

    def __init__(self, _path: Path) -> None:
        pass

    def encode(self, text: str) -> EncodedInput:
        if text.startswith("Long"):
            raise EmbeddingError(ErrorCode.INPUT_TOO_LONG, "Input is over the fixture ceiling")
        return EncodedInput(256, [ord(text[0])], [1])

    def count(self, text: str) -> int:
        if text.startswith("h") and FakeTokenizer.count_hangs_left > 0:
            FakeTokenizer.count_hangs_left -= 1
            time.sleep(HANG_SECONDS)
        return len(text.split())


class FakeNative:
    """One slow embedding per call, and the faults a test arms.

    ``hangs_left`` embeddings of ``h...`` and ``load_hangs_left`` loads outlast the bound; ``raises`` is raised by
    every embedding; ``embeds`` counts the embeddings started.
    """

    hangs_left = 0
    load_hangs_left = 0
    raises: Exception | None = None
    embeds = 0

    def __init__(self) -> None:
        self.compute_preference = "cpu"
        self.observed_compute_unit = "cpu"
        self.compute_evidence: set[str] = set()
        self.fallback_reason: str | None = None

    async def load(self, _path: Path, _preference: str) -> None:
        if FakeNative.load_hangs_left > 0:
            FakeNative.load_hangs_left -= 1
            time.sleep(HANG_SECONDS)

    async def embed(self, encoded: EncodedInput) -> list[float]:
        FakeNative.embeds += 1
        if FakeNative.raises is not None:
            raise FakeNative.raises
        if encoded.input_ids[0] == ord("h") and FakeNative.hangs_left > 0:
            FakeNative.hangs_left -= 1
            time.sleep(HANG_SECONDS)
        else:
            time.sleep(EMBED_SECONDS)
        return [1.0] + [0.0] * (DIMENSION - 1)


class FixtureRuntime(EmbeddingRuntime):
    """The real runtime over the fakes, preparing in process; records any ``_call`` made from the worker thread."""

    prepared = 0
    nested = 0

    def prepare(self) -> None:
        FixtureRuntime.prepared += 1
        self.prepare_in_process()

    def _verified_asset(self) -> Any:
        return SimpleNamespace(tokenizer_path=Path("/fixture/tokenizer.json"), model_path=Path("/fixture/model.aimodel"))

    def _call(self, operation: Callable[[], T]) -> T:
        if threading.current_thread().name.startswith(WORKER_PREFIX):
            FixtureRuntime.nested += 1
        return super()._call(operation)


def _wait_for_abandoned_workers() -> None:
    """A timed-out call still holds its worker; let it return so no later test sees its late embeddings."""
    deadline = time.monotonic() + 10 * HANG_SECONDS
    while time.monotonic() < deadline and any(t.name.startswith(WORKER_PREFIX) for t in threading.enumerate()):
        time.sleep(0.05)


class TimeoutRecoveryTests(unittest.TestCase):
    """The plugin over the real runtime, a scaled bound and a fake native model."""

    def setUp(self) -> None:
        FakeNative.hangs_left = 0
        FakeNative.load_hangs_left = 0
        FakeNative.raises = None
        FakeTokenizer.count_hangs_left = 0
        FixtureRuntime.prepared = 0
        FixtureRuntime.nested = 0
        for patcher in (
            patch.object(Future, "result", _scaled_result),
            patch.object(runtime_module, "NativeModel", FakeNative),
            patch.object(runtime_module, "NomicTokenizer", FakeTokenizer),
            patch("coreai_embeddings_plugin.plugin.EmbeddingRuntime", FixtureRuntime),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.addCleanup(_wait_for_abandoned_workers)
        self.plugin = CoreAIEmbeddingsPlugin({"asset_root": "/fixture"})
        self.plugin.prepare_for_readiness()
        self.addCleanup(lambda: asyncio.run(self.plugin.cleanup()))
        self.assertTrue(self.plugin.is_ready())
        FakeNative.embeds = 0  # the readiness probe is not a caller's embedding

    def test_a_batch_longer_than_the_bound_completes_one_input_at_a_time(self) -> None:
        inputs = [f"batch input {index}" for index in range(8)]
        started = time.monotonic()
        outcome = self.plugin.generate_embeddings(inputs)
        elapsed = time.monotonic() - started
        self.assertGreater(elapsed, BOUND_SECONDS, "the batch must outlast the bound or this proves nothing")
        self.assertEqual(outcome["action_status"], "completed", outcome["error"])
        self.assertEqual(len(outcome["data"]["result"]["embeddings"]), len(inputs))
        self.assertTrue(self.plugin.is_ready())
        self.assertEqual(FixtureRuntime.prepared, 1)

    def test_an_over_long_last_input_is_refused_before_any_embedding(self) -> None:
        outcome = self.plugin.generate_embeddings(["first input", "second input", "Long last input"])
        self.assertEqual(outcome["action_status"], "error")
        self.assertEqual(outcome["error"]["code"], ErrorCode.INPUT_TOO_LONG.value)
        self.assertEqual(FakeNative.embeds, 0, "the whole batch is tokenized before the first embedding")
        self.assertTrue(self.plugin.is_ready())
        self.assertEqual(FixtureRuntime.prepared, 1)

    def test_a_hung_call_is_reported_and_the_next_call_is_served(self) -> None:
        FakeNative.hangs_left = 1
        hung_runtime = self.plugin._runtime
        with self.assertLogs("coreai_embeddings_plugin.plugin", level="WARNING") as logged:
            outcome = self.plugin.generate_embeddings(["hang"])
        self.assertEqual(outcome["action_status"], "error")
        self.assertIn("exceeded 120 seconds", outcome["error"]["message"])
        self.assertTrue(any("prepared again after a timed-out" in line for line in logged.output), logged.output)
        self.assertIsNot(self.plugin._runtime, hung_runtime)
        self.assertTrue(self.plugin.is_ready())
        self.assertEqual(FixtureRuntime.prepared, 2)
        again = self.plugin.generate_embeddings(["after one", "after two"])
        self.assertEqual(again["action_status"], "completed", again["error"])
        self.assertEqual(len(again["data"]["result"]["embeddings"]), 2)
        self.assertEqual(self.plugin._count_tokens("three word text"), 3)
        self.assertEqual(outcome["error"]["code"], TIMEOUT_CODE)

    def test_a_hung_token_count_prepares_a_new_runtime_once(self) -> None:
        FakeTokenizer.count_hangs_left = 1
        with self.assertLogs("coreai_embeddings_plugin.plugin", level="WARNING") as logged, self.assertRaises(
            EmbeddingError,
        ) as raised:
            self.plugin._count_tokens("hang")
        self.assertEqual(raised.exception.code.value, TIMEOUT_CODE)
        self.assertTrue(any("prepared again after a timed-out" in line for line in logged.output), logged.output)
        self.assertTrue(self.plugin.is_ready())
        self.assertEqual(FixtureRuntime.prepared, 2)
        self.assertEqual(self.plugin._count_tokens("two words"), 2)
        self.assertEqual(self.plugin.generate_embeddings(["served"])["action_status"], "completed")
        self.assertEqual(FixtureRuntime.prepared, 2)

    def test_a_preparation_that_times_out_is_not_repeated_by_later_calls(self) -> None:
        FakeNative.hangs_left = 1
        FakeNative.load_hangs_left = 100  # every preparation after the first call hangs
        first = self.plugin.generate_embeddings(["hang"])
        self.assertEqual(first["error"]["code"], TIMEOUT_CODE)
        self.assertFalse(self.plugin.is_ready(), "a re-preparation that timed out leaves the plugin unavailable")
        self.assertEqual(FixtureRuntime.prepared, 2)
        for _ in range(3):
            started = time.monotonic()
            outcome = self.plugin.generate_embeddings(["again"])
            self.assertEqual(FixtureRuntime.prepared, 2, "a later generate_embeddings call prepared again")
            self.assertLess(time.monotonic() - started, BOUND_SECONDS / 2, "later calls fail fast")
            self.assertEqual(outcome["error"]["code"], TIMEOUT_CODE)
            started = time.monotonic()
            with self.assertRaises(EmbeddingError) as raised:
                self.plugin._count_tokens("two words")
            self.assertLess(time.monotonic() - started, BOUND_SECONDS / 2, "a count fails fast too")
            self.assertEqual(raised.exception.code.value, TIMEOUT_CODE)
        self.assertEqual(FixtureRuntime.prepared, 2, "no later call prepares again")

    def test_a_failed_native_call_stays_loud_without_preparing_again(self) -> None:
        for raised, code in (
            (RuntimeError("native failure"), ErrorCode.INFERENCE_FAILED),
            (EmbeddingError(ErrorCode.UNAVAILABLE, "native unavailable"), ErrorCode.UNAVAILABLE),
            (EmbeddingError(ErrorCode.INVALID_OUTPUT, "native output invalid"), ErrorCode.INVALID_OUTPUT),
        ):
            with self.subTest(code=code.value):
                self.plugin.prepare_for_readiness()
                prepared = FixtureRuntime.prepared
                FakeNative.raises = raised
                outcome = self.plugin.generate_embeddings(["failing"])
                self.assertEqual(outcome["error"]["code"], code.value)
                self.assertFalse(self.plugin.is_ready())
                self.assertEqual(FixtureRuntime.prepared, prepared, "no re-preparation after a failed native call")
                again = self.plugin.generate_embeddings(["failing again"])
                self.assertEqual(again["error"]["code"], code.value)
                self.assertEqual(FixtureRuntime.prepared, prepared)
                FakeNative.raises = None

    def test_nothing_on_the_worker_reenters_the_runtime_lock(self) -> None:
        self.assertEqual(self.plugin.generate_embeddings(["one", "two"])["action_status"], "completed")
        self.assertEqual(self.plugin._count_tokens("one two"), 2)
        self.assertEqual(FixtureRuntime.nested, 0)


if __name__ == "__main__":
    unittest.main()
