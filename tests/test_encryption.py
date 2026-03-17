"""Tests for encryption at rest."""
import pytest
from skills.shared.encryption import EncryptionManager


class TestEncryption:
    def test_encrypt_decrypt_roundtrip(self, encryption_manager):
        plaintext = "Credit card: 4111-1111-1111-1234"
        ciphertext = encryption_manager.encrypt(plaintext)

        assert ciphertext != plaintext
        assert encryption_manager.decrypt(ciphertext) == plaintext

    def test_empty_string(self, encryption_manager):
        assert encryption_manager.encrypt("") == ""
        assert encryption_manager.decrypt("") == ""

    def test_unicode_content(self, encryption_manager):
        text = "支付金额: ¥5,000 — Tōkyō office"
        encrypted = encryption_manager.encrypt(text)
        assert encryption_manager.decrypt(encrypted) == text

    def test_different_keys_cannot_decrypt(self):
        mgr1 = EncryptionManager(key="key-one")
        mgr2 = EncryptionManager(key="key-two")

        ciphertext = mgr1.encrypt("secret data")
        result = mgr2.decrypt(ciphertext)
        assert result == "[DECRYPTION_FAILED]"

    def test_same_key_produces_different_ciphertexts(self, encryption_manager):
        """Fernet includes a timestamp, so same input → different output."""
        ct1 = encryption_manager.encrypt("test")
        ct2 = encryption_manager.encrypt("test")
        assert ct1 != ct2  # Different due to timestamp/IV

    def test_key_rotation(self):
        old_mgr = EncryptionManager(key="old-key")
        ciphertext = old_mgr.encrypt("sensitive financial data")

        new_mgr = EncryptionManager(key="old-key")
        rotated = new_mgr.encrypt(new_mgr.decrypt(ciphertext))

        # New key can decrypt rotated data
        assert new_mgr.decrypt(rotated) == "sensitive financial data"

    def test_ephemeral_key_warning(self):
        """When no key is set, manager should be ephemeral."""
        import os
        old_key = os.environ.pop("DATA_ENCRYPTION_KEY", None)
        try:
            mgr = EncryptionManager(key=None)
            assert mgr.is_ephemeral
        finally:
            if old_key:
                os.environ["DATA_ENCRYPTION_KEY"] = old_key

    def test_long_content(self, encryption_manager):
        """Test with large content (contract-sized)."""
        large_text = "x" * 100_000
        encrypted = encryption_manager.encrypt(large_text)
        assert encryption_manager.decrypt(encrypted) == large_text
