"""Focused contract for bounded vault qualification probes."""

from abc import ABC, abstractmethod
from typing import ClassVar

from ananta.core.domain.types import ActionResult
from ananta.core.services.call_context import CallContext


class VaultQualificationInterface(ABC):
    """Non-operational vault health proofs kept separate from secret operations."""

    INTERFACE_VERSION: ClassVar[str] = "1.0.0"

    @abstractmethod
    def qualify_keychain(
        self,
        *,
        call_context: CallContext | None = None,
    ) -> ActionResult:
        """Report bounded Keychain availability and user-access proof."""
        ...

    @abstractmethod
    def qualify_round_trip(
        self,
        *,
        call_context: CallContext | None = None,
    ) -> ActionResult:
        """Verify a private canary round trip that deletes the canary before return."""
        ...
