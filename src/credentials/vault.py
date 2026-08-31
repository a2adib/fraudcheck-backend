"""
AES-256-GCM credential vault (FR-2.2, FR-2.3).

Courier passwords are the one secret in this system that must be *recoverable* — the
service replays them at every login — so they are encrypted rather than hashed. GCM is
authenticated encryption: a tampered ciphertext fails to decrypt rather than yielding
garbage plaintext that then gets posted to a courier portal.

Two details worth stating explicitly:

**The nonce is stored with the ciphertext.** A fresh 12-byte nonce per encryption,
prepended to the ciphertext and base64'd together. Nonce reuse under one key is the
classic way to break GCM, so it is never derived from anything.

**Every ciphertext is bound to its row.** The tenant, provider and field travel as
GCM's additional authenticated data. Moving a ciphertext to another merchant's row —
by a bug or by hand in the database — makes it undecryptable rather than usable, so
possession of the blob is not possession of the secret.
"""

import base64
import logging
import secrets

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from src.config import settings
from src.logistics.enums import ProviderEnum

logger = logging.getLogger(__name__)

NONCE_BYTES = 12


class VaultError(Exception):
    """Raised when a ciphertext cannot be decrypted with the key it claims."""


def build_aad(user_public_id: str, provider: ProviderEnum, field: str) -> bytes:
    """Build the context a ciphertext is cryptographically bound to."""
    return f"{user_public_id}:{provider.value}:{field}".encode()


def _key_for_version(key_version: int) -> bytes:
    """
    Resolve a key version to key material (FR-2.3).

    Only the current version exists today. The indirection is the point: rotation means
    adding a version here and re-encrypting on next use, not a migration that has to
    decrypt every row with a key the new code no longer has.
    """
    if key_version != settings.ENCRYPTION_KEY_VERSION:
        msg = f"No key material for key_version {key_version}"
        raise VaultError(msg)
    return settings.encryption_key_bytes


def encrypt(plaintext: str, aad: bytes) -> str:
    """Encrypt to ``base64(nonce || ciphertext || tag)`` under the current key."""
    nonce = secrets.token_bytes(NONCE_BYTES)
    ciphertext = AESGCM(_key_for_version(settings.ENCRYPTION_KEY_VERSION)).encrypt(
        nonce, plaintext.encode(), aad
    )
    return base64.b64encode(nonce + ciphertext).decode()


def decrypt(stored: str, aad: bytes, key_version: int) -> str:
    """
    Decrypt a stored blob.

    Raises:
        VaultError: if the key version is unknown, the blob is malformed, or the
            authentication tag does not match — which includes the case of a ciphertext
            that belongs to a different tenant, provider or field.

    """
    try:
        raw = base64.b64decode(stored)
    except (ValueError, TypeError) as exc:
        msg = "Stored credential is not valid base64"
        raise VaultError(msg) from exc

    if len(raw) <= NONCE_BYTES:
        msg = "Stored credential is too short to contain a nonce"
        raise VaultError(msg)

    nonce, ciphertext = raw[:NONCE_BYTES], raw[NONCE_BYTES:]
    try:
        return AESGCM(_key_for_version(key_version)).decrypt(nonce, ciphertext, aad).decode()
    except InvalidTag as exc:
        # Deliberately vague: the failure mode is identical whether the key is wrong,
        # the blob was tampered with, or it was lifted from another merchant's row.
        msg = "Stored credential failed authentication"
        raise VaultError(msg) from exc


def mask_username(username: str) -> str:
    """
    Mask a username for display: ``abcdef@gmail.com`` -> ``ab***@gmail.com`` (AC-2.1).

    The domain survives because a merchant needs to recognise *which* account they
    stored; the local part is what identifies it.
    """
    local, separator, domain = username.partition("@")
    visible = 2
    masked_local = f"{local[:visible]}***" if len(local) > visible else "*" * len(local)
    if separator:
        return f"{masked_local}@{domain}"
    return masked_local
