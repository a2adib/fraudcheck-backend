"""Courier vocabulary shared by the adapters, the vault and the check orchestrator."""

from enum import StrEnum


class ProviderEnum(StrEnum):
    """
    The couriers this service can query.

    Values are lowercase because they travel in URLs (``/credentials/pathao``) and in
    API payloads. A member exists as soon as the *credential* shape is known — the
    registry decides which of them actually has a live adapter, so ``steadfast`` is
    listed here while its contract is still being established (FR-3, M2).
    """

    PATHAO = "pathao"
    STEADFAST = "steadfast"
    REDX = "redx"


class ProviderStatus(StrEnum):
    """How one provider's leg of a check ended (spec §4, ``ProviderResult.status``)."""

    OK = "ok"
    UNAVAILABLE = "unavailable"
    AUTH_FAILED = "auth_failed"
    TIMEOUT = "timeout"


class ProviderErrorCode(StrEnum):
    """
    Machine-readable reason a provider leg did not return stats.

    The frontend renders these; they are part of the API contract and are named in the
    acceptance criteria (``no_credential`` AC-2.7, ``circuit_open`` AC-5.1,
    ``token_wait_timeout`` AC-4.6).
    """

    NO_CREDENTIAL = "no_credential"
    CIRCUIT_OPEN = "circuit_open"
    TOKEN_WAIT_TIMEOUT = "token_wait_timeout"  # noqa: S105 — an error code, not a secret
    AUTH_FAILED = "auth_failed"
    TIMEOUT = "timeout"
    PARSE_ERROR = "parse_error"
    PROVIDER_ERROR = "provider_error"
