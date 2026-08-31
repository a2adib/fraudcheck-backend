"""
Courier-layer failures.

Every one of these is *expected* — a provider being down, slow, or rejecting a
credential is normal operation for this service, not an incident. The orchestrator
maps each to a ``ProviderStatus`` + ``ProviderErrorCode`` and carries on with the
other providers (FR-6.4).
"""

from src.logistics.enums import ProviderErrorCode, ProviderStatus


class ProviderError(Exception):
    """Base for every courier failure. Carries the wire shape the API reports."""

    status: ProviderStatus = ProviderStatus.UNAVAILABLE
    error_code: ProviderErrorCode = ProviderErrorCode.PROVIDER_ERROR


class ProviderTimeout(ProviderError):  # noqa: N818 — the name FR-3/AC-3.5 specifies
    """The provider did not answer within the adapter's declared timeout (AC-3.5)."""

    status = ProviderStatus.TIMEOUT
    error_code = ProviderErrorCode.TIMEOUT


class ProviderAuthError(ProviderError):
    """The provider rejected the merchant's credential (401/403), or login failed."""

    status = ProviderStatus.AUTH_FAILED
    error_code = ProviderErrorCode.AUTH_FAILED


class ProviderParseError(ProviderError):
    """The provider answered in a shape the adapter does not understand (AC-3.3)."""

    error_code = ProviderErrorCode.PARSE_ERROR


class CircuitOpenError(ProviderError):
    """The breaker is open for this provider, so no call was made (AC-5.1)."""

    error_code = ProviderErrorCode.CIRCUIT_OPEN


class TokenWaitTimeout(ProviderAuthError):  # noqa: N818 — matches ProviderTimeout
    """
    Another worker holds the login lock and no token appeared in time (AC-4.6).

    Modelled as an auth failure because that is what the caller experiences — no usable
    token — but reported with its own error code so the cause stays legible.
    """

    error_code = ProviderErrorCode.TOKEN_WAIT_TIMEOUT
