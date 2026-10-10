import base64
import ctypes
import hashlib
import hmac
import os
import subprocess
import sys
import threading
from typing import Callable, Optional


class SecretResolutionError(ValueError):
    pass


class SecretResolver:
    def __init__(
        self,
        runner: Optional[Callable] = None,
        timeout_seconds: int = 5,
    ) -> None:
        self._runner = subprocess.run if runner is None else runner
        self._timeout_seconds = timeout_seconds

    def resolve(self, reference: Optional[str], env_fallback: Optional[str]) -> str:
        if not reference:
            return (env_fallback or "").strip()

        parts = reference.split(":")
        if len(parts) != 3 or parts[0] != "keychain":
            raise SecretResolutionError("invalid media secret reference")
        service = parts[1].strip()
        account = parts[2].strip()
        if not service or not account:
            raise SecretResolutionError("invalid media secret reference")
        command = [
            "/usr/bin/security",
            "find-generic-password",
            "-w",
            "-s",
            service,
            "-a",
            account,
        ]
        runner_failed = False
        try:
            result = self._runner(
                command,
                check=True,
                capture_output=True,
                text=True,
                timeout=self._timeout_seconds,
                shell=False,
            )
        except Exception:
            runner_failed = True
        if runner_failed:
            raise SecretResolutionError("media secret resolution failed") from None
        value = (getattr(result, "stdout", "") or "").strip()
        if not value:
            raise SecretResolutionError("media secret resolution failed")
        return value


class BackupKeyProvider:
    SERVICE = "qq-openclaw-backup"
    ACCOUNT = "backup-master-v1"

    def __init__(
        self,
        runner: Optional[Callable] = None,
        random_bytes: Optional[Callable[[int], bytes]] = None,
        timeout_seconds: int = 5,
    ) -> None:
        self._runner = subprocess.run if runner is None else runner
        self._native_keychain = (
            _MacOSKeychain(self.SERVICE, self.ACCOUNT)
            if runner is None and sys.platform == "darwin"
            else None
        )
        self._random_bytes = os.urandom if random_bytes is None else random_bytes
        self._timeout_seconds = timeout_seconds
        self._lock = threading.Lock()
        self._cached_key = None

    def get_or_create(self) -> bytes:
        with self._lock:
            if self._cached_key is not None:
                return self._cached_key
            key = self._get_or_create_uncached()
            self._cached_key = key
            return key

    def _get_or_create_uncached(self) -> bytes:
        status, existing = self._read_existing()
        if status == "found":
            assert existing is not None
            return existing
        if status != "missing":
            raise SecretResolutionError("backup key resolution failed")

        random_failed = False
        generated = b""
        try:
            generated = self._random_bytes(32)
        except Exception:
            random_failed = True
        if random_failed:
            raise SecretResolutionError("backup key resolution failed") from None
        if not isinstance(generated, bytes) or len(generated) != 32:
            raise SecretResolutionError("backup key resolution failed")
        encoded = base64.b64encode(generated).decode("ascii")
        add_command = [
            "/usr/bin/security",
            "add-generic-password",
            "-s",
            self.SERVICE,
            "-a",
            self.ACCOUNT,
            "-w",
        ]
        add_failed = False
        add_result = None
        try:
            if self._native_keychain is not None:
                add_result = self._native_keychain.add(generated)
            else:
                add_result = self._runner(
                    add_command,
                    check=False,
                    capture_output=True,
                    text=True,
                    input=encoded + "\n",
                    timeout=self._timeout_seconds,
                    shell=False,
                )
        except Exception:
            add_failed = True
        add_succeeded = (
            add_result == "created"
            if self._native_keychain is not None
            else int(getattr(add_result, "returncode", 0)) == 0
        )
        if not add_failed and add_succeeded:
            status, persisted = self._read_existing()
            if status == "found" and persisted == generated:
                return persisted
            raise SecretResolutionError("backup key resolution failed")

        status, winner = self._read_existing()
        if status == "found":
            assert winner is not None
            return winner
        raise SecretResolutionError("backup key resolution failed") from None

    def _read_existing(self):
        if self._native_keychain is not None:
            return self._native_keychain.read()
        find_command = [
            "/usr/bin/security",
            "find-generic-password",
            "-w",
            "-s",
            self.SERVICE,
            "-a",
            self.ACCOUNT,
        ]
        find_failed = False
        try:
            result = self._runner(
                find_command,
                check=False,
                capture_output=True,
                text=True,
                timeout=self._timeout_seconds,
                shell=False,
            )
        except Exception:
            find_failed = True
            result = None
        if find_failed:
            return "failed", None

        returncode = int(getattr(result, "returncode", 0))
        if returncode == 0:
            key = _decode_backup_key(getattr(result, "stdout", ""))
            if key is None:
                return "failed", None
            return "found", key
        if returncode == 44:
            return "missing", None
        return "failed", None


class ApprovalBindingKeyProvider:
    """Derives the approval HMAC key from the Keychain-backed master key."""

    _DOMAIN = b"qq-openclaw/approval-binding/v1"

    def __init__(self, backup_key_provider: Optional[BackupKeyProvider] = None) -> None:
        self._backup_key_provider = backup_key_provider or BackupKeyProvider()

    def get(self) -> bytes:
        try:
            master_key = self._backup_key_provider.get_or_create()
        except Exception:
            raise SecretResolutionError("approval binding key resolution failed") from None
        if type(master_key) is not bytes or len(master_key) != 32:
            raise SecretResolutionError("approval binding key resolution failed")
        return hmac.new(master_key, self._DOMAIN, hashlib.sha256).digest()


def _decode_backup_key(value: object) -> Optional[bytes]:
    try:
        decoded = base64.b64decode(str(value).strip(), validate=True)
    except Exception:
        return None
    return decoded if len(decoded) == 32 else None


class _MacOSKeychain:
    _SUCCESS = 0
    _DUPLICATE_ITEM = -25299
    _ITEM_NOT_FOUND = -25300

    def __init__(self, service: str, account: str) -> None:
        self._service = service.encode("utf-8")
        self._account = account.encode("utf-8")
        self._security = ctypes.CDLL(
            "/System/Library/Frameworks/Security.framework/Security"
        )
        self._configure_functions()

    def read(self):
        length = ctypes.c_uint32()
        data = ctypes.c_void_p()
        status = self._security.SecKeychainFindGenericPassword(
            None,
            len(self._service),
            self._service,
            len(self._account),
            self._account,
            ctypes.byref(length),
            ctypes.byref(data),
            None,
        )
        if status == self._ITEM_NOT_FOUND:
            return "missing", None
        if status != self._SUCCESS:
            return "failed", None
        try:
            stored = ctypes.string_at(data, length.value)
        finally:
            self._security.SecKeychainItemFreeContent(None, data)
        if len(stored) == 32:
            return "found", stored
        decoded = _decode_backup_key(stored.decode("ascii", errors="ignore"))
        return ("found", decoded) if decoded is not None else ("failed", None)

    def add(self, key: bytes) -> str:
        buffer = ctypes.create_string_buffer(key)
        status = self._security.SecKeychainAddGenericPassword(
            None,
            len(self._service),
            self._service,
            len(self._account),
            self._account,
            len(key),
            ctypes.cast(buffer, ctypes.c_void_p),
            None,
        )
        if status == self._SUCCESS:
            return "created"
        if status == self._DUPLICATE_ITEM:
            return "duplicate"
        return "failed"

    def _configure_functions(self) -> None:
        find = self._security.SecKeychainFindGenericPassword
        find.argtypes = (
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.c_char_p,
            ctypes.c_uint32,
            ctypes.c_char_p,
            ctypes.POINTER(ctypes.c_uint32),
            ctypes.POINTER(ctypes.c_void_p),
            ctypes.POINTER(ctypes.c_void_p),
        )
        find.restype = ctypes.c_int32
        add = self._security.SecKeychainAddGenericPassword
        add.argtypes = (
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.c_char_p,
            ctypes.c_uint32,
            ctypes.c_char_p,
            ctypes.c_uint32,
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_void_p),
        )
        add.restype = ctypes.c_int32
        free = self._security.SecKeychainItemFreeContent
        free.argtypes = (ctypes.c_void_p, ctypes.c_void_p)
        free.restype = ctypes.c_int32


__all__ = [
    "ApprovalBindingKeyProvider",
    "BackupKeyProvider",
    "SecretResolutionError",
    "SecretResolver",
]
