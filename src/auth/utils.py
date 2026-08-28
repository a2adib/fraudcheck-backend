"""
Auth primitives.

Ported from ``erp-backend/src/auth/utils.py`` with one deliberate change: the
``CryptContext`` leads with Argon2id per NFR-2, where upstream uses bcrypt at 12
rounds. Both schemes are registered with ``deprecated="auto"`` so any bcrypt hash
carried over from an upstream export is verified once and transparently rehashed to
Argon2id on the next successful login (FR-1 port note).

Only the hashing half is ported so far — sessions, refresh rotation, OTP reset and
RBAC land with M1 (FR-1).
"""

from passlib.context import CryptContext

MIN_PASSWORD_LENGTH = 10  # FR-1, AC-1.3. Upstream erp-backend uses 8.

pwd_context = CryptContext(schemes=["argon2", "bcrypt"], deprecated="auto")


def get_password_hash(password: str) -> str:
    """Hash a password with Argon2id."""
    # passlib ships no type information (see [[tool.mypy.overrides]] in pyproject),
    # so each result is pinned to a concrete type on the way out.
    hashed: str = pwd_context.hash(password)
    return hashed


def verify_password(plain_password: str, hashed_password: str) -> bool:
    """Verify a password against its hash. Accepts a legacy bcrypt hash."""
    matches: bool = pwd_context.verify(plain_password, hashed_password)
    return matches


def needs_rehash(hashed_password: str) -> bool:
    """Report whether a stored hash uses a deprecated scheme and needs upgrading."""
    stale: bool = pwd_context.needs_update(hashed_password)
    return stale
