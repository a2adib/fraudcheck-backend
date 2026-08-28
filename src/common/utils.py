import hashlib
import secrets


def generate_public_id() -> str:
    """Generate a unique 11-character public id."""
    return secrets.token_urlsafe(nbytes=8)


def generate_otp_token() -> str:
    """Generate a 6-digit OTP."""
    return f"{secrets.randbelow(1000000):06d}"


def generate_opaque_token(nbytes: int = 32) -> str:
    """Generate a high-entropy URL-safe opaque token secret."""
    return secrets.token_urlsafe(nbytes)


def split_prefixed_token(token: str) -> tuple[str, str] | None:
    """Split a ``{public_id}.{secret}`` token. Returns ``None`` if malformed."""
    if "." not in token:
        return None
    prefix, secret = token.split(".", 1)
    if not prefix or not secret:
        return None
    return prefix, secret


def sha256_hex(value: str) -> str:
    """
    Fast, deterministic hash for high-entropy secrets and lookup keys.

    Correct for opaque tokens and API keys, which are 32 random bytes and so have
    no brute-force surface. It is NOT a password hash — use ``get_password_hash``
    for anything a human chose.
    """
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def compute_device_fingerprint(user_agent: str | None, *extra_signals: str) -> str:
    """SHA-256 fingerprint of the user-agent plus any extra stable signals."""
    raw = "|".join([user_agent or "", *extra_signals])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()
