"""Vault caller-enforcement helpers and decorators.

W-VAULT-CALLER-ENFORCE (P0 Tier 2 sub-2, state-service consolidation
campaign): activates the two structural enforcement paths the Tier 1
signature-wiring left dormant.

Two enforcement layers:

* ``_enforce_namespace(key, ctx)`` — for key-bearing methods. Plugin
  principals can only read/write keys whose middle segment matches the
  caller plugin's name; operator/operator_equivalent principals bypass;
  external principals cannot touch plugin-scoped keys; missing
  ``CallContext`` is a server-side bug and raises immediately.

* ``@requires_operator_principal`` — a method decorator for the 18
  operator-only verbs (see ``vault_service_proxy.OPERATOR_ONLY_METHODS``
  for the lockstep list). The decorator reads the bound ``call_context``
  keyword argument, asserts ``ctx.is_operator_principal``, raises
  ``VaultAccessDeniedError`` otherwise.

Both layers live here (vs. duplicated in each concrete vault plugin) so
``macos_vault_plugin`` and ``secrets_manager_vault_plugin`` share the
exact same authorization surface. A drift between them would re-open
the very gap this commit closes.

Reference:
- ``ananta/src/ananta/core/services/call_context.py`` —
  ``CallContext`` definition, ``VaultAccessDeniedError``,
  ``VaultKeyMalformedError`` exception classes.
- ``ananta/src/ananta/services/vault_service/vault_service_proxy.py`` —
  ``OPERATOR_ONLY_METHODS`` lockstep constant.
- ``workbench/2026-06-07_state_service_consolidation_master_plan.md``
  §3.3.4, §3.3.5 — master plan invariants.
"""

from __future__ import annotations

from collections.abc import Callable
from functools import wraps

from ananta.core.services.call_context import (
    CallContext,
    VaultAccessDeniedError,
    VaultKeyMalformedError,
)


def enforce_namespace(key: str, ctx: CallContext | None) -> None:
    """Authorize a key-bearing vault call against the caller's principal.

    Per master plan §3.3.1 + §3.3.4. Splits ``key`` on the first two
    ``.`` characters so the credential segment may contain further
    dots. Raises immediately on:

    * ``ctx is None`` — server-side bug (the proxy / ActionProcessor
      must always inject); raises ``VaultAccessDeniedError``.
    * Plugin principal with no ``calling_plugin`` (external principal
      pretending to be a plugin) — raises ``VaultAccessDeniedError``.
    * Key with fewer than three segments — raises ``VaultKeyMalformedError``
      (structural, not authz).
    * Plugin principal whose ``calling_plugin`` doesn't match the key's
      plugin segment — raises ``VaultAccessDeniedError``.

    Operator and operator-equivalent principals bypass the namespace
    check (operator can read/write any key in any namespace; that's the
    only path for cross-namespace operations like rename across plugin
    scopes).
    """
    if ctx is None:
        raise VaultAccessDeniedError("missing CallContext on vault call")
    if ctx.is_operator_principal:
        return
    if ctx.calling_plugin is None:
        raise VaultAccessDeniedError(
            "external principal cannot access plugin-scoped vault keys",
        )
    parts = key.split(".", 2)
    if len(parts) < 3:
        raise VaultKeyMalformedError(
            f"key {key!r} not in <solet>.<plugin>.<credential> form",
        )
    if parts[1] != ctx.calling_plugin:
        raise VaultAccessDeniedError(
            f"plugin {ctx.calling_plugin!r} cannot access key in "
            f"namespace {parts[1]!r}",
        )


def assert_operator_principal(
    ctx: CallContext | None, method_name: str,
) -> None:
    """Authorize an operator-only vault method call.

    Used by ``@requires_operator_principal`` and (for callers that want
    explicit guard placement) directly. Raises
    ``VaultAccessDeniedError`` when the principal isn't operator or
    operator-equivalent. Missing ``CallContext`` is also a denial — the
    server-side caller-stamping path is supposed to always inject one.
    """
    if ctx is None or not ctx.is_operator_principal:
        raise VaultAccessDeniedError(
            f"{method_name} requires operator/operator-equivalent principal",
        )


def requires_operator_principal[**P, R](
    func: Callable[P, R],
) -> Callable[P, R]:
    """Decorate a vault method that may only be invoked by an operator.

    The decorator reads ``call_context`` from kwargs, asserts the
    principal is operator or operator-equivalent, and forwards to the
    wrapped method on success. Picks up the method name automatically
    so the smoke parity test can introspect the decorated set without
    a separate constant lockstep on the plugin side.

    The wrapped method MUST accept ``call_context`` as a keyword-only
    parameter (matches the Tier 1 signature-wiring contract). Positional
    ``call_context`` is not supported intentionally — every existing
    call site already passes it as kwarg.
    """

    @wraps(func)
    def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
        ctx = kwargs.get("call_context")
        ctx_typed = ctx if isinstance(ctx, CallContext) else None
        assert_operator_principal(ctx_typed, func.__name__)
        return func(*args, **kwargs)

    return wrapper


__all__ = [
    "assert_operator_principal",
    "enforce_namespace",
    "requires_operator_principal",
]
