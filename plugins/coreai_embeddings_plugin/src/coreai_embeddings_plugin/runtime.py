"""Synchronous ownership boundary around the asynchronous Core AI runtime."""

import asyncio
import platform
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import TYPE_CHECKING, TypeVar

from .cache_canary import CacheCanary, CanaryReport
from .contracts import EmbeddingError, ErrorCode
from .native import NativeModel
from .tokenization import NomicTokenizer

if TYPE_CHECKING:
    from .assets import InstalledAsset

T = TypeVar("T")
#: The oldest macOS this runtime serves on. 27 for now (rul_5cc2910c): Core AI Nomic has only been run on 27, so a
#: macOS 26 update keeps LM Studio; lowering it after the macOS 26 measurement (iss_c6abab0d) is a data change here
#: and in the release flow's host_profiles.apple_embeddings.macos_major_min, pinned equal by embedding_token_budget_smoke.
MIN_MACOS_MAJOR = 27


class EmbeddingRuntime:
    """Serialize native calls on one worker, including calls from an event loop."""

    def __init__(self, asset_root: Path, preference: str) -> None:
        self.asset_root = asset_root
        self.preference = preference
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="coreai-embedding")
        self._lock = threading.Lock()
        self._runner: asyncio.Runner | None = None
        self._native: NativeModel | None = None
        self._tokenizer: NomicTokenizer | None = None
        self._closed = False
        self._failed = False
        self.canary = CacheCanary(asset_root, preference)
        self._canary_report: CanaryReport | None = None

    def _call(self, operation: Callable[[], T]) -> T:
        with self._lock:
            if self._closed or self._failed:
                raise EmbeddingError(ErrorCode.UNAVAILABLE, "Runtime closed or failed; prepare again")
            future = self._executor.submit(operation)
            try:
                return future.result(timeout=120)
            except TimeoutError as exc:
                self._failed = True
                future.cancel()
                raise EmbeddingError(ErrorCode.UNAVAILABLE, "Core AI operation exceeded 120 seconds") from exc

    def prepare(self) -> None:
        """Verify assets, prove the compiled cache out of process, then load in process."""
        self._call(self._prepare_with_canary)

    def prepare_in_process(self) -> None:
        """Canary child entry: the same proof, never spawning a further canary."""
        self._call(self._prepare)

    def _prepare_with_canary(self) -> None:
        asset = self._verified_asset()
        self._canary_report = self.canary.ensure_healthy()
        self._load(asset)

    def _prepare(self) -> None:
        self._load(self._verified_asset())

    def _verified_asset(self) -> "InstalledAsset":
        from .assets import AssetCorruptError, AssetMissingError, verify_installed_asset

        if platform.system() != "Darwin" or int(platform.mac_ver()[0].split(".")[0] or "0") < MIN_MACOS_MAJOR:
            raise EmbeddingError(ErrorCode.UNAVAILABLE, f"Core AI embeddings require macOS {MIN_MACOS_MAJOR} or later")
        try:
            return verify_installed_asset(self.asset_root)
        except AssetMissingError as exc:
            raise EmbeddingError(ErrorCode.ASSET_MISSING, str(exc)) from exc
        except AssetCorruptError as exc:
            raise EmbeddingError(ErrorCode.ASSET_CORRUPT, str(exc)) from exc

    def _load(self, asset: "InstalledAsset") -> None:
        self._tokenizer = NomicTokenizer(asset.tokenizer_path)
        self._runner = asyncio.Runner()
        self._native = NativeModel()
        self._runner.run(self._native.load(asset.model_path, self.preference))
        self._runner.run(self._native.embed(self._tokenizer.encode("readiness probe")))

    def count_tokens(self, text: str) -> int:
        """The prepared tokenizer's count for one input, on the owner thread."""
        return self._call(lambda: self._count(text))

    def _count(self, text: str) -> int:
        if self._tokenizer is None:
            raise EmbeddingError(ErrorCode.UNAVAILABLE, "Runtime has not been prepared")
        return self._tokenizer.count(text)

    def generate(self, texts: list[str]) -> list[list[float]]:
        """Validate all token lengths before producing any batch output."""
        return self._call(lambda: self._generate(texts))

    def _generate(self, texts: list[str]) -> list[list[float]]:
        if self._runner is None or self._native is None or self._tokenizer is None:
            raise EmbeddingError(ErrorCode.UNAVAILABLE, "Runtime has not been prepared")
        encoded = [self._tokenizer.encode(text) for text in texts]
        return [self._runner.run(self._native.embed(item)) for item in encoded]

    def diagnostics(self) -> dict[str, object]:
        """Distinguish configured preference from measured execution evidence."""
        canary = {"cache_canary": self._canary_report.as_dict()} if self._canary_report else {}
        if self._native is None:
            return {"compute_preference": self.preference, "observed_compute_unit": "unknown", **canary}
        return {
            **canary,
            "compute_preference": self.preference,
            "selected_compute_preference": self._native.compute_preference,
            "observed_compute_unit": self._native.observed_compute_unit,
            "compute_evidence": sorted(self._native.compute_evidence),
            "fallback_reason": self._native.fallback_reason,
        }

    def close(self) -> None:
        """Close on the owner thread; repeated cleanup is harmless."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._executor.submit(self._close_native)
            self._executor.shutdown(wait=False)

    def _close_native(self) -> None:
        self._native = None
        self._tokenizer = None
        if self._runner is not None:
            self._runner.close()
            self._runner = None
