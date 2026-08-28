"""FR-1.10 — password reset by OTP (6 digits, 15-min expiry, 3 retries, 2-min resend)."""

from collections.abc import Iterator

import pytest
import time_machine
from httpx import AsyncClient
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from src.auth.models import Otp, PasswordResetToken, UserSession
from src.config import settings
from tests.auth.test_login import PASSWORD, login, make_user

NEW_PASSWORD = "a-brand-new-secret"


@pytest.fixture
def sent_emails(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[tuple[str, str]]]:
    """Capture (recipient, otp_code) instead of sending. No SMTP in tests."""
    captured: list[tuple[str, str]] = []

    async def fake_send_otp_email(email: str, otp_code: str) -> bool:
        captured.append((email, otp_code))
        return True

    monkeypatch.setattr("src.auth.services.send_otp_email", fake_send_otp_email)
    return captured


async def start_reset(client: AsyncClient, identifier: str = "merchant@example.com") -> str:
    response = await client.post("/auth/password/forgot", json={"identifier": identifier})
    assert response.status_code == 200
    return str(response.json()["data"]["reset_token"])


class TestForgotPassword:
    async def test_otp_is_emailed_and_never_returned(
        self, client: AsyncClient, async_session: AsyncSession, sent_emails: list
    ):
        """The code goes out by email; the response carries only the correlator."""
        await make_user(async_session)

        response = await client.post(
            "/auth/password/forgot", json={"identifier": "merchant@example.com"}
        )

        assert response.status_code == 200
        assert len(sent_emails) == 1
        recipient, otp_code = sent_emails[0]
        assert recipient == "merchant@example.com"
        assert len(otp_code) == 6
        assert otp_code not in response.text

    async def test_unknown_account_is_indistinguishable(
        self, client: AsyncClient, async_session: AsyncSession, sent_emails: list
    ):
        """No enumeration: an unknown identifier gets the same shape and no email."""
        await make_user(async_session)

        known = await client.post(
            "/auth/password/forgot", json={"identifier": "merchant@example.com"}
        )
        unknown = await client.post(
            "/auth/password/forgot", json={"identifier": "nobody@example.com"}
        )

        assert known.status_code == unknown.status_code == 200
        assert known.json()["detail"] == unknown.json()["detail"]
        assert set(known.json()["data"]) == set(unknown.json()["data"])
        assert len(sent_emails) == 1  # only the real account was mailed

    async def test_mobile_only_account_sends_nothing_but_still_succeeds(
        self, client: AsyncClient, async_session: AsyncSession, sent_emails: list
    ):
        """No address to send to — the caller still cannot tell (email-only delivery)."""
        await make_user(async_session, email=None, mobile="01712345678")

        response = await client.post("/auth/password/forgot", json={"identifier": "01712345678"})

        assert response.status_code == 200
        assert sent_emails == []

    async def test_resend_respects_the_cooldown(
        self, client: AsyncClient, async_session: AsyncSession, sent_emails: list
    ):
        """FR-1.10 — a resend inside the 2-minute window is a 429 carrying the wait."""
        await make_user(async_session)
        reset_token = await start_reset(client)

        response = await client.post(
            "/auth/otp/resend",
            json={"identifier": "merchant@example.com", "reset_token": reset_token},
        )

        assert response.status_code == 429
        assert response.json()["error"] == "otp_cooldown"
        assert 0 < response.json()["retry_after_seconds"] <= settings.OTP_RETRY_DELAY_MINUTES * 60
        assert len(sent_emails) == 1

    async def test_resend_after_the_cooldown_issues_a_new_code(
        self, client: AsyncClient, async_session: AsyncSession, sent_emails: list
    ):
        await make_user(async_session)
        reset_token = await start_reset(client)

        with time_machine.travel(
            _minutes_from_now(settings.OTP_RETRY_DELAY_MINUTES + 1), tick=False
        ):
            response = await client.post(
                "/auth/otp/resend",
                json={"identifier": "merchant@example.com", "reset_token": reset_token},
            )

        assert response.status_code == 200
        assert len(sent_emails) == 2
        assert sent_emails[0][1] != sent_emails[1][1]


class TestVerifyOtp:
    async def test_correct_otp_returns_a_reset_token(
        self, client: AsyncClient, async_session: AsyncSession, sent_emails: list
    ):
        await make_user(async_session)
        reset_token = await start_reset(client)
        _, otp_code = sent_emails[0]

        response = await client.post(
            "/auth/otp/verify",
            json={
                "identifier": "merchant@example.com",
                "otp": otp_code,
                "reset_token": reset_token,
            },
        )

        assert response.status_code == 200
        assert response.json()["data"]["password_reset_token"]

    async def test_three_wrong_attempts_exhaust_the_otp(
        self, client: AsyncClient, async_session: AsyncSession, sent_emails: list
    ):
        """FR-1.10 — after OTP_RETRY_LIMIT failures the code is dead, not just refused."""
        await make_user(async_session)
        reset_token = await start_reset(client)
        _, otp_code = sent_emails[0]
        wrong = "000000" if otp_code != "000000" else "111111"

        payload = {
            "identifier": "merchant@example.com",
            "otp": wrong,
            "reset_token": reset_token,
        }
        for _ in range(settings.OTP_RETRY_LIMIT - 1):
            assert (await client.post("/auth/otp/verify", json=payload)).status_code == 401

        exhausted = await client.post("/auth/otp/verify", json=payload)
        assert exhausted.status_code == 429
        assert exhausted.json()["error"] == "otp_attempts_exceeded"

        # Even the correct code no longer works — the OTP was voided.
        recovered = await client.post(
            "/auth/otp/verify",
            json={
                "identifier": "merchant@example.com",
                "otp": otp_code,
                "reset_token": reset_token,
            },
        )
        assert recovered.status_code == 401

    async def test_expired_otp_is_410(
        self, client: AsyncClient, async_session: AsyncSession, sent_emails: list
    ):
        """FR-1.10 — 15-minute expiry."""
        await make_user(async_session)
        reset_token = await start_reset(client)
        _, otp_code = sent_emails[0]

        with time_machine.travel(_minutes_from_now(settings.OTP_EXPIRE_MINUTES + 1), tick=False):
            response = await client.post(
                "/auth/otp/verify",
                json={
                    "identifier": "merchant@example.com",
                    "otp": otp_code,
                    "reset_token": reset_token,
                },
            )

        assert response.status_code == 410
        assert response.json()["error"] == "otp_expired"

    async def test_otp_from_another_reset_attempt_is_refused(
        self, client: AsyncClient, async_session: AsyncSession, sent_emails: list
    ):
        """The correlator binds an OTP to the flow that requested it."""
        await make_user(async_session)
        await start_reset(client)
        _, first_code = sent_emails[0]

        with time_machine.travel(
            _minutes_from_now(settings.OTP_RETRY_DELAY_MINUTES + 1), tick=False
        ):
            second_token = await start_reset(client)

        response = await client.post(
            "/auth/otp/verify",
            json={
                "identifier": "merchant@example.com",
                "otp": first_code,
                "reset_token": second_token,
            },
        )

        assert response.status_code == 401

    async def test_non_numeric_otp_is_rejected_by_the_schema(
        self, client: AsyncClient, async_session: AsyncSession, sent_emails: list
    ):
        await make_user(async_session)
        reset_token = await start_reset(client)

        response = await client.post(
            "/auth/otp/verify",
            json={
                "identifier": "merchant@example.com",
                "otp": "abcdef",
                "reset_token": reset_token,
            },
        )

        assert response.status_code == 422


class TestResetPassword:
    async def test_ac_1_12_reset_invalidates_every_session(
        self, client: AsyncClient, async_session: AsyncSession, sent_emails: list
    ):
        """AC-1.12 — a refresh token issued before the reset is 401 afterwards."""
        user = await make_user(async_session)
        tokens = (await login(client, "merchant@example.com")).json()["data"]

        password_reset_token = await _run_reset_flow(client, sent_emails)
        response = await client.post(
            "/auth/password/reset",
            json={
                "password_reset_token": password_reset_token,
                "new_password": NEW_PASSWORD,
                "confirm_password": NEW_PASSWORD,
            },
        )
        assert response.status_code == 200

        refreshed = await client.post(
            "/auth/token/refresh", json={"refresh_token": tokens["refresh_token"]}
        )
        assert refreshed.status_code == 401

        sessions = (
            await async_session.exec(select(UserSession).where(UserSession.user_id == user.id))
        ).all()
        assert all(not db_session.is_active for db_session in sessions)

        assert (
            await login(client, "merchant@example.com", password=NEW_PASSWORD)
        ).status_code == 200
        assert (await login(client, "merchant@example.com", password=PASSWORD)).status_code == 401

    async def test_reset_clears_an_active_lockout(
        self, client: AsyncClient, async_session: AsyncSession, sent_emails: list
    ):
        """FR-1.8 + FR-1.10 — resetting the password is the documented way out of a lockout."""
        await make_user(async_session)
        for _ in range(settings.MAX_FAILED_LOGIN_ATTEMPTS):
            await login(client, "merchant@example.com", password="nope-nope-nope")
        assert (await login(client, "merchant@example.com")).status_code == 423

        password_reset_token = await _run_reset_flow(client, sent_emails)
        await client.post(
            "/auth/password/reset",
            json={
                "password_reset_token": password_reset_token,
                "new_password": NEW_PASSWORD,
                "confirm_password": NEW_PASSWORD,
            },
        )

        assert (
            await login(client, "merchant@example.com", password=NEW_PASSWORD)
        ).status_code == 200

    async def test_reset_token_is_single_use(
        self, client: AsyncClient, async_session: AsyncSession, sent_emails: list
    ):
        await make_user(async_session)
        password_reset_token = await _run_reset_flow(client, sent_emails)
        body = {
            "password_reset_token": password_reset_token,
            "new_password": NEW_PASSWORD,
            "confirm_password": NEW_PASSWORD,
        }
        assert (await client.post("/auth/password/reset", json=body)).status_code == 200

        replay = await client.post("/auth/password/reset", json=body)

        assert replay.status_code == 401
        assert replay.json()["error"] == "invalid_reset_token"

    async def test_expired_reset_token_is_401(
        self, client: AsyncClient, async_session: AsyncSession, sent_emails: list
    ):
        await make_user(async_session)
        password_reset_token = await _run_reset_flow(client, sent_emails)

        with time_machine.travel(
            _minutes_from_now(settings.PASSWORD_RESET_TOKEN_EXPIRE_MINUTES + 1), tick=False
        ):
            response = await client.post(
                "/auth/password/reset",
                json={
                    "password_reset_token": password_reset_token,
                    "new_password": NEW_PASSWORD,
                    "confirm_password": NEW_PASSWORD,
                },
            )

        assert response.status_code == 401

    async def test_garbage_reset_token_is_401(self, client: AsyncClient):
        response = await client.post(
            "/auth/password/reset",
            json={
                "password_reset_token": "not-a-token",
                "new_password": NEW_PASSWORD,
                "confirm_password": NEW_PASSWORD,
            },
        )

        assert response.status_code == 401

    async def test_mismatched_confirmation_is_422(self, client: AsyncClient):
        response = await client.post(
            "/auth/password/reset",
            json={
                "password_reset_token": "whatever.secret",
                "new_password": NEW_PASSWORD,
                "confirm_password": "something-else-entirely",
            },
        )

        assert response.status_code == 422


class TestStoredSecrets:
    async def test_the_plaintext_otp_is_never_stored(
        self, client: AsyncClient, async_session: AsyncSession, sent_emails: list
    ):
        """Only the hash is persisted, and it is a KDF hash — six digits is a tiny keyspace."""
        await make_user(async_session)
        await start_reset(client)
        _, otp_code = sent_emails[0]

        stored = (await async_session.exec(select(Otp))).all()

        assert len(stored) == 1
        assert otp_code not in stored[0].token_hash
        assert stored[0].token_hash.startswith("$argon2")

    async def test_the_reset_secret_is_never_stored(
        self, client: AsyncClient, async_session: AsyncSession, sent_emails: list
    ):
        await make_user(async_session)
        password_reset_token = await _run_reset_flow(client, sent_emails)
        secret = password_reset_token.split(".", 1)[1]

        stored = (await async_session.exec(select(PasswordResetToken))).all()

        assert len(stored) == 1
        assert secret not in stored[0].token_hash


def _minutes_from_now(minutes: int):
    from datetime import UTC, datetime, timedelta

    return datetime.now(UTC) + timedelta(minutes=minutes)


async def _run_reset_flow(client: AsyncClient, sent_emails: list) -> str:
    """Forgot -> verify, returning the password-reset token."""
    reset_token = await start_reset(client)
    _, otp_code = sent_emails[-1]
    verified = await client.post(
        "/auth/otp/verify",
        json={
            "identifier": "merchant@example.com",
            "otp": otp_code,
            "reset_token": reset_token,
        },
    )
    assert verified.status_code == 200
    return str(verified.json()["data"]["password_reset_token"])
