"""Process-wide gate deciding whether a keychain read may block on a GUI prompt.

A credential read the item's ACL does not authorize has two possible outcomes,
and only one of them is survivable for a daemon:

* **User interaction ALLOWED** (the process default) — macOS resolves the
  refusal by raising a SecurityAgent confirmation dialog and **blocks the
  calling thread until someone answers it**. There is no timeout. For a
  background solet nobody is watching, that is forever.
* **User interaction DISALLOWED** — the same refusal returns
  ``errSecAuthFailed`` immediately, and the caller fails loud.

This is not hypothetical. On 2026-08-31 a Homebrew ``python@3.13`` upgrade
(3.13.13_1 → 3.13.15) replaced the ad-hoc-signed interpreter binary that every
newly spawned solet resolves to. An ad-hoc signature has no stable signing
authority, so the Keychain ACL pins the exact code-directory hash — the new
build was a different application, and the state plugin's ``db_password`` item
refused it. Two blue-green cutovers each hung for the swap's full 600 s
``register_timeout`` inside that single read, emitted not one log line, and
stalled the platform's action queue behind them. That incident was
diagnosed on 2026-09-01; the write-up lives in the workbench, which ships with
no profile, so it is deliberately not cited as a path here.

Two properties of this gate are load-bearing, and both are deliberate:

* **It is scoped to reads, not latched for the process.** Operator-run seed
  tooling legitimately needs the prompt — answering it is precisely how a human
  re-grants the ACL after the signing identity changes. A latched gate would
  make the repair path unusable, which is why
  :func:`~macos_vault_plugin.keychain.SystemKeychain` takes an explicit
  ``allow_user_interaction`` opt-out rather than this module setting the state
  once at import.
* **It restores the prior value in a ``finally``.** A read that raises must not
  leave the process unable to prompt, or one failed daemon read would silently
  disarm an operator tool running later in the same process.

ctypes rather than PyObjC on purpose: the setting is process-global state
inside ``Security.framework``, so it applies equally to the ``keyring`` reads in
:mod:`~macos_vault_plugin.keychain` and the ``SecItemCopyMatching`` reads in
:mod:`~macos_vault_plugin.macos_keychain`, and reaching it through the stdlib
adds no dependency to either.
"""

from __future__ import annotations

import contextlib
import ctypes
import ctypes.util
import functools
from collections.abc import Generator
from typing import Final

#: ``errSecSuccess`` — <Security/SecBase.h>.
_OSSTATUS_OK: Final[int] = 0

_SECURITY_FRAMEWORK: Final[str] = "Security"


@functools.cache
def _security_framework() -> ctypes.CDLL:
    """Load ``Security.framework`` once per process, with prototypes bound.

    Raises:
        RuntimeError: the framework is absent — this vault substrate is
            macOS-only per ``[[solet-locality]]`` and has no fallback.
    """
    path = ctypes.util.find_library(_SECURITY_FRAMEWORK)
    if path is None:
        raise RuntimeError(
            "macos_vault_plugin.keychain_interaction: Security.framework not "
            "found. This vault substrate is macOS-only; there is no non-macOS "
            "fallback path.",
        )
    framework = ctypes.CDLL(path)
    framework.SecKeychainSetUserInteractionAllowed.restype = ctypes.c_int32
    framework.SecKeychainSetUserInteractionAllowed.argtypes = (ctypes.c_ubyte,)
    framework.SecKeychainGetUserInteractionAllowed.restype = ctypes.c_int32
    framework.SecKeychainGetUserInteractionAllowed.argtypes = (
        ctypes.POINTER(ctypes.c_ubyte),
    )
    return framework


def user_interaction_allowed() -> bool:
    """Whether this process may currently be shown a Keychain prompt.

    Raises:
        RuntimeError: the Security framework reported a non-zero ``OSStatus``.
    """
    state = ctypes.c_ubyte()
    status = _security_framework().SecKeychainGetUserInteractionAllowed(
        ctypes.byref(state),
    )
    if status != _OSSTATUS_OK:
        raise RuntimeError(
            "macos_vault_plugin.keychain_interaction: "
            f"SecKeychainGetUserInteractionAllowed failed (OSStatus {status}).",
        )
    return state.value != 0


def set_user_interaction_allowed(allowed: bool) -> None:
    """Allow or forbid Keychain prompts for this process.

    Raises:
        RuntimeError: the Security framework reported a non-zero ``OSStatus``.
    """
    status = _security_framework().SecKeychainSetUserInteractionAllowed(
        1 if allowed else 0,
    )
    if status != _OSSTATUS_OK:
        raise RuntimeError(
            "macos_vault_plugin.keychain_interaction: "
            f"SecKeychainSetUserInteractionAllowed({allowed}) failed "
            f"(OSStatus {status}).",
        )


@contextlib.contextmanager
def user_interaction_disallowed() -> Generator[None]:
    """Run the body with Keychain prompts forbidden, then restore the prior state.

    An unauthorized read inside this block returns ``errSecAuthFailed`` at once
    instead of blocking on a dialog. The previous value is restored even when
    the body raises, so a failed daemon read never disarms an operator tool
    running later in the same process.
    """
    previous = user_interaction_allowed()
    set_user_interaction_allowed(False)
    try:
        yield
    finally:
        set_user_interaction_allowed(previous)


def read_gate(allow_user_interaction: bool) -> contextlib.AbstractContextManager[None]:
    """The context manager a credential read should run under.

    Args:
        allow_user_interaction: ``True`` for operator-run tooling that must be
            able to answer a prompt; ``False`` (the daemon default) to fail fast
            instead of blocking.
    """
    if allow_user_interaction:
        return contextlib.nullcontext()
    return user_interaction_disallowed()


__all__ = [
    "read_gate",
    "set_user_interaction_allowed",
    "user_interaction_allowed",
    "user_interaction_disallowed",
]
