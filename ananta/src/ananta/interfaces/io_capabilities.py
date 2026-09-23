from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class IOCapability(StrEnum):
    """Enumerates the delivery modes supported by IO plugins."""

    TEXT = "text"
    RICH_TEXT = "rich_text"
    FILE_UPLOAD = "file_upload"
    IMAGE_UPLOAD = "image_upload"
    URL_ONLY = "url_only"
    AUDIO_OUT = "audio_out"
    AUDIO_IN = "audio_in"


@dataclass(slots=True)
class IOContent:
    """Structured representation of content delivered through IO plugins."""

    text: str | None = None
    blob_id: str | None = None
    media_type: str | None = None
    caption: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
