"""FR-2.2/2.3 — the AES-256-GCM vault itself, with no HTTP in the way."""

import base64

import pytest

from src.config import settings
from src.credentials.vault import VaultError, build_aad, decrypt, encrypt, mask_username
from src.logistics.enums import ProviderEnum

SECRET = "s3cr3t-courier-password"
TENANT = "abcdefghijk"


def aad(
    tenant: str = TENANT, provider: ProviderEnum = ProviderEnum.PATHAO, field: str = "password"
):
    return build_aad(tenant, provider, field)


class TestRoundTrip:
    def test_encrypt_then_decrypt_returns_the_original(self):
        assert decrypt(encrypt(SECRET, aad()), aad(), settings.ENCRYPTION_KEY_VERSION) == SECRET

    def test_ac_2_2_ciphertext_does_not_contain_the_plaintext(self):
        """AC-2.2 — what lands in Postgres is unreadable."""
        stored = encrypt(SECRET, aad())

        assert SECRET not in stored
        assert SECRET.encode() not in base64.b64decode(stored)

    def test_every_encryption_uses_a_fresh_nonce(self):
        """Identical plaintexts must not produce identical ciphertexts."""
        assert encrypt(SECRET, aad()) != encrypt(SECRET, aad())


class TestBinding:
    def test_a_ciphertext_cannot_be_moved_to_another_tenant(self):
        """The row a ciphertext belongs to is authenticated, not just stored beside it."""
        stored = encrypt(SECRET, aad())

        with pytest.raises(VaultError):
            decrypt(stored, aad(tenant="zzzzzzzzzzz"), settings.ENCRYPTION_KEY_VERSION)

    def test_a_ciphertext_cannot_be_moved_to_another_provider(self):
        stored = encrypt(SECRET, aad(provider=ProviderEnum.PATHAO))

        with pytest.raises(VaultError):
            decrypt(stored, aad(provider=ProviderEnum.REDX), settings.ENCRYPTION_KEY_VERSION)

    def test_the_username_blob_cannot_be_read_as_the_password(self):
        stored = encrypt(SECRET, aad(field="username"))

        with pytest.raises(VaultError):
            decrypt(stored, aad(field="password"), settings.ENCRYPTION_KEY_VERSION)


class TestKeyVersion:
    def test_ac_2_3_an_unknown_key_version_fails_loudly(self):
        """AC-2.3 — the version is what rotation will hang off, so it is enforced now."""
        stored = encrypt(SECRET, aad())

        with pytest.raises(VaultError):
            decrypt(stored, aad(), settings.ENCRYPTION_KEY_VERSION + 1)

    def test_a_tampered_ciphertext_is_rejected(self):
        stored = bytearray(base64.b64decode(encrypt(SECRET, aad())))
        stored[-1] ^= 0xFF

        with pytest.raises(VaultError):
            decrypt(
                base64.b64encode(bytes(stored)).decode(), aad(), settings.ENCRYPTION_KEY_VERSION
            )

    def test_a_malformed_blob_is_rejected(self):
        with pytest.raises(VaultError):
            decrypt("not-base64!!", aad(), settings.ENCRYPTION_KEY_VERSION)


class TestMasking:
    @pytest.mark.parametrize(
        ("username", "expected"),
        [
            ("abcdef@gmail.com", "ab***@gmail.com"),
            ("a@gmail.com", "*@gmail.com"),
            ("merchant-account", "me***"),
            ("ab", "**"),
        ],
    )
    def test_ac_2_1_username_is_masked_for_display(self, username: str, expected: str):
        """AC-2.1 — enough to recognise the account, not enough to reuse it."""
        assert mask_username(username) == expected
