"""LLM session ledger service — platform-owned, not plugin-backed.

Construction happens in ``startup_sequence._init_session_ledger_service``
after ``create_service_wrappers`` and ``start_service_plugins``. The
concrete class lives in ``service.py`` and is re-exported here for
``from ananta.services.session_ledger_service import SessionLedgerService``.
"""

from ananta.services.session_ledger_service.service import (
    SessionLedgerService as SessionLedgerService,
)

__all__ = ["SessionLedgerService"]
