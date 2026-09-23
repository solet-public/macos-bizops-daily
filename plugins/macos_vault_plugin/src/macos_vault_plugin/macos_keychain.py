"""macOS Security framework lookup for generic-password keychain entries.

Searches BOTH the iCloud Keychain (Apple Passwords app, macOS Sequoia 15+) and the
legacy login.keychain via direct calls to ``Security.framework`` through PyObjC.

The legacy ``security`` CLI — and therefore the ``keyring`` library that wraps it on
Darwin — only sees ``login.keychain-db`` and ``System.keychain``. It does NOT see
items stored in iCloud Keychain. Apple's Security framework, however, exposes a
single ``SecItemCopyMatching`` API that can match either keychain when the query
sets ``kSecAttrSynchronizable`` to ``kSecAttrSynchronizableAny``.

Reference: https://developer.apple.com/documentation/security/ksecattrsynchronizable
"""

from __future__ import annotations

import sys
from typing import Any, Final

# OSStatus codes returned by SecItemCopyMatching.
# Source: <Security/SecBase.h>; mirrored in pyobjc-framework-Security as constants.
_ERR_SEC_SUCCESS: Final[int] = 0
_ERR_SEC_ITEM_NOT_FOUND: Final[int] = -25300

_DARWIN_PLATFORM: Final[str] = "darwin"
_NON_DARWIN_MESSAGE: Final[str] = (
    "macos_keychain.get_password is only available on Darwin; "
    "callers must gate by sys.platform first."
)


def get_password(service: str, account: str) -> str | None:
    """Look up a generic password in macOS keychains.

    Searches iCloud Keychain (Apple Passwords) AND the legacy login.keychain by
    issuing two queries via ``SecItemCopyMatching``:

    1. ``kSecAttrSynchronizable = kSecAttrSynchronizableAny`` — matches items in
       either keychain. This is Apple's documented "match either iCloud or
       local" sentinel and on its own is sufficient for the common case.
    2. ``kSecAttrSynchronizable = False`` — defensive fallback for entries that
       are explicitly flagged as non-synchronizable, in case the ``Any`` mode
       somehow fails to find them. Belt-and-suspenders.

    Returns the matching password decoded as UTF-8, or ``None`` if neither query
    finds a match. Any other ``OSStatus`` (auth denied, malformed query, etc.)
    is raised as ``RuntimeError`` with the numeric status in the message.

    On non-Darwin platforms, raises ``RuntimeError`` — callers must gate by
    ``sys.platform == "darwin"`` before calling.
    """
    if sys.platform != _DARWIN_PLATFORM:
        raise RuntimeError(_NON_DARWIN_MESSAGE)

    import Security

    found = _query_keychain(Security, service, account, Security.kSecAttrSynchronizableAny)
    if found is not None:
        return found

    return _query_keychain(Security, service, account, False)


def _query_keychain(
    security_module: Any,
    service: str,
    account: str,
    synchronizable: Any,
) -> str | None:
    """Issue a single ``SecItemCopyMatching`` query and decode its result.

    Returns the password string on success, ``None`` on ``errSecItemNotFound``,
    and raises ``RuntimeError`` for any other ``OSStatus``.
    """
    query = {
        security_module.kSecClass: security_module.kSecClassGenericPassword,
        security_module.kSecAttrService: service,
        security_module.kSecAttrAccount: account,
        security_module.kSecAttrSynchronizable: synchronizable,
        security_module.kSecMatchLimit: security_module.kSecMatchLimitOne,
        security_module.kSecReturnData: True,
    }
    status, data = security_module.SecItemCopyMatching(query, None)

    if status == _ERR_SEC_SUCCESS:
        if data is None:
            return None
        return bytes(data).decode("utf-8")

    if status == _ERR_SEC_ITEM_NOT_FOUND:
        return None

    raise RuntimeError(
        f"SecItemCopyMatching failed with OSStatus {status} "
        f"(service={service!r} account={account!r})."
    )
