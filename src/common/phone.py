"""
Bangladesh mobile number normalization.

Ported from ``erp-backend/src/auth/utils.py:31,69``, which already implements exactly
the rule this project's FR-6.9 specifies. It lives in ``common`` rather than ``auth``
because two domains need it: auth (login identifier) and checks (phone normalization
and cache keying).

The one change from upstream is the exception. Upstream raises ``AuthAPIError``
directly; here it raises a neutral ``InvalidPhoneNumberError`` so each caller can map it to
the right HTTP shape — ``AuthAPIError`` on the login path, ``HTTP422`` on the check
path — without ``common`` depending on ``auth``.
"""

import re

BD_MOBILE_RE = re.compile(r"^01[3-9]\d{8}$")

_PHONE_MASK_PREFIX = 3
_PHONE_MASK_SUFFIX = 3


class InvalidPhoneNumberError(ValueError):
    """Raised when a string cannot be normalized to a valid BD mobile number."""


def normalize_bd_mobile(raw: str) -> str:
    """
    Normalise a Bangladesh mobile number to the local ``01XXXXXXXXX`` format.

    Handles ``+880…``, ``880…``, ``01…`` and bare 10-digit (``1…``) inputs and strips
    spaces, dashes and parentheses. Valid operator prefixes are ``013``-``019``.

    Raises:
        InvalidPhoneNumberError: when the result is not a valid BD mobile number.

    """
    cleaned = re.sub(r"[\s\-()]", "", raw.strip())

    if cleaned.startswith("+880"):
        cleaned = cleaned[4:]
    elif cleaned.startswith("880"):
        cleaned = cleaned[3:]
    elif cleaned.startswith("0"):
        cleaned = cleaned[1:]

    normalized = f"0{cleaned}"

    if not BD_MOBILE_RE.match(normalized):
        msg = "Invalid Bangladesh mobile number format."
        raise InvalidPhoneNumberError(msg)
    return normalized


def mask_phone(phone: str) -> str:
    """
    Mask a phone number for logging: ``01712345678`` -> ``017*****678`` (FR-14.5).

    Anything too short to mask meaningfully is fully redacted rather than partially
    revealed.
    """
    if len(phone) < _PHONE_MASK_PREFIX + _PHONE_MASK_SUFFIX:
        return "*" * len(phone)
    hidden = len(phone) - _PHONE_MASK_PREFIX - _PHONE_MASK_SUFFIX
    return f"{phone[:_PHONE_MASK_PREFIX]}{'*' * hidden}{phone[-_PHONE_MASK_SUFFIX:]}"
