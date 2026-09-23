"""String Generation Service.

Provides cryptographically secure random string generation functionality extracted from StateService.
Handles various encoding formats (base36, hex, uuid) with proper validation and error handling.
"""

import logging
import secrets
from datetime import UTC, datetime

from ananta.core.domain.types import ActionResult, ErrorDetail

logger = logging.getLogger(__name__)


class StringGenerationService:
    """Service for generating cryptographically secure random strings.

    Provides centralized string generation for action names, IDs, tokens, and other unique identifiers.
    Extracted from StateService to achieve better separation of concerns.
    """

    def __init__(self) -> None:
        """Initialize string generation service."""
        logger.debug("StringGenerationService initialized with cryptographic security")

    def generate_unique_string(self, length: int = 13, encoding: str = "base36") -> ActionResult:
        """Generate cryptographically secure random string for action names, IDs, and other unique identifiers.

        This method provides a centralized, DRY-compliant source for all random string generation
        in the Ananta framework, eliminating scattered UUID and random generation patterns.

        Args:
            length: Length of random string (1-64 chars, default: 13 to match existing patterns)
            encoding: Encoding format ('base36', 'hex', or 'uuid')

        Returns:
            ActionResult with generated random string and metadata

        Security:
            Uses secrets module for cryptographically secure random generation
        """

        # Validate parameters
        validation_error = self._validate_generate_string_parameters(length, encoding)
        if validation_error:
            return validation_error

        try:
            result_string, entropy_bits = self._generate_string_by_encoding(length, encoding)
            return self._create_generate_string_success_response(
                result_string, length, encoding, entropy_bits
            )
        except Exception as e:
            return self._create_generate_string_error_response(encoding, length, e)

    def _validate_generate_string_parameters(
        self, length: int, encoding: str
    ) -> ActionResult | None:
        """Validate parameters for generate_unique_string operation."""

        if length < 1 or length > 64:
            error_detail: ErrorDetail = {
                "type": "validation_error",
                "code": "invalid_parameter",
                "message": f"Length must be integer between 1-64, got: {length}",
                "details": {"parameter": "length", "value": length},
                "severity": "error",
                "timestamp": datetime.now(UTC).isoformat(),
            }
            result: ActionResult = {
                "action_status": "error",
                "data": {},
                "actions": [],
                "error": error_detail,
                "timestamp": datetime.now(UTC).isoformat(),
            }
            return result

        if encoding not in ["base36", "hex", "uuid"]:
            error_detail_enc: ErrorDetail = {
                "type": "validation_error",
                "code": "invalid_parameter",
                "message": f"Encoding must be 'base36', 'hex', or 'uuid', got: {encoding}",
                "details": {"parameter": "encoding", "value": encoding},
                "severity": "error",
                "timestamp": datetime.now(UTC).isoformat(),
            }
            result_enc: ActionResult = {
                "action_status": "error",
                "data": {},
                "actions": [],
                "error": error_detail_enc,
                "timestamp": datetime.now(UTC).isoformat(),
            }
            return result_enc
        return None

    def _generate_string_by_encoding(self, length: int, encoding: str) -> tuple[str, int]:
        """Generate string based on specified encoding type."""
        if encoding == "base36":
            result_string, entropy_bits = self._generate_base36_string(length)
        elif encoding == "hex":
            result_string, entropy_bits = self._generate_hex_string(length)
        elif encoding == "uuid":
            result_string, entropy_bits = self._generate_uuid_string(length)
        else:
            raise ValueError(f"Unsupported encoding: {encoding}")

        self._validate_generated_string(result_string, length)

        return result_string, entropy_bits

    def _generate_base36_string(self, length: int) -> tuple[str, int]:
        """Generate base36 encoded string using cryptographic randomness."""
        # Use same cryptographic approach as existing _generate_table_id
        bits_needed = max(26, length * 6)  # Minimum 26 bits, or 6 bits per char for safety
        random_num = secrets.randbits(bits_needed)
        result_string = self._to_base36(random_num)

        # PARANOID CHECK: Ensure we have enough characters, pad if needed
        if len(result_string) < length:
            additional_bits = (length - len(result_string)) * 6
            additional_num = secrets.randbits(additional_bits)
            additional_str = self._to_base36(additional_num)
            result_string += additional_str

        result_string = result_string[:length]
        entropy_bits = min(bits_needed, length * 5)
        return result_string, entropy_bits

    def _generate_hex_string(self, length: int) -> tuple[str, int]:
        """Generate hexadecimal string."""
        hex_bytes_needed = (length + 1) // 2
        result_string = secrets.token_hex(hex_bytes_needed)[:length]
        entropy_bits = length * 4
        return result_string, entropy_bits

    def _generate_uuid_string(self, length: int) -> tuple[str, int]:
        """Generate UUID-based string."""
        uuid_str = secrets.token_hex(16)
        result_string = uuid_str[:length]
        entropy_bits = min(128, length * 4)
        return result_string, entropy_bits

    def _validate_generated_string(self, result_string: str, expected_length: int) -> None:
        """Validate that generated string meets requirements."""
        if len(result_string) != expected_length:
            raise ValueError(
                f"Generated string length {len(result_string)} != requested {expected_length}"
            )

        if not result_string.replace("-", "").replace("_", "").isalnum():
            raise ValueError(f"Generated string contains invalid characters: {result_string}")

    def _create_generate_string_success_response(
        self, result_string: str, length: int, encoding: str, entropy_bits: int
    ) -> ActionResult:
        """Create success response for generate_unique_string operation."""
        # TODO: Add assertion or validation that len(result_string) == length
        _ = length  # Acknowledge parameter is part of public API
        result: ActionResult = {
            "action_status": "completed",
            "data": {
                "random_string": result_string,
                "length": len(result_string),
                "encoding": encoding,
                "entropy_bits": entropy_bits,
            },
            "actions": [],
            "error": None,
            "timestamp": datetime.now(UTC).isoformat(),
        }
        return result

    def _create_generate_string_error_response(
        self, encoding: str, length: int, exception: Exception
    ) -> ActionResult:
        """Create error response for generate_unique_string operation."""
        error_detail: ErrorDetail = {
            "type": "runtime_error",
            "code": "generation_failed",
            "message": f"Random string generation failed: {str(exception)}",
            "details": {"encoding": encoding, "length": length, "exception": str(exception)},
            "severity": "error",
            "timestamp": datetime.now(UTC).isoformat(),
        }
        result: ActionResult = {
            "action_status": "error",
            "data": {},
            "actions": [],
            "error": error_detail,
            "timestamp": datetime.now(UTC).isoformat(),
        }
        return result

    def to_base36(self, num: int) -> str:
        """Convert number to base36 string.

        Args:
            num: Integer to convert to base36

        Returns:
            Base36 string representation of the number
        """
        if num == 0:
            return "0"
        digits = "0123456789abcdefghijklmnopqrstuvwxyz"
        result = ""
        while num:
            result = digits[num % 36] + result
            num //= 36
        return result

    def _to_base36(self, num: int) -> str:
        """Convert number to base36 string (private method for internal use)."""
        return self.to_base36(num)
