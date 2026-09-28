"""Tokenize exactly as the pinned Nomic tokenizer; never truncate caller text."""

from dataclasses import dataclass
from pathlib import Path

from tokenizers import Tokenizer

from .contracts import BUCKETS, EmbeddingError, ErrorCode


@dataclass(frozen=True)
class EncodedInput:
    """One padded model input with the actual attention mask."""

    bucket: int
    input_ids: list[int]
    attention_mask: list[int]


class NomicTokenizer:
    """A private, immutable-configuration tokenizer owned by one runtime."""

    def __init__(self, path: Path) -> None:
        self._tokenizer = Tokenizer.from_file(str(path))
        self._tokenizer.no_truncation()
        self._tokenizer.no_padding()
        pad_id = self._tokenizer.token_to_id("[PAD]")
        if pad_id is None:
            raise EmbeddingError(ErrorCode.ASSET_CORRUPT, "Tokenizer has no [PAD] token")
        self._pad_id = pad_id

    def count(self, text: str) -> int:
        """Tokens including special tokens: the number ``encode`` compares to the ceiling."""
        return len(self._tokenizer.encode(text, add_special_tokens=True).ids)

    def encode(self, text: str) -> EncodedInput:
        """Include special tokens in the ceiling; preserve caller prefixes."""
        encoded = self._tokenizer.encode(text, add_special_tokens=True)
        length = len(encoded.ids)
        if length > BUCKETS[-1]:
            raise EmbeddingError(
                ErrorCode.INPUT_TOO_LONG,
                f"Input has {length} tokens including special tokens; maximum is {BUCKETS[-1]}",
            )
        bucket = next(size for size in BUCKETS if size >= length)
        padding = bucket - length
        return EncodedInput(
            bucket, encoded.ids + [self._pad_id] * padding,
            encoded.attention_mask + [0] * padding,
        )


def validate_inputs(inputs: object, model: str | None, input_type: str) -> None:
    """Fail the whole batch before inference for unsupported requests."""
    from .contracts import MODEL_ID

    if model is not None and model != MODEL_ID:
        raise EmbeddingError(ErrorCode.UNSUPPORTED_MODEL, f"Unsupported model: {model}")
    if input_type != "text":
        raise EmbeddingError(ErrorCode.INVALID_INPUT, "Only text inputs are supported")
    if not isinstance(inputs, list) or not inputs:
        raise EmbeddingError(ErrorCode.INVALID_INPUT, "inputs must be a nonempty list")
    if any(not isinstance(text, str) or not text.strip() for text in inputs):
        raise EmbeddingError(ErrorCode.INVALID_INPUT, "Every input must be nonempty text")
