"""
FR-1 — authentication (register, login, refresh rotation, logout).

Every test name carries the acceptance-criterion id it proves, per CLAUDE.md.
"""

import time

import pytest
from httpx import AsyncClient
from sqlmodel.ext.asyncio.session import AsyncSession

from src.auth.utils import REFRESH_COOKIE_PATH, get_password_hash
from src.config import settings
from src.users.enums import UserStatus
from src.users.models import User

PASSWORD = "correct-horse-battery"


async def make_user(
    session: AsyncSession,
    *,
    email: str = "merchant@example.com",
    mobile: str | None = None,
    password: str = PASSWORD,
    status: UserStatus = UserStatus.ACTIVE,
) -> User:
    user = User(
        email=email,
        mobile=mobile,
        password_hash=get_password_hash(password),
        full_name="Test Merchant",
        status=status,
    )
    session.add(user)
    await session.commit()
    await session.refresh(user)
    return user


async def login(client: AsyncClient, identifier: str, password: str = PASSWORD, **extra: object):
    return await client.post(
        "/auth/login", json={"identifier": identifier, "password": password, **extra}
    )


class TestRegistration:
    async def test_ac_1_1_registration_returns_public_id_and_never_the_password(
        self, client: AsyncClient
    ):
        """AC-1.1 — 201 with the public_id, and no password material anywhere."""
        response = await client.post(
            "/auth/register",
            json={
                "email": "new@example.com",
                "password": PASSWORD,
                "full_name": "New Merchant",
                "mobile": "+8801712345678",
            },
        )

        assert response.status_code == 201
        body = response.json()
        assert body["data"]["public_id"]
        assert body["data"]["mobile"] == "01712345678"  # normalized on the way in
        assert PASSWORD not in response.text
        assert "password" not in response.text

    async def test_ac_1_2_duplicate_email_is_a_generic_409(
        self, client: AsyncClient, async_session: AsyncSession
    ):
        """AC-1.2 — the conflict must not confirm *which* field collided."""
        await make_user(async_session, email="taken@example.com")

        response = await client.post(
            "/auth/register",
            json={"email": "taken@example.com", "password": PASSWORD, "full_name": "Copycat"},
        )

        assert response.status_code == 409
        assert response.json()["error"] == "account_exists"
        assert "email" not in response.json()["message"].lower()

    async def test_ac_1_3_short_password_is_rejected(self, client: AsyncClient):
        """AC-1.3 — under 10 characters is a 422 from the schema, before any DB work."""
        response = await client.post(
            "/auth/register",
            json={"email": "short@example.com", "password": "9charsxx", "full_name": "Shorty"},
        )

        assert response.status_code == 422


class TestLogin:
    async def test_ac_1_4_valid_credentials_return_both_tokens(
        self, client: AsyncClient, async_session: AsyncSession
    ):
        """AC-1.4 — 200 with an access token and a refresh token."""
        await make_user(async_session)

        response = await login(client, "merchant@example.com")

        assert response.status_code == 200
        data = response.json()["data"]
        assert data["access_token"]
        assert data["refresh_token"]
        assert data["token_type"] == "Bearer"

    async def test_ac_1_5_wrong_password_is_constant_time(
        self,
        client: AsyncClient,
        async_session: AsyncSession,
        monkeypatch: pytest.MonkeyPatch,
    ):
        """
        AC-1.5 — a wrong password and an unknown account cost the same.

        Both paths must run the KDF; skipping it on a lookup miss is a timing oracle
        for which emails are registered. The spec's bound is 50ms over 20 samples.

        Lockout is lifted for the duration: 20 deliberate failures would otherwise trip
        it at the 5th and start measuring the 423 path instead of the password path.
        """
        monkeypatch.setattr(settings, "MAX_FAILED_LOGIN_ATTEMPTS", 10_000)
        await make_user(async_session)
        samples = 20

        async def mean_ms(identifier: str) -> float:
            total = 0.0
            for _ in range(samples):
                started = time.perf_counter()
                response = await login(client, identifier, password="wrong-password-here")
                total += (time.perf_counter() - started) * 1000
                assert response.status_code == 401
            return total / samples

        wrong_password = await mean_ms("merchant@example.com")
        unknown_account = await mean_ms("nobody@example.com")

        assert abs(wrong_password - unknown_account) < 50

    async def test_wrong_password_and_unknown_account_are_indistinguishable(
        self, client: AsyncClient, async_session: AsyncSession
    ):
        """The bodies must match too — timing is only half of the oracle."""
        await make_user(async_session)

        wrong = await login(client, "merchant@example.com", password="wrong-password-here")
        unknown = await login(client, "nobody@example.com", password="wrong-password-here")

        assert wrong.status_code == unknown.status_code == 401
        assert wrong.json() == unknown.json()

    @pytest.mark.parametrize(
        "identifier",
        ["01712345678", "+8801712345678", "880-171-2345678"],
    )
    async def test_ac_1_9_every_mobile_form_resolves_to_one_account(
        self, client: AsyncClient, async_session: AsyncSession, identifier: str
    ):
        """AC-1.9 — all three identifier forms log in as the same merchant."""
        user = await make_user(async_session, email=None, mobile="01712345678")

        response = await login(client, identifier)

        assert response.status_code == 200
        me = await client.get(
            "/auth/me",
            headers={"Authorization": f"Bearer {response.json()['data']['access_token']}"},
        )
        assert me.json()["data"]["public_id"] == user.public_id

    async def test_ac_1_10_lockout_survives_the_correct_password(
        self, client: AsyncClient, async_session: AsyncSession
    ):
        """AC-1.10 — after 5 failures the 6th attempt is 423 even when it is right."""
        await make_user(async_session)

        for _ in range(settings.MAX_FAILED_LOGIN_ATTEMPTS):
            assert (
                await login(client, "merchant@example.com", password="nope-nope-nope")
            ).status_code == 401

        response = await login(client, "merchant@example.com")

        assert response.status_code == 423
        assert response.json()["error"] == "account_locked"
        assert response.json()["retry_after_seconds"] > 0

    async def test_deactivated_account_gets_403_not_401(
        self, client: AsyncClient, async_session: AsyncSession
    ):
        """A blocked merchant is told the account is disabled, not that the password is wrong."""
        await make_user(async_session, status=UserStatus.BLOCKED)

        response = await login(client, "merchant@example.com")

        assert response.status_code == 403
        assert response.json()["error"] == "account_deactivated"


class TestCookieDelivery:
    async def test_ac_1_11_cookie_mode_leaks_no_token_into_the_body(
        self, client: AsyncClient, async_session: AsyncSession
    ):
        """AC-1.11 — no token string in the body; the refresh cookie is path-scoped."""
        await make_user(async_session)

        response = await login(client, "merchant@example.com", token_delivery="cookie")

        assert response.status_code == 200
        data = response.json()["data"]
        assert data["access_token"] is None
        assert data["refresh_token"] is None

        cookies = {cookie.name: cookie for cookie in response.cookies.jar}
        assert cookies["access_token"].path == "/"
        assert cookies["refresh_token"].path == REFRESH_COOKIE_PATH
        set_cookie_headers = " ".join(response.headers.get_list("set-cookie")).lower()
        assert "httponly" in set_cookie_headers
        assert "samesite=strict" in set_cookie_headers

    async def test_cookie_session_authenticates_protected_routes(
        self, client: AsyncClient, async_session: AsyncSession
    ):
        """With no Authorization header, the access cookie carries the session."""
        user = await make_user(async_session)
        await login(client, "merchant@example.com", token_delivery="cookie")

        response = await client.get("/auth/me")

        assert response.status_code == 200
        assert response.json()["data"]["public_id"] == user.public_id


class TestProtectedRoutes:
    async def test_ac_1_7_no_authorization_header_is_401(self, client: AsyncClient):
        """AC-1.7 — a protected endpoint refuses an anonymous caller."""
        response = await client.get("/auth/me")

        assert response.status_code == 401
