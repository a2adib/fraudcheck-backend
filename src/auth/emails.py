"""
Auth email templates.

Ported from ``erp-backend/src/auth/emails.py`` without its Jinja dependency — one
template with two substitutions does not need a template engine, and the project has
no ``TEMPLATES_DIR``.
"""

import logging

from src.common.emails import send_email
from src.config import settings

logger = logging.getLogger(__name__)

OTP_SUBJECT = "Your Fraud Checker BD password reset code"

_OTP_TEMPLATE = """\
<!doctype html>
<html lang="en">
  <body style="font-family: system-ui, sans-serif; color: #1a1a1a;">
    <h2>Password reset</h2>
    <p>Use this code to reset your Fraud Checker BD password:</p>
    <p style="font-size: 28px; letter-spacing: 6px; font-weight: 700;">{otp_code}</p>
    <p>The code expires in {otp_duration} minutes and can be used once.</p>
    <p style="color: #6b6b6b;">
      If you did not request a password reset, ignore this email — nothing has changed.
    </p>
  </body>
</html>
"""


async def send_otp_email(email: str, otp_code: str) -> bool:
    """Send the password-reset OTP (FR-1.10). Returns whether it was accepted."""
    html_content = _OTP_TEMPLATE.format(otp_code=otp_code, otp_duration=settings.OTP_EXPIRE_MINUTES)
    return await send_email(to_email=email, subject=OTP_SUBJECT, html_content=html_content)
