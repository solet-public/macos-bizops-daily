"""Native adapter fault controls, independent of measured hardware evidence."""

import asyncio
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "ananta" / "src"))
sys.path.insert(0, str(ROOT / "plugins" / "coreai_embeddings_plugin" / "src"))

import numpy as np  # noqa: E402
from coreai_embeddings_plugin.contracts import EmbeddingError  # noqa: E402
from coreai_embeddings_plugin.native import NativeModel  # noqa: E402
from coreai_embeddings_plugin.tokenization import EncodedInput  # noqa: E402


def _output(vector: object) -> dict[str, object]:
    return {"embedding": SimpleNamespace(numpy=lambda: vector)}


class NativeTests(unittest.TestCase):
    """Exercise failure paths which real healthy hardware cannot deterministically trigger."""

    def test_invalid_outputs_fail(self) -> None:
        cases = [np.zeros((1, 768), dtype=np.float32), np.ones((1, 10), dtype=np.float32), np.full((1, 768), np.nan, dtype=np.float32), np.ones((1, 768), dtype=np.float64)]
        for vector in cases:
            with self.subTest(shape=vector.shape):
                native = NativeModel()
                native._functions = {256: AsyncMock(return_value=_output(vector))}
                sdk = SimpleNamespace(NDArray=lambda value: value)
                with patch("coreai_embeddings_plugin.native.importlib.import_module", return_value=sdk):
                    with self.assertRaises(EmbeddingError):
                        asyncio.run(native.embed(EncodedInput(256, [1]*256, [1]*256)))

    def test_gpu_load_failure_falls_back_to_cpu(self) -> None:
        model = MagicMock()
        model.function_names = ["seq256", "seq512", "seq1024", "seq2048"]
        sdk = MagicMock()
        sdk.AIModel.load = AsyncMock(side_effect=[RuntimeError("GPU unavailable"), model])
        native = NativeModel()
        with patch("coreai_embeddings_plugin.native.importlib.import_module", return_value=sdk):
            asyncio.run(native.load(Path("/fixture.aimodel"), "gpu"))
        self.assertEqual(native.compute_preference, "cpu")
        self.assertEqual(native.fallback_reason, "GPU unavailable")
        self.assertEqual(native.observed_compute_unit, "unknown")
        self.assertEqual(sdk.AIModel.load.await_count, 2)

    def test_gpu_inference_failure_retries_once(self) -> None:
        vector = np.zeros((1, 768), dtype=np.float32)
        vector[0, 0] = 1
        native = NativeModel()
        native._path = Path("/fixture.aimodel")
        native._functions = {256: AsyncMock(side_effect=RuntimeError("GPU execution failed"))}
        async def load(_path: Path, preference: str) -> None:
            native.compute_preference = preference
            native._functions = {256: AsyncMock(return_value=_output(vector))}
        sdk = SimpleNamespace(NDArray=lambda value: value)
        with patch.object(native, "load", side_effect=load) as loader:
            with patch("coreai_embeddings_plugin.native.importlib.import_module", return_value=sdk):
                output = asyncio.run(native.embed(EncodedInput(256, [1]*256, [1]*256)))
        loader.assert_called_once_with(Path("/fixture.aimodel"), "cpu")
        self.assertEqual(output[0], 1)
        self.assertEqual(native.observed_compute_unit, "cpu")
        self.assertEqual(native.fallback_reason, "GPU execution failed")

    def test_missing_bucket_is_asset_error_without_fallback(self) -> None:
        sdk = MagicMock()
        model = MagicMock()
        model.function_names = ["seq256"]
        sdk.AIModel.load = AsyncMock(return_value=model)
        with patch("coreai_embeddings_plugin.native.importlib.import_module", return_value=sdk):
            with self.assertRaises(EmbeddingError):
                asyncio.run(NativeModel().load(Path("/fixture.aimodel"), "gpu"))
        self.assertEqual(sdk.AIModel.load.await_count, 1)


if __name__ == "__main__":
    unittest.main()
