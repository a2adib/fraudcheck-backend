from enum import StrEnum


class UserStatus(StrEnum):
    ACTIVE = "ACTIVE"
    INACTIVE = "INACTIVE"
    BLOCKED = "BLOCKED"


class Locale(StrEnum):
    """UI locale, persisted to the profile so it follows the merchant across devices."""

    EN = "en"
    BN = "bn"
