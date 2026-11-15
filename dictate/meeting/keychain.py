"""Store the cloud API key in the macOS login Keychain.

Uses the Security framework directly (ctypes) instead of the `security`
command: a key passed on a command line is visible to every process on the
Mac through `ps` for as long as the command runs.

The key is encrypted at rest by macOS and readable only by this user. It is
never written to config.toml (which is in git), a log, or an error message.
"""

import ctypes
import ctypes.util

SERVICE = "Dictate Meeting Mode"
ACCOUNT = "api-key"

_ERR_NOT_FOUND = -25300  # errSecItemNotFound

_sec = ctypes.CDLL(ctypes.util.find_library("Security"))
_cf = ctypes.CDLL(ctypes.util.find_library("CoreFoundation"))

_sec.SecKeychainFindGenericPassword.argtypes = [
    ctypes.c_void_p, ctypes.c_uint32, ctypes.c_char_p,
    ctypes.c_uint32, ctypes.c_char_p,
    ctypes.POINTER(ctypes.c_uint32), ctypes.POINTER(ctypes.c_void_p),
    ctypes.POINTER(ctypes.c_void_p)]
_sec.SecKeychainFindGenericPassword.restype = ctypes.c_int32
_sec.SecKeychainAddGenericPassword.argtypes = [
    ctypes.c_void_p, ctypes.c_uint32, ctypes.c_char_p,
    ctypes.c_uint32, ctypes.c_char_p,
    ctypes.c_uint32, ctypes.c_char_p, ctypes.POINTER(ctypes.c_void_p)]
_sec.SecKeychainAddGenericPassword.restype = ctypes.c_int32
_sec.SecKeychainItemModifyAttributesAndData.argtypes = [
    ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32, ctypes.c_char_p]
_sec.SecKeychainItemModifyAttributesAndData.restype = ctypes.c_int32
_sec.SecKeychainItemDelete.argtypes = [ctypes.c_void_p]
_sec.SecKeychainItemDelete.restype = ctypes.c_int32
_sec.SecKeychainItemFreeContent.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
_sec.SecKeychainItemFreeContent.restype = ctypes.c_int32
_cf.CFRelease.argtypes = [ctypes.c_void_p]
_cf.CFRelease.restype = None


class KeychainError(Exception):
    """Carries only an OS status code — never any part of the key."""


def _find(service: bytes, account: bytes, want_item: bool):
    length = ctypes.c_uint32(0)
    data = ctypes.c_void_p()
    item = ctypes.c_void_p()
    status = _sec.SecKeychainFindGenericPassword(
        None, len(service), service, len(account), account,
        ctypes.byref(length), ctypes.byref(data),
        ctypes.byref(item) if want_item else None)
    if status == _ERR_NOT_FOUND:
        return None, None
    if status != 0:
        raise KeychainError(f"Keychain read failed (status {status})")
    try:
        secret = ctypes.string_at(data, length.value).decode("utf-8", "replace")
    finally:
        _sec.SecKeychainItemFreeContent(None, data)
    return secret, (item if want_item else None)


def get_key(service: str = SERVICE, account: str = ACCOUNT) -> str | None:
    secret, _ = _find(service.encode(), account.encode(), want_item=False)
    return secret or None


def set_key(key: str, service: str = SERVICE, account: str = ACCOUNT) -> None:
    key = key.strip()
    if not key or any(c.isspace() for c in key):
        raise ValueError("an API key cannot be empty or contain spaces")
    svc, acct, secret = service.encode(), account.encode(), key.encode()
    _, item = _find(svc, acct, want_item=True)
    if item is not None:
        try:
            status = _sec.SecKeychainItemModifyAttributesAndData(
                item, None, len(secret), secret)
        finally:
            _cf.CFRelease(item)
    else:
        status = _sec.SecKeychainAddGenericPassword(
            None, len(svc), svc, len(acct), acct, len(secret), secret, None)
    if status != 0:
        raise KeychainError(f"Keychain write failed (status {status})")


def delete_key(service: str = SERVICE, account: str = ACCOUNT) -> bool:
    _, item = _find(service.encode(), account.encode(), want_item=True)
    if item is None:
        return False
    try:
        status = _sec.SecKeychainItemDelete(item)
    finally:
        _cf.CFRelease(item)
    if status != 0:
        raise KeychainError(f"Keychain delete failed (status {status})")
    return True
