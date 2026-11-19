"""Keep the API key in Windows Credential Manager.

The key is encrypted by Windows for this account only. It is never written
to settings.toml, a log, or an error message, and it is only sent to the
host it was saved for (the target name includes the host).
"""

import ctypes
import sys

PREFIX = "Dictate/"


class CredentialError(Exception):
    """Safe to show: never contains the key."""


def _target(host: str) -> str:
    return f"{PREFIX}{host}"


def get_key(host: str) -> str | None:
    from dictate_win import winapi as api

    found = api.PCREDENTIALW()
    if not api.CredReadW(_target(host), api.CRED_TYPE_GENERIC, 0, ctypes.byref(found)):
        error = api.last_error()
        if error == api.ERROR_NOT_FOUND:
            return None
        raise CredentialError(f"Credential Manager could not be read (error {error})")
    try:
        cred = found.contents
        blob = ctypes.string_at(cred.CredentialBlob, cred.CredentialBlobSize)
        return blob.decode("utf-8") or None
    finally:
        api.CredFree(found)


def set_key(host: str, key: str) -> None:
    from dictate_win import winapi as api

    key = (key or "").strip()
    if not key:
        raise CredentialError("the key is empty")
    data = key.encode("utf-8")
    blob = (ctypes.c_ubyte * len(data)).from_buffer_copy(data)
    cred = api.CREDENTIALW()
    cred.Type = api.CRED_TYPE_GENERIC
    cred.TargetName = _target(host)
    cred.Comment = "API key used by Dictate"
    cred.CredentialBlobSize = len(data)
    cred.CredentialBlob = ctypes.cast(blob, ctypes.POINTER(ctypes.c_ubyte))
    cred.Persist = api.CRED_PERSIST_LOCAL_MACHINE
    cred.UserName = "Dictate"
    if not api.CredWriteW(ctypes.byref(cred), 0):
        raise CredentialError(
            f"Credential Manager refused to save the key (error {api.last_error()})")


def delete_key(host: str) -> bool:
    """True if a key was removed."""
    from dictate_win import winapi as api

    if api.CredDeleteW(_target(host), api.CRED_TYPE_GENERIC, 0):
        return True
    error = api.last_error()
    if error == api.ERROR_NOT_FOUND:
        return False
    raise CredentialError(f"Credential Manager could not delete the key (error {error})")


if __name__ == "__main__":
    # Used by uninstall.ps1: python -m dictate_win.credentials --forget HOST
    if len(sys.argv) == 3 and sys.argv[1] == "--forget":
        print("removed" if delete_key(sys.argv[2]) else "nothing saved")
