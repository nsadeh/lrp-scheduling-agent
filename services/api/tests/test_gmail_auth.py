"""Unit tests for Gmail token store encryption."""

from cryptography.fernet import Fernet

from api.gmail.auth import TokenStore


class TestTokenStoreEncryption:
    """Test encrypt/decrypt round-trip without Postgres."""

    def test_encrypt_decrypt_roundtrip(self):
        key = Fernet.generate_key()
        # We can test the encrypt/decrypt methods directly by constructing
        # a TokenStore with a dummy pool (we won't call DB methods)
        store = TokenStore(db_pool=None, encryption_key=key)
        original = "1//0abc-refresh-token-xyz"
        encrypted = store._encrypt(original)
        assert isinstance(encrypted, bytes)
        assert encrypted != original.encode()
        decrypted = store._decrypt(encrypted)
        assert decrypted == original

    def test_different_keys_fail(self):
        key1 = Fernet.generate_key()
        key2 = Fernet.generate_key()
        store1 = TokenStore(db_pool=None, encryption_key=key1)
        store2 = TokenStore(db_pool=None, encryption_key=key2)

        encrypted = store1._encrypt("secret-token")
        import pytest

        from api.gmail.exceptions import GmailAuthError

        with pytest.raises(GmailAuthError, match="wrong encryption key"):
            store2._decrypt(encrypted)
