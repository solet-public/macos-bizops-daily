"""Pinned build recipe for the measured Nomic Core AI bucketed asset.

This is a build-time tool; runtime imports no Torch, Transformers or HF client.
The default operation verifies cached upstream inputs only. Conversion needs
an explicit unused output path and never overwrites the measured prototype.
"""

from __future__ import annotations

import argparse
import importlib.metadata
import platform
from pathlib import Path

from .acquisition import FILE_PINS, UPSTREAM_REVISION, verify_source_files

MODEL_REPOSITORY = "nomic-ai/nomic-embed-text-v1.5"
MODEL_CODE_REPOSITORY = "nomic-ai/nomic-bert-2048"
MODEL_CODE_REVISION = "7710840340a098cfb869c4f65e87cf2b1b70caca"
SOURCE_MODEL_SHA256 = "9e7d262b1fe5ea350782829496efa831901b77486bbde1cea54a4c822d010d5c"
SOURCE_MODEL_SIZE_BYTES = 546938168
CREATION_DATE_EPOCH = 1790397134  # measured prototype: 2026-09-26T04:32:14Z
BUCKETS = (256, 512, 1024, 2048)
BUILD_DEPENDENCIES = {
    "coreai-core": "1.0.0b3",
    "coreai-torch": "0.4.3",
    "torch": "2.13.0",
    "transformers": "4.57.3",
    "tokenizers": "0.22.2",
    "huggingface-hub": "0.36.2",
    "safetensors": "0.8.0",
}


def _assert_build_environment() -> None:
    if platform.system() != "Darwin" or tuple(int(part) for part in platform.mac_ver()[0].split(".")[:1]) < (27,):
        raise RuntimeError("Core AI conversion requires macOS 27 or later")
    for package, expected in BUILD_DEPENDENCIES.items():
        observed = importlib.metadata.version(package)
        if observed != expected:
            raise RuntimeError(f"{package} version {observed} differs from pinned {expected}")


def _snapshot(cache_dir: Path, repository: str, revision: str, files: list[str]) -> Path:
    from huggingface_hub import snapshot_download

    return Path(
        snapshot_download(
            repo_id=repository,
            revision=revision,
            cache_dir=cache_dir,
            allow_patterns=files,
            local_files_only=True,
        )
    )


def _verify_upstream(model_snapshot: Path) -> None:
    import hashlib

    model = model_snapshot / "model.safetensors"
    if model.stat().st_size != SOURCE_MODEL_SIZE_BYTES:
        raise RuntimeError("upstream model.safetensors size differs from pin")
    digest = hashlib.sha256()
    with model.open("rb") as stream:
        while block := stream.read(1024 * 1024):
            digest.update(block)
    if digest.hexdigest() != SOURCE_MODEL_SHA256:
        raise RuntimeError("upstream model.safetensors SHA-256 differs from pin")
    tokenizer_pin = next(pin for pin in FILE_PINS if pin.relative_path == "tokenizer.json")
    tokenizer = model_snapshot / "tokenizer.json"
    if tokenizer.stat().st_size != tokenizer_pin.size_bytes:
        raise RuntimeError("upstream tokenizer size differs from pin")
    digest = hashlib.sha256(tokenizer.read_bytes()).hexdigest()
    if digest != tokenizer_pin.sha256:
        raise RuntimeError("upstream tokenizer SHA-256 differs from pin")


def _convert(model_snapshot: Path, cache_dir: Path, output: Path) -> None:
    import torch
    from coreai.runtime import AIModelAssetMetadata  # pyright: ignore[reportMissingImports]
    from coreai_torch import TorchConverter, get_decomp_table  # pyright: ignore[reportMissingImports]
    from transformers import AutoModel, AutoTokenizer

    class Embedder(torch.nn.Module):
        def __init__(self, model: torch.nn.Module) -> None:
            super().__init__()
            self.model = model

        def forward(self, input_ids: torch.Tensor, attention_mask: torch.Tensor) -> torch.Tensor:
            hidden = self.model(input_ids=input_ids, attention_mask=attention_mask)[0]
            mask = attention_mask.unsqueeze(-1).to(hidden.dtype)
            pooled = (hidden * mask).sum(1) / mask.sum(1).clamp(min=1e-9)
            return torch.nn.functional.normalize(pooled.float(), p=2, dim=1)

    tokenizer = AutoTokenizer.from_pretrained(model_snapshot, local_files_only=True)
    base = AutoModel.from_pretrained(
        model_snapshot,
        trust_remote_code=True,
        code_revision=MODEL_CODE_REVISION,
        cache_dir=cache_dir,
        local_files_only=True,
        use_safetensors=True,
    )
    embedder = Embedder(base.eval()).eval().to(torch.float16)  # pyright: ignore[reportPrivateImportUsage]
    converter = TorchConverter(mode=TorchConverter.Mode.RELEASE)
    for bucket in BUCKETS:
        encoded = tokenizer(
            ["Hello, my dog is cute"],
            return_tensors="pt",
            padding="max_length",
            max_length=bucket,
        )
        arguments = {
            "input_ids": encoded["input_ids"].to(torch.int32),  # pyright: ignore[reportPrivateImportUsage]
            "attention_mask": encoded["attention_mask"].to(torch.int32),  # pyright: ignore[reportPrivateImportUsage]
        }
        with torch.no_grad():
            exported = torch.export.export(embedder, args=(), kwargs=arguments)
            exported = exported.run_decompositions(get_decomp_table())
        converter.add_exported_program(
            exported,
            input_names=["input_ids", "attention_mask"],
            output_names=["embedding"],
            entrypoint_name=f"seq{bucket}",
        )
    metadata = AIModelAssetMetadata()
    metadata.author = "Nomic AI"
    metadata.license = "Apache-2.0"
    metadata.model_description = "nomic-embed-text-v1.5, length-bucket entrypoints seq256/512/1024/2048"
    metadata.creation_date = CREATION_DATE_EPOCH
    converter.to_coreai().save_asset(output, metadata)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hf-cache", required=True, type=Path)
    parser.add_argument("--output", type=Path, help="Unused .aimodel path; omit to verify upstream inputs only")
    arguments = parser.parse_args()
    _assert_build_environment()
    model_snapshot = _snapshot(
        arguments.hf_cache,
        MODEL_REPOSITORY,
        UPSTREAM_REVISION,
        ["model.safetensors", "config.json", "tokenizer.json", "tokenizer_config.json", "special_tokens_map.json", "vocab.txt"],
    )
    _snapshot(arguments.hf_cache, MODEL_CODE_REPOSITORY, MODEL_CODE_REVISION, ["*.py"])
    _verify_upstream(model_snapshot)
    print(f"Verified pinned upstream {MODEL_REPOSITORY}@{UPSTREAM_REVISION}")
    if arguments.output is None:
        return
    output: Path = arguments.output
    if output.suffix != ".aimodel" or output.exists() or output.is_symlink():
        raise RuntimeError("--output must name an unused .aimodel directory")
    if output.parent.is_symlink():
        raise RuntimeError("--output parent must not be a symlink")
    output.parent.mkdir(parents=True, exist_ok=True)
    _convert(model_snapshot, arguments.hf_cache, output)
    verify_source_files(output, model_snapshot / "tokenizer.json")
    print(f"Verified pinned converted output: {output}")


if __name__ == "__main__":
    main()
