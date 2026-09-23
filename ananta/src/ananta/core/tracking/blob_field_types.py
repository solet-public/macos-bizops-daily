"""Type definitions for blob_fields mapping in result processor customizations.

blob_fields maps attachment field names to either:
- str: field name to look up in action result data
- LiteralValue(value): constant value to use directly

Example:
    blob_fields = {
        "blob_id": "audio_blob_key",            # Lookup result["data"]["audio_blob_key"]
        "artifact_type": LiteralValue("audio"), # Use constant "audio"
        "media_type": LiteralValue("audio/wav"),# Use constant "audio/wav"
    }
"""

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class LiteralValue:
    """Wrapper for constant values in blob_fields mapping.

    Use this to distinguish constant values from field name lookups.
    Bare strings are interpreted as field names to look up in result data.
    LiteralValue instances provide the wrapped value directly.
    """

    value: Any
