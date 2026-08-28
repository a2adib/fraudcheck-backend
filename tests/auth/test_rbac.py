"""FR-1.11 — roles, permissions, and the guards that read the JWT ``pb`` claim."""

from collections.abc import AsyncGenerator

import pytest
from fastapi import Depends, FastAPI
from httpx import ASGITransport, AsyncClient
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from src.auth.associations import RolePermissionLink, UserRoleLink
from src.auth.enums import PermissionCode
from src.auth.models import Permission, Role
from src.auth.permissions import (
    decode_permission_bitmask,
    encode_permission_bitmask,
    require_all_permissions,
    require_any_permission,
    require_permission,
)
from src.auth.services import DEFAULT_ROLE_NAME, AuthorizationService
from src.common.mixins import require_id
from src.database import get_session
from src.users.models import User
from tests.auth.test_login import login, make_user

OVERRIDE_PATH = "/test-only/orders/override"


async def grant(session: AsyncSession, user: User, codes: list[PermissionCode]) -> Role:
    """Give the user a role holding exactly ``codes``. Returns the role."""
    role = Role(name=f"role-{user.public_id}", description="test role")
    session.add(role)
    await session.commit()
    await session.refresh(role)

    permissions = (
        await session.exec(
            select(Permission).where(Permission.code.in_([c.value for c in codes]))  # type: ignore[attr-defined]
        )
    ).all()
    for permission in permissions:
        session.add(
            RolePermissionLink(role_id=require_id(role), permission_id=require_id(permission))
        )
    session.add(
        UserRoleLink(
            user_id=require_id(user), role_id=require_id(role), created_by_id=require_id(user)
        )
    )
    await session.commit()
    return role


async def bearer(client: AsyncClient, identifier: str = "merchant@example.com") -> dict[str, str]:
    """Log in and return an Authorization header carrying the fresh ``pb`` claim."""
    response = await login(client, identifier)
    assert response.status_code == 200
    return {"Authorization": f"Bearer {response.json()['data']['access_token']}"}


@pytest.fixture
async def guard_client(async_session: AsyncSession) -> AsyncGenerator[AsyncClient]:
    """
    Build a client for a throwaway app holding routes behind the real guards.

    AC-1.13 names the order-decision override endpoint, which lands with the orders
    domain at M6. Mounting a test-only route behind the *same*
    ``require_permission(RISK_ORDER_REVIEW)`` dependency proves the guard today, and the
    real endpoint reuses it unchanged.

    A separate app, not the shared one: registering routes on ``test_app`` would leave
    them behind for every later test in the worker.
    """
    guard_app = FastAPI()

    async def override_get_session() -> AsyncGenerator[AsyncSession]:
        yield async_session

    guard_app.dependency_overrides[get_session] = override_get_session

    @guard_app.post(
        OVERRIDE_PATH,
        dependencies=[Depends(require_permission(PermissionCode.RISK_ORDER_REVIEW))],
    )
    async def override_decision() -> dict[str, str]:
        return {"status": "overridden"}

    @guard_app.get(
        "/test-only/any",
        dependencies=[
            Depends(
                require_any_permission(PermissionCode.CHECK_RUN, PermissionCode.RISK_ORDER_REVIEW)
            )
        ],
    )
    async def any_route() -> dict[str, bool]:
        return {"ok": True}

    @guard_app.get(
        "/test-only/all",
        dependencies=[
            Depends(
                require_all_permissions(PermissionCode.CHECK_RUN, PermissionCode.RISK_ORDER_REVIEW)
            )
        ],
    )
    async def all_route() -> dict[str, bool]:
        return {"ok": True}

    async with AsyncClient(
        transport=ASGITransport(app=guard_app), base_url="http://test"
    ) as async_client:
        yield async_client


class TestPermissionGuard:
    async def test_ac_1_13_missing_permission_is_403(
        self,
        client: AsyncClient,
        guard_client: AsyncClient,
        async_session: AsyncSession,
        permission_catalogue: list[str],
    ):
        """AC-1.13 — no ``risk.order.review`` means a 403 from the override endpoint."""
        user = await make_user(async_session)
        await grant(async_session, user, [PermissionCode.RISK_ORDER_READ])

        response = await guard_client.post(OVERRIDE_PATH, headers=await bearer(client))

        assert response.status_code == 403
        assert PermissionCode.RISK_ORDER_REVIEW in response.json()["detail"]

    async def test_granted_permission_passes_the_guard(
        self,
        client: AsyncClient,
        guard_client: AsyncClient,
        async_session: AsyncSession,
        permission_catalogue: list[str],
    ):
        user = await make_user(async_session)
        await grant(async_session, user, [PermissionCode.RISK_ORDER_REVIEW])

        response = await guard_client.post(OVERRIDE_PATH, headers=await bearer(client))

        assert response.status_code == 200

    async def test_guarded_route_still_requires_authentication(self, guard_client: AsyncClient):
        """AC-1.7 — the permission guard does not replace the session check."""
        assert (await guard_client.post(OVERRIDE_PATH)).status_code == 401

    async def test_a_revoked_role_still_holds_until_the_token_is_reminted(
        self,
        client: AsyncClient,
        guard_client: AsyncClient,
        async_session: AsyncSession,
        permission_catalogue: list[str],
    ):
        """
        The documented staleness: ``pb`` is read from the token, not the database.

        Revoking mid-session takes effect at the next login/refresh — killing access
        immediately means killing the session.
        """
        user = await make_user(async_session)
        role = await grant(async_session, user, [PermissionCode.RISK_ORDER_REVIEW])
        headers = await bearer(client)

        await AuthorizationService(async_session).assign_roles(user, [], user.public_id)

        assert (await guard_client.post(OVERRIDE_PATH, headers=headers)).status_code == 200
        assert role.public_id  # the role itself still exists; only the grant is gone
        assert (
            await guard_client.post(OVERRIDE_PATH, headers=await bearer(client))
        ).status_code == 403


class TestBitmask:
    def test_round_trips_every_code(self):
        codes = [code.value for code in PermissionCode]

        assert decode_permission_bitmask(encode_permission_bitmask(codes)) == set(codes)

    def test_unknown_codes_are_dropped_not_shifted(self):
        """An unrecognised code must not shift the bits of the real ones."""
        encoded = encode_permission_bitmask(["nope.not.real", PermissionCode.CHECK_RUN.value])

        assert decode_permission_bitmask(encoded) == {PermissionCode.CHECK_RUN.value}

    @pytest.mark.parametrize("claim", ["", "!!!not-base64!!!"])
    def test_a_missing_or_malformed_claim_grants_nothing(self, claim: str):
        assert decode_permission_bitmask(claim) == set()


class TestCatalogue:
    async def test_sync_is_idempotent(
        self, async_session: AsyncSession, permission_catalogue: list[str]
    ):
        """`just permissions` runs on every deploy — it must not duplicate rows."""
        before = len(permission_catalogue)

        again = await AuthorizationService(async_session).sync_permission_catalogue()

        assert len(again) == before == len(PermissionCode)

    async def test_owner_role_holds_every_permission(
        self, async_session: AsyncSession, permission_catalogue: list[str]
    ):
        role = (
            await async_session.exec(select(Role).where(Role.name == DEFAULT_ROLE_NAME))
        ).first()
        assert role is not None

        linked = (
            await async_session.exec(
                select(RolePermissionLink).where(RolePermissionLink.role_id == role.id)
            )
        ).all()

        assert len(linked) == len(PermissionCode)

    async def test_registration_grants_the_owner_role(
        self, client: AsyncClient, async_session: AsyncSession, permission_catalogue: list[str]
    ):
        """A merchant owns their tenant, so registration hands them every permission."""
        response = await client.post(
            "/auth/register",
            json={
                "email": "owner@example.com",
                "password": "correct-horse-battery",
                "full_name": "Owner",
            },
        )
        assert response.status_code == 201

        me = await client.post(
            "/auth/login",
            json={"identifier": "owner@example.com", "password": "correct-horse-battery"},
        )
        permissions = await client.get(
            "/auth/permissions/me",
            headers={"Authorization": f"Bearer {me.json()['data']['access_token']}"},
        )

        assert len(permissions.json()["data"]) == len(PermissionCode)

    async def test_registration_survives_an_unsynced_catalogue(
        self, client: AsyncClient, async_session: AsyncSession
    ):
        """No catalogue (fixture not requested) must not break signup — just leave it unprivileged."""
        response = await client.post(
            "/auth/register",
            json={
                "email": "nocatalogue@example.com",
                "password": "correct-horse-battery",
                "full_name": "No Catalogue",
            },
        )

        assert response.status_code == 201


class TestRoleRoutes:
    async def test_role_crud_requires_admin_permissions(
        self, client: AsyncClient, async_session: AsyncSession, permission_catalogue: list[str]
    ):
        user = await make_user(async_session)
        await grant(async_session, user, [PermissionCode.CHECK_RUN])
        headers = await bearer(client)

        assert (await client.get("/auth/roles", headers=headers)).status_code == 403
        created = await client.post(
            "/auth/roles", headers=headers, json={"name": "Analyst", "permission_public_ids": []}
        )
        assert created.status_code == 403

    async def test_admin_can_create_read_update_and_delete_a_role(
        self, client: AsyncClient, async_session: AsyncSession, permission_catalogue: list[str]
    ):
        user = await make_user(async_session)
        await grant(
            async_session,
            user,
            [
                PermissionCode.ADMIN_ROLE_CREATE,
                PermissionCode.ADMIN_ROLE_READ,
                PermissionCode.ADMIN_ROLE_UPDATE,
                PermissionCode.ADMIN_ROLE_DELETE,
            ],
        )
        headers = await bearer(client)
        catalogue = await client.get("/auth/permissions", headers=headers)
        review = next(
            item
            for item in catalogue.json()["data"]
            if item["code"] == PermissionCode.RISK_ORDER_REVIEW
        )

        created = await client.post(
            "/auth/roles",
            headers=headers,
            json={"name": "Reviewer", "permission_public_ids": [review["public_id"]]},
        )
        assert created.status_code == 201
        role_public_id = created.json()["data"]["public_id"]

        detail = await client.get(f"/auth/roles/{role_public_id}", headers=headers)
        assert [p["code"] for p in detail.json()["data"]["permissions"]] == [
            PermissionCode.RISK_ORDER_REVIEW
        ]

        updated = await client.put(
            f"/auth/roles/{role_public_id}",
            headers=headers,
            json={"name": "Senior Reviewer", "permission_public_ids": []},
        )
        assert updated.status_code == 200
        assert updated.json()["data"]["name"] == "Senior Reviewer"

        assert (
            await client.delete(f"/auth/roles/{role_public_id}", headers=headers)
        ).status_code == 200
        assert (
            await client.get(f"/auth/roles/{role_public_id}", headers=headers)
        ).status_code == 404

    async def test_duplicate_role_name_is_rejected(
        self, client: AsyncClient, async_session: AsyncSession, permission_catalogue: list[str]
    ):
        user = await make_user(async_session)
        await grant(async_session, user, [PermissionCode.ADMIN_ROLE_CREATE])
        headers = await bearer(client)
        body = {"name": "Reviewer", "permission_public_ids": []}

        assert (await client.post("/auth/roles", headers=headers, json=body)).status_code == 201
        duplicate = await client.post("/auth/roles", headers=headers, json=body)

        assert duplicate.status_code == 400

    async def test_unknown_permission_id_is_404(
        self, client: AsyncClient, async_session: AsyncSession, permission_catalogue: list[str]
    ):
        user = await make_user(async_session)
        await grant(async_session, user, [PermissionCode.ADMIN_ROLE_CREATE])
        headers = await bearer(client)

        response = await client.post(
            "/auth/roles",
            headers=headers,
            json={"name": "Ghost", "permission_public_ids": ["does-not-ex"]},
        )

        assert response.status_code == 404


class TestAssignRoles:
    async def test_assignment_replaces_rather_than_appends(
        self, client: AsyncClient, async_session: AsyncSession, permission_catalogue: list[str]
    ):
        admin = await make_user(async_session)
        await grant(
            async_session,
            admin,
            [PermissionCode.ADMIN_USER_UPDATE, PermissionCode.ADMIN_ROLE_CREATE],
        )
        target = await make_user(async_session, email="target@example.com")
        headers = await bearer(client)
        created = await client.post(
            "/auth/roles",
            headers=headers,
            json={"name": "Reviewer", "permission_public_ids": []},
        )
        role_public_id = created.json()["data"]["public_id"]

        assigned = await client.post(
            f"/auth/users/{target.public_id}/roles",
            headers=headers,
            json={"role_public_ids": [role_public_id]},
        )
        assert assigned.status_code == 200

        cleared = await client.post(
            f"/auth/users/{target.public_id}/roles",
            headers=headers,
            json={"role_public_ids": []},
        )
        assert cleared.status_code == 200

        links = (
            await async_session.exec(select(UserRoleLink).where(UserRoleLink.user_id == target.id))
        ).all()
        assert links == []


class TestCompositeGuards:
    async def test_require_any_and_require_all(
        self,
        client: AsyncClient,
        guard_client: AsyncClient,
        async_session: AsyncSession,
        permission_catalogue: list[str],
    ):
        """One of two codes passes ``require_any``; both are needed for ``require_all``."""
        user = await make_user(async_session)
        await grant(async_session, user, [PermissionCode.CHECK_RUN])
        headers = await bearer(client)

        assert (await guard_client.get("/test-only/any", headers=headers)).status_code == 200
        assert (await guard_client.get("/test-only/all", headers=headers)).status_code == 403
