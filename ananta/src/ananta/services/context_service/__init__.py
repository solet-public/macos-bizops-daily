"""Context service — decoupled core provider of the agent-context briefing API.

See ``service.py`` (concrete ``ContextService``) and ``interfaces/public.py``
(``ContextServiceAPI`` — the ``@service_interface_process`` registry surface).
"""

from __future__ import annotations

from ananta.services.context_service.service import ContextService

__all__ = ["ContextService"]
