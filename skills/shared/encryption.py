"""
Encryption at rest for sensitive financial data.
Uses Fernet symmetric encryption (AES-128-CBC with HMAC-SHA256).
"""
import os
import base64
import hashlib
from typing import Optional

from cryptography.fernet import Fernet, InvalidToken

from .config import get_logger

logger = get_logger("encryption")


class EncryptionManager:
    """Manages encryption/decryption for sensitive data at rest."""

    def __init__(self, key: Optional[str] = None):
        raw_key = key or os.getenv("DATA_ENCRYPTION_KEY")
        if not raw_key:
            logger.warning(
                "DATA_ENCRYPTION_KEY not set — generating ephemeral key. "
                "Data encrypted in this session cannot be decrypted later! "
                "Set DATA_ENCRYPTION_KEY in .env for persistent encryption."
            )
            self._fernet = Fernet(Fernet.generate_key())
            self._is_ephemeral = True
        else:
            # Derive a proper Fernet key from the user-provided key
            derived = hashlib.sha256(raw_key.encode()).digest()
            fernet_key = base64.urlsafe_b64encode(derived)
            self._fernet = Fernet(fernet_key)
            self._is_ephemeral = False

    @property
    def is_ephemeral(self) -> bool:
        return self._is_ephemeral

    def encrypt(self, plaintext: str) -> str:
        """Encrypt a string. Returns base64-encoded ciphertext."""
        if not plaintext:
            return ""
        token = self._fernet.encrypt(plaintext.encode("utf-8"))
        return token.decode("utf-8")

    def decrypt(self, ciphertext: str) -> str:
        """Decrypt a base64-encoded ciphertext string."""
        if not ciphertext:
            return ""
        try:
            plaintext = self._fernet.decrypt(ciphertext.encode("utf-8"))
            return plaintext.decode("utf-8")
        except InvalidToken:
            logger.error("Decryption failed — wrong key or corrupted data")
            return "[DECRYPTION_FAILED]"

    def rotate_key(self, old_key: str, new_key: str, ciphertext: str) -> str:
        """Re-encrypt data with a new key."""
        old_mgr = EncryptionManager(key=old_key)
        new_mgr = EncryptionManager(key=new_key)
        plaintext = old_mgr.decrypt(ciphertext)
        if plaintext == "[DECRYPTION_FAILED]":
            raise ValueError("Cannot rotate — decryption with old key failed")
        return new_mgr.encrypt(plaintext)
