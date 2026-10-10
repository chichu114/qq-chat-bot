import os

from cryptography.hazmat.primitives.ciphers.aead import AESGCM


_MAGIC = b"CHICHU-BACKUP\x00"
_VERSION = 1
_NONCE_BYTES = 12
_TAG_BYTES = 16
_HEADER = _MAGIC + bytes((_VERSION,))


class BackupIntegrityError(ValueError):
    pass


class BackupEnvelope:
    @staticmethod
    def encrypt(plaintext: bytes, key: bytes) -> bytes:
        if not isinstance(plaintext, bytes) or not _valid_key(key):
            raise BackupIntegrityError("backup integrity check failed")
        failed = False
        nonce = b""
        ciphertext = b""
        try:
            nonce = os.urandom(_NONCE_BYTES)
            ciphertext = AESGCM(key).encrypt(nonce, plaintext, _HEADER)
        except Exception:
            failed = True
        if failed:
            raise BackupIntegrityError("backup integrity check failed") from None
        return _HEADER + nonce + ciphertext

    @staticmethod
    def decrypt(ciphertext: bytes, key: bytes) -> bytes:
        minimum_size = len(_HEADER) + _NONCE_BYTES + _TAG_BYTES
        if (
            not isinstance(ciphertext, bytes)
            or not _valid_key(key)
            or len(ciphertext) < minimum_size
            or not ciphertext.startswith(_MAGIC)
            or ciphertext[len(_MAGIC)] != _VERSION
        ):
            raise BackupIntegrityError("backup integrity check failed")
        nonce_start = len(_HEADER)
        nonce_end = nonce_start + _NONCE_BYTES
        nonce = ciphertext[nonce_start:nonce_end]
        encrypted_payload = ciphertext[nonce_end:]
        failed = False
        plaintext = b""
        try:
            plaintext = AESGCM(key).decrypt(nonce, encrypted_payload, _HEADER)
        except Exception:
            failed = True
        if failed:
            raise BackupIntegrityError("backup integrity check failed") from None
        return plaintext


def _valid_key(key: bytes) -> bool:
    return isinstance(key, bytes) and len(key) == 32


__all__ = ["BackupEnvelope", "BackupIntegrityError"]
