from __future__ import annotations

import ctypes
import hmac
import sys

SERVICE = "com.cc.quota-fetcher.deepseek-api-key"
ACCOUNT = "default"
ERR_SEC_SUCCESS = 0
ERR_SEC_DUPLICATE_ITEM = -25299
ERR_SEC_ITEM_NOT_FOUND = -25300


class KeychainError(RuntimeError):
    pass


_security = ctypes.CDLL("/System/Library/Frameworks/Security.framework/Security")
_core_foundation = ctypes.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")

_security.SecKeychainFindGenericPassword.argtypes = [
    ctypes.c_void_p,
    ctypes.c_uint32,
    ctypes.c_char_p,
    ctypes.c_uint32,
    ctypes.c_char_p,
    ctypes.POINTER(ctypes.c_uint32),
    ctypes.POINTER(ctypes.c_void_p),
    ctypes.POINTER(ctypes.c_void_p)
]
_security.SecKeychainFindGenericPassword.restype = ctypes.c_int32
_security.SecKeychainAddGenericPassword.argtypes = [
    ctypes.c_void_p,
    ctypes.c_uint32,
    ctypes.c_char_p,
    ctypes.c_uint32,
    ctypes.c_char_p,
    ctypes.c_uint32,
    ctypes.c_void_p,
    ctypes.POINTER(ctypes.c_void_p)
]
_security.SecKeychainAddGenericPassword.restype = ctypes.c_int32
_security.SecKeychainItemModifyAttributesAndData.argtypes = [
    ctypes.c_void_p,
    ctypes.c_void_p,
    ctypes.c_uint32,
    ctypes.c_void_p
]
_security.SecKeychainItemModifyAttributesAndData.restype = ctypes.c_int32
_security.SecKeychainItemFreeContent.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
_security.SecKeychainItemFreeContent.restype = ctypes.c_int32
_core_foundation.CFRelease.argtypes = [ctypes.c_void_p]
_core_foundation.CFRelease.restype = None


def _release_item(item_ref) -> None:
    if item_ref:
        _core_foundation.CFRelease(ctypes.c_void_p(item_ref))


def _find_secret(include_data: bool):
    service = SERVICE.encode("utf-8")
    account = ACCOUNT.encode("utf-8")
    password_length = ctypes.c_uint32()
    password_data = ctypes.c_void_p()
    item_ref = ctypes.c_void_p()
    status = _security.SecKeychainFindGenericPassword(
        None,
        len(service),
        service,
        len(account),
        account,
        ctypes.byref(password_length) if include_data else None,
        ctypes.byref(password_data) if include_data else None,
        ctypes.byref(item_ref)
    )
    secret = None
    if status == ERR_SEC_SUCCESS and include_data:
        try:
            secret = ctypes.string_at(password_data, password_length.value).decode("utf-8")
        except UnicodeDecodeError:
            _release_item(item_ref.value)
            raise KeychainError("keychain-read-failed")
        finally:
            if password_data.value:
                _security.SecKeychainItemFreeContent(None, password_data)
    return status, secret, item_ref.value


def _modify_item(item_ref, secret: str) -> int:
    encoded = secret.encode("utf-8")
    buffer = ctypes.create_string_buffer(encoded)
    return _security.SecKeychainItemModifyAttributesAndData(
        ctypes.c_void_p(item_ref),
        None,
        len(encoded),
        ctypes.cast(buffer, ctypes.c_void_p)
    )


def _add_item(secret: str) -> int:
    service = SERVICE.encode("utf-8")
    account = ACCOUNT.encode("utf-8")
    encoded = secret.encode("utf-8")
    buffer = ctypes.create_string_buffer(encoded)
    item_ref = ctypes.c_void_p()
    status = _security.SecKeychainAddGenericPassword(
        None,
        len(service),
        service,
        len(account),
        account,
        len(encoded),
        ctypes.cast(buffer, ctypes.c_void_p),
        ctypes.byref(item_ref)
    )
    _release_item(item_ref.value)
    return status


def read_secret() -> str | None:
    status, secret, item_ref = _find_secret(include_data=True)
    if status == ERR_SEC_ITEM_NOT_FOUND:
        return None
    if status != ERR_SEC_SUCCESS:
        raise KeychainError("keychain-read-failed")
    _release_item(item_ref)
    return secret


def create_secret_if_absent(secret: str) -> bool:
    status = _add_item(secret)
    if status == ERR_SEC_SUCCESS:
        return True
    if status != ERR_SEC_DUPLICATE_ITEM:
        raise KeychainError("keychain-write-failed")
    existing = read_secret()
    if existing is None:
        raise KeychainError("keychain-read-failed")
    return hmac.compare_digest(existing.encode("utf-8"), secret.encode("utf-8"))


def write_secret(secret: str) -> None:
    status, _, item_ref = _find_secret(include_data=False)
    if status == ERR_SEC_SUCCESS:
        try:
            status = _modify_item(item_ref, secret)
        finally:
            _release_item(item_ref)
    elif status == ERR_SEC_ITEM_NOT_FOUND:
        status = _add_item(secret)
    if status != ERR_SEC_SUCCESS:
        raise KeychainError("keychain-write-failed")


def valid_secret(secret: str) -> bool:
    return 8 <= len(secret) <= 4096 and not any(
        ord(character) < 32 or ord(character) == 127 for character in secret
    )


def main() -> int:
    if sys.argv[1:] == ["--check"]:
        try:
            return 0 if read_secret() is not None else 1
        except KeychainError:
            return 1
    if sys.argv[1:] != ["--set-stdin"]:
        return 2
    secret = sys.stdin.read()
    if secret.endswith("\n"):
        secret = secret[:-1]
    if secret.endswith("\r"):
        secret = secret[:-1]
    if not valid_secret(secret):
        return 2
    try:
        write_secret(secret)
    except KeychainError:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
