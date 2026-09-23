"""Managed Codex host-driver facade.

``agent_runtime`` and ``host`` stay orthogonal: this module exposes the
``("codex", "headless")`` persistent app-server driver and the
``("codex", "tmux")`` detached interactive driver without inventing compound
host names.  The implementations live in focused sibling modules so the
transport protocols can evolve independently while callers retain one stable
import surface.
"""

from .codex_app_server import (
    CodexAppServerHostDriver,
)
from .codex_app_server import (
    _CodexAppServerClient as _CodexAppServerClient,
)
from .codex_common import _identity_env as _identity_env
from .codex_tmux import (
    CodexTmuxHostDriver,
)
from .codex_tmux import (
    _CodexTmuxDriverChannel as _CodexTmuxDriverChannel,
)

__all__ = [
    "CodexAppServerHostDriver",
    "CodexTmuxHostDriver",
]
