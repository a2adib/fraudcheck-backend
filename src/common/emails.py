"""
Email transport.

Ported in shape from ``erp-backend/src/common/emails.py``, but with an explicit
fallback: when ``SMTP_HOST`` is unset the message is logged instead of sent, so the
OTP flow (FR-1.10) is fully exercisable locally with no mail server. ``Config``
refuses to start a deployed environment in that state, so the fallback can never be
what production is doing.

Nothing here logs a full address (FR-14.5) — recipients are masked.
"""

import logging
from email.message import EmailMessage

import aiosmtplib

from src.config import settings

logger = logging.getLogger(__name__)

_MASK_PREFIX = 2


def mask_email(email: str) -> str:
    """Mask an address for logging: ``merchant@example.com`` -> ``me***@example.com``."""
    local, separator, domain = email.partition("@")
    if not separator:
        return "*" * len(email)
    if len(local) <= _MASK_PREFIX:
        return f"{'*' * len(local)}@{domain}"
    return f"{local[:_MASK_PREFIX]}***@{domain}"


def _build_message(to_email: str, subject: str, html_content: str) -> EmailMessage:
    message = EmailMessage()
    message["From"] = f"{settings.SMTP_FROM_NAME} <{settings.SMTP_FROM_EMAIL}>"
    message["To"] = to_email
    message["Subject"] = subject
    message.set_content("This message requires an HTML-capable mail client.")
    message.add_alternative(html_content, subtype="html")
    return message


async def _send_via_smtp(message: EmailMessage) -> bool:
    try:
        await aiosmtplib.send(
            message,
            hostname=settings.SMTP_HOST,
            port=settings.SMTP_PORT,
            username=settings.SMTP_USERNAME,
            password=settings.SMTP_PASSWORD,
            start_tls=settings.SMTP_USE_TLS,
            timeout=settings.SMTP_TIMEOUT_SECONDS,
        )
    except (aiosmtplib.SMTPException, OSError):
        # A failed send must not surface the recipient or the body in the traceback,
        # and must not take the request down with it — callers decide what a false means.
        logger.exception("Failed to send email to %s", mask_email(str(message["To"])))
        return False
    return True


def _log_instead_of_sending(to_email: str, subject: str, html_content: str) -> bool:
    """
    Development fallback: record that the mail would have gone out.

    The body is included only on a debug environment, because for the OTP mail the body
    *is* the credential.
    """
    logger.warning(
        "SMTP is not configured — email not sent. to=%s subject=%s",
        mask_email(to_email),
        subject,
    )
    if settings.ENVIRONMENT.is_debug:
        logger.info("Email body (debug environments only):\n%s", html_content)
    return True


async def send_email(to_email: str, subject: str, html_content: str) -> bool:
    """Send an HTML email. Returns whether it was accepted for delivery."""
    if not settings.SMTP_HOST:
        return _log_instead_of_sending(to_email, subject, html_content)

    return await _send_via_smtp(_build_message(to_email, subject, html_content))
