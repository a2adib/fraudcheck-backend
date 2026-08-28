"""FR-1.4 to FR-1.6 — refresh rotation, reuse detection, logout, password change."""

from httpx import AsyncClient
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from src.auth.models import UserSession
from src.securities.models import ActivityLog
from tests.auth.test_login import PASSWORD, login, make_user


async def login_tokens(client: AsyncClient, identifier: str = "merchant@example.com") -> dict:
    response = await login(client, identifier)
    assert response.status_code == 200
    return response.json()["data"]


class TestRefreshRotation:
    async def test_refresh_rotates_the_token(
        self, client: AsyncClient, async_session: AsyncSession
    ):
        """FR-1.4 — refresh returns a *different* refresh token, not the same one back."""
        await make_user(async_session)
        tokens = await login_tokens(client)

        response = await client.post(
            "/auth/token/refresh", json={"refresh_token": tokens["refresh_token"]}
        )

        assert response.status_code == 200
        rotated = response.json()["data"]
        assert rotated["access_token"]
        assert rotated["refresh_token"] != tokens["refresh_token"]

    async def test_ac_1_6_revoked_refresh_token_is_401(
        self, client: AsyncClient, async_session: AsyncSession
    ):
        """AC-1.6 — a token whose session was revoked by logout no longer refreshes."""
        await make_user(async_session)
        tokens = await login_tokens(client)

        await client.post("/auth/logout", json={"refresh_token": tokens["refresh_token"]})
        response = await client.post(
            "/auth/token/refresh", json={"refresh_token": tokens["refresh_token"]}
        )

        assert response.status_code == 401

    async def test_ac_1_8_reuse_revokes_every_session(
        self, client: AsyncClient, async_session: AsyncSession
    ):
        """AC-1.8 — replaying a rotated token is 401 and kills all of the user's sessions."""
        user = await make_user(async_session)
        first = await login_tokens(client)
        # A second, unrelated session that must also die when reuse is detected.
        second = await login_tokens(client)

        rotated = await client.post(
            "/auth/token/refresh", json={"refresh_token": first["refresh_token"]}
        )
        assert rotated.status_code == 200

        replay = await client.post(
            "/auth/token/refresh", json={"refresh_token": first["refresh_token"]}
        )

        assert replay.status_code == 401
        assert replay.json()["error"] == "token_reuse_detected"

        sessions = (
            await async_session.exec(select(UserSession).where(UserSession.user_id == user.id))
        ).all()
        assert len(sessions) == 2
        assert all(not db_session.is_active for db_session in sessions)

        # And the untouched second session's token is dead too.
        assert (
            await client.post(
                "/auth/token/refresh", json={"refresh_token": second["refresh_token"]}
            )
        ).status_code == 401

    async def test_garbage_refresh_token_is_401(
        self, client: AsyncClient, async_session: AsyncSession
    ):
        await make_user(async_session)

        response = await client.post("/auth/token/refresh", json={"refresh_token": "not-a-token"})

        assert response.status_code == 401
        assert response.json()["error"] == "invalid_token"

    async def test_missing_refresh_token_is_400(self, client: AsyncClient):
        response = await client.post("/auth/token/refresh", json={})

        assert response.status_code == 400
        assert response.json()["error"] == "missing_token"


class TestLogout:
    async def test_ac_1_5_logout_deactivates_the_session(
        self, client: AsyncClient, async_session: AsyncSession
    ):
        """FR-1.5 — logout flips the session's is_active off."""
        user = await make_user(async_session)
        tokens = await login_tokens(client)

        response = await client.post(
            "/auth/logout", json={"refresh_token": tokens["refresh_token"]}
        )

        assert response.status_code == 200
        sessions = (
            await async_session.exec(select(UserSession).where(UserSession.user_id == user.id))
        ).all()
        assert all(not db_session.is_active for db_session in sessions)

    async def test_access_token_dies_with_its_session(
        self, client: AsyncClient, async_session: AsyncSession
    ):
        """A still-unexpired access token stops working once its session is revoked."""
        await make_user(async_session)
        tokens = await login_tokens(client)
        headers = {"Authorization": f"Bearer {tokens['access_token']}"}
        assert (await client.get("/auth/me", headers=headers)).status_code == 200

        await client.post("/auth/logout", json={"refresh_token": tokens["refresh_token"]})

        assert (await client.get("/auth/me", headers=headers)).status_code == 401

    async def test_cookie_client_logs_out_with_only_the_access_cookie(
        self, client: AsyncClient, async_session: AsyncSession
    ):
        """The refresh cookie is scoped away from /auth/logout, so `sid` is the fallback."""
        user = await make_user(async_session)
        await login(client, "merchant@example.com", token_delivery="cookie")

        response = await client.post("/auth/logout", json={})

        assert response.status_code == 200
        sessions = (
            await async_session.exec(select(UserSession).where(UserSession.user_id == user.id))
        ).all()
        assert all(not db_session.is_active for db_session in sessions)


class TestPasswordChange:
    async def test_ac_1_12_password_change_kills_every_refresh_token(
        self, client: AsyncClient, async_session: AsyncSession
    ):
        """AC-1.12 — after a password change, a previously issued refresh token is 401."""
        await make_user(async_session)
        tokens = await login_tokens(client)

        changed = await client.post(
            "/auth/change-password",
            headers={"Authorization": f"Bearer {tokens['access_token']}"},
            json={
                "current_password": PASSWORD,
                "new_password": "a-brand-new-secret",
                "confirm_password": "a-brand-new-secret",
            },
        )
        assert changed.status_code == 200

        response = await client.post(
            "/auth/token/refresh", json={"refresh_token": tokens["refresh_token"]}
        )

        assert response.status_code == 401
        assert (
            await login(client, "merchant@example.com", password="a-brand-new-secret")
        ).status_code == 200

    async def test_wrong_current_password_is_401(
        self, client: AsyncClient, async_session: AsyncSession
    ):
        await make_user(async_session)
        tokens = await login_tokens(client)

        response = await client.post(
            "/auth/change-password",
            headers={"Authorization": f"Bearer {tokens['access_token']}"},
            json={
                "current_password": "not-the-password",
                "new_password": "a-brand-new-secret",
                "confirm_password": "a-brand-new-secret",
            },
        )

        assert response.status_code == 401


class TestAuditTrail:
    async def test_ac_1_12_side_effecting_routes_write_an_activity_log(
        self, client: AsyncClient, async_session: AsyncSession
    ):
        """FR-1.12 — login and logout each leave an audit row, carrying no secrets."""
        user = await make_user(async_session)
        tokens = await login_tokens(client)
        await client.post("/auth/logout", json={"refresh_token": tokens["refresh_token"]})

        logs = (
            await async_session.exec(
                select(ActivityLog).where(ActivityLog.created_by_id == user.id)
            )
        ).all()

        descriptions = [log.description for log in logs]
        assert "Merchant logged in successfully" in descriptions
        assert "Merchant logged out" in descriptions
        assert all(tokens["refresh_token"] not in str(log.data) for log in logs)
