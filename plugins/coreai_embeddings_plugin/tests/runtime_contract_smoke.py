"""Focused boundary/lifecycle tests; native numeric proof is a separate real test."""

import asyncio
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "ananta" / "src"))
sys.path.insert(0, str(ROOT / "plugins" / "coreai_embeddings_plugin" / "src"))

from coreai_embeddings_plugin.contracts import DIMENSION, MODEL_ID, EmbeddingError, ErrorCode  # noqa: E402
from coreai_embeddings_plugin.plugin import CoreAIEmbeddingsPlugin  # noqa: E402
from coreai_embeddings_plugin.runtime import EmbeddingRuntime  # noqa: E402
from coreai_embeddings_plugin.tokenization import NomicTokenizer, validate_inputs  # noqa: E402
from tokenizers import Tokenizer, models, pre_tokenizers, processors  # noqa: E402


class TokenBoundaryTests(unittest.TestCase):
    """An actual tokenizer fixture exercises special tokens and padding masks."""

    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        path = Path(self.directory.name) / "tokenizer.json"
        tokenizer = Tokenizer(models.WordLevel({"[PAD]": 0, "[UNK]": 1, "[CLS]": 2, "[SEP]": 3, "a": 4}, unk_token="[UNK]"))
        tokenizer.pre_tokenizer = pre_tokenizers.Whitespace()
        tokenizer.post_processor = processors.TemplateProcessing(
            single="[CLS] $A [SEP]", special_tokens=[("[CLS]", 2), ("[SEP]", 3)],
        )
        tokenizer.save(str(path))
        self.tokenizer = NomicTokenizer(path)

    def tearDown(self) -> None:
        self.directory.cleanup()

    def test_all_boundaries_include_special_tokens(self) -> None:
        for length, bucket in [(254, 256), (255, 512), (510, 512), (511, 1024), (1022, 1024), (1023, 2048), (2046, 2048)]:
            with self.subTest(length=length):
                encoded = self.tokenizer.encode(" ".join(["a"] * length))
                self.assertEqual(encoded.bucket, bucket)
                self.assertEqual(len(encoded.input_ids), bucket)
                self.assertEqual(sum(encoded.attention_mask), length + 2)
                self.assertEqual(encoded.input_ids[:2], [2, 4])
                self.assertEqual(encoded.input_ids[length + 1], 3)
                self.assertTrue(all(value == 0 for value in encoded.input_ids[length + 2:]))
        with self.assertRaises(EmbeddingError) as caught:
            self.tokenizer.encode(" ".join(["a"] * 2047))
        self.assertEqual(caught.exception.code, ErrorCode.INPUT_TOO_LONG)

    def test_input_contract(self) -> None:
        for inputs in ([], "text", [None], [""], ["   "]):
            with self.subTest(inputs=inputs), self.assertRaises(EmbeddingError):
                validate_inputs(inputs, None, "text")
        with self.assertRaises(EmbeddingError):
            validate_inputs(["a"], "unknown", "text")
        with self.assertRaises(EmbeddingError):
            validate_inputs(["a"], None, "image")
        validate_inputs(["search_query: a"], MODEL_ID, "text")


class FakeRuntime:
    """Fault-injection fixture; never used for numeric evidence."""

    fail_prepare = False
    fail_generate = False

    def __init__(self, _root: Path, _preference: str) -> None:
        self.closed = False
        self.inputs: list[str] = []

    def prepare(self) -> None:
        if self.fail_prepare:
            raise EmbeddingError(ErrorCode.ASSET_MISSING, "asset absent")

    def generate(self, inputs: list[str]) -> list[list[float]]:
        if self.fail_generate:
            raise EmbeddingError(ErrorCode.INVALID_OUTPUT, "bad output")
        self.inputs = inputs
        return [[1.0] + [0.0] * (DIMENSION - 1) for _ in inputs]

    def close(self) -> None:
        self.closed = True

    def diagnostics(self) -> dict[str, object]:
        return {"observed_compute_unit": "fixture"}


class LifecycleTests(unittest.TestCase):
    """Missing, repaired, corrected, closed and reopened runtime transitions."""

    def test_missing_repair_and_reopen(self) -> None:
        plugin = CoreAIEmbeddingsPlugin({"asset_root": "/fixture"})
        with patch("coreai_embeddings_plugin.plugin.EmbeddingRuntime", FakeRuntime):
            FakeRuntime.fail_prepare = True
            plugin.prepare_for_readiness()
            self.assertFalse(plugin.is_ready())
            self.assertEqual(plugin.generate_embeddings(["hello"])["error"]["code"], ErrorCode.ASSET_MISSING)
            self.assertEqual(plugin.get_runtime_status()["severity"], "warning")
            FakeRuntime.fail_prepare = False
            plugin.prepare_for_readiness()
            self.assertTrue(plugin.is_ready())
            self.assertEqual(plugin.generate_embeddings(["search_query: hello"])["action_status"], "completed")
            asyncio.run(plugin.cleanup())
            asyncio.run(plugin.cleanup())
            self.assertFalse(plugin.is_ready())
            plugin.prepare_for_readiness()
            self.assertTrue(plugin.is_ready())
            plugin.initialize({"asset_root": "relative"})
            self.assertFalse(plugin.is_ready())
            plugin.prepare_for_readiness()
            self.assertFalse(plugin.is_ready())

    def test_bad_output_revokes_readiness(self) -> None:
        plugin = CoreAIEmbeddingsPlugin({"asset_root": "/fixture"})
        with patch("coreai_embeddings_plugin.plugin.EmbeddingRuntime", FakeRuntime):
            plugin.prepare_for_readiness()
            FakeRuntime.fail_generate = True
            try:
                self.assertEqual(plugin.generate_embeddings(["hello"])["action_status"], "error")
                self.assertFalse(plugin.is_ready())
            finally:
                FakeRuntime.fail_generate = False

    def test_metadata_is_not_readiness(self) -> None:
        plugin = CoreAIEmbeddingsPlugin()
        self.assertEqual(plugin.get_default_dimensions(), 768)
        self.assertEqual(plugin.get_embedding_dimension()["data"]["result"]["dimension"], 768)
        self.assertFalse(plugin.list_models()["data"]["result"]["models"][0]["available"])
        self.assertEqual(plugin.get_embedding_dimension("wrong")["action_status"], "error")
        self.assertEqual(plugin.generate_embeddings(["x"])["action_status"], "error")

    def test_sync_worker_inside_running_loop(self) -> None:
        runtime = EmbeddingRuntime(Path("/fixture"), "cpu")
        async def invoke() -> Any:
            return runtime._call(lambda: asyncio.run(asyncio.sleep(0, result=42)))
        try:
            self.assertEqual(asyncio.run(invoke()), 42)
        finally:
            runtime.close()
        with self.assertRaises(EmbeddingError):
            runtime.prepare()


if __name__ == "__main__":
    unittest.main()
