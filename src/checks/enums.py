from enum import StrEnum


class CheckSource(StrEnum):
    """Where a check came in from — the same pipeline serves all four (spec §4)."""

    WEB = "web"
    API = "api"
    BULK = "bulk"
    ORDER = "order"


class RiskBand(StrEnum):
    """
    FR-7.2 bands, and the vocabulary the whole service scores in.

    **Risk-oriented**: ``HIGH`` means a *bad* customer. Govaly's upstream badge is
    quality-oriented and inverted — its ``HIGH`` renders green — so a value from there
    must be mapped by the explicit table in FR-15.12, never by name.
    """

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class CheckEvent(StrEnum):
    """The SSE event names, in the order FR-6.5 requires them."""

    STARTED = "started"
    PROVIDER_RESULT = "provider_result"
    SCORE = "score"
    DONE = "done"
