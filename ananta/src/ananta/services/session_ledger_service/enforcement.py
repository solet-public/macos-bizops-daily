"""Session-ledger registration authorization (P1.1.E authz gate).

Once the pulling plugins honor a per-source ``root_uri`` (P1.1.E), the public
``register_source`` verb lets any caller point the ledger at arbitrary local
files. So the public verb gates a **filesystem** ``root_uri`` behind two
checks:

* an operator (or operator-equivalent) principal — a plugin / external
  principal may not register a filesystem root; and
* containment — the realpath-resolved candidate must live under a realpath of
  one operator-configured ``ledger_allowed_roots`` entry (``commonpath``
  equality, NOT string-prefix).

Blob-id / pushed / symbolic ``root_uri`` values (``bmd-*``, ``pushed:*``,
``local:*``) are not filesystem roots and are admitted unconditionally — the
export + pushed registration paths use them and route through the trusted
internal seam regardless.

Mirrors the vault-service caller-enforcement pattern
(``ananta/src/ananta/services/vault_service/enforcement.py``) but stays
ledger-local so a ledger authz failure surfaces a ledger-specific error
(``LedgerAuthorizationError``) rather than a vault one. Uses the CallContext
principal path ONLY — never the ``state`` / ``extract_authenticated_principal``
path (mixing the two principal sources is the round-4 footgun).
"""

from __future__ import annotations

import os

from ananta.core.services.call_context import CallContext
from ananta.llm.session_ledger.root_uri import root_uri_to_path


class LedgerAuthorizationError(Exception):
    """Raised when a session-ledger registration call fails authorization."""


def assert_operator_principal(
    call_context: CallContext | None, method_name: str,
) -> None:
    """Authorize an operator-only ledger verb (e.g. the blob-identity backfill).

    Mirrors ``vault_service.enforcement.assert_operator_principal``. Missing
    ``CallContext`` is a denial — the server-side caller-stamping path always
    injects one when ``requires_call_context=True``.
    """
    if call_context is None or not call_context.is_operator_principal:
        raise LedgerAuthorizationError(
            f"{method_name} requires an operator/operator-equivalent principal",
        )


def is_filesystem_root_uri(root_uri: str) -> bool:
    """True iff ``root_uri`` is filesystem-INTENDED (a path or ``file://`` URI).

    A ``file://`` / ``/`` / ``~`` prefix is filesystem-intended, so the gate must
    apply — INCLUDING a malformed ``file://`` (bad authority). Such a malformed
    URI is then DENIED by the containment check (which fails to resolve it),
    rather than silently admitted as a non-filesystem 'sentinel' (the
    self-containment fix). Only genuine sentinels (``pushed:*`` / ``local:*`` /
    blob ids) are non-filesystem and admitted unconditionally.
    """
    return root_uri.startswith(("file://", "/", "~"))


def assert_register_source_authorized(
    *,
    root_uri: str,
    call_context: CallContext | None,
    allowed_roots: list[str],
    method_name: str = "register_source",
) -> None:
    """Authorize a public ``register_source`` call.

    Non-filesystem ``root_uri`` values are admitted unconditionally. A
    filesystem ``root_uri`` requires an operator/operator-equivalent principal
    AND containment under one ``allowed_roots`` entry. With an empty
    ``allowed_roots`` the secure default is to deny every filesystem
    registration — the operator opts paths in via the profile config.
    """
    if not is_filesystem_root_uri(root_uri):
        return
    if call_context is None or not call_context.is_operator_principal:
        raise LedgerAuthorizationError(
            f"{method_name} of a filesystem root_uri requires an "
            "operator/operator-equivalent principal",
        )
    try:
        candidate = os.path.realpath(str(root_uri_to_path(root_uri)))
    except ValueError as exc:
        # Filesystem-intended but unresolvable (e.g. a malformed ``file://``
        # authority) — deny rather than admit. The gate is self-contained.
        raise LedgerAuthorizationError(
            f"{method_name}: malformed filesystem root_uri {root_uri!r}: {exc}",
        ) from exc
    for allowed in allowed_roots:
        allowed_real = os.path.realpath(
            os.path.expanduser(os.path.expandvars(allowed)),
        )
        if os.path.commonpath([allowed_real, candidate]) == allowed_real:
            return
    raise LedgerAuthorizationError(
        f"{method_name}: filesystem root_uri {root_uri!r} is not contained in "
        "any operator-configured ledger_allowed_roots",
    )
