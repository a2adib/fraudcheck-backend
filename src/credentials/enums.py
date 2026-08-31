from enum import StrEnum


class CredentialStatus(StrEnum):
    """
    Whether a stored credential is known to work (FR-2.4).

    ``UNTESTED`` is the honest default: a credential that has been saved but never
    exercised tells us nothing, and pretending otherwise would show a merchant a green
    tick for a password that has never once been used.
    """

    UNTESTED = "untested"
    VALID = "valid"
    INVALID = "invalid"
