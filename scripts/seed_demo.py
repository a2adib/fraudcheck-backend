"""
Seed the demo dataset (FR-11.5).

Run with ``just seed``. Idempotent — re-running refreshes the demo accounts and their
grants rather than creating a second set, so it is safe after every migration.

Scope grows with the milestones. Today that is the permission catalogue, the roles, the
demo accounts (FR-1.11) and 50 historical checks. The rest of FR-11.5 (200
``OrderContext`` rows with labelled outcomes — enough to train the demo model for
AC-11.8) lands with the tables it needs, at M6.

Three accounts, not one, because RBAC is only demonstrable with something to compare:
the owner holds every permission, the reviewer can override a decision (AC-1.13), and
the analyst can only read. Logging in as each is the fastest way to see the guards work.
The check history hangs off the owner, which is the account the demo logs in as.
"""

import asyncio
import sys
from datetime import UTC, datetime, timedelta
from typing import NamedTuple

from sqlalchemy import func, text
from sqlmodel import col, select
from sqlmodel.ext.asyncio.session import AsyncSession

from src.auth.enums import PermissionCode
from src.auth.models import Permission, Role
from src.auth.services import DEFAULT_ROLE_NAME, AuthorizationService
from src.auth.utils import get_password_hash
from src.checks.enums import CheckSource
from src.checks.models import CheckRequest, ProviderResult
from src.checks.scoring import score_courier_history
from src.config import settings
from src.constants import Environment
from src.database import AsyncSessionLocal
from src.logistics.enums import ProviderEnum, ProviderStatus
from src.logistics.providers.mock import RESERVED_SCENARIOS, scenario_for
from src.logistics.schemas import DeliveryStats
from src.users.enums import Locale, UserStatus
from src.users.models import User

# One password for every demo account — printed on purpose, and never a real credential.
DEMO_PASSWORD = "DemoPassword123"  # noqa: S105

DEMO_EMAIL = "demo@fraudcheck.test"
DEMO_MOBILE = "01712345678"
DEMO_FULL_NAME = "Demo Merchant"

REVIEWER_ROLE = "Risk Reviewer"
ANALYST_ROLE = "Analyst"


class RoleSpec(NamedTuple):
    name: str
    description: str
    codes: list[PermissionCode]


class AccountSpec(NamedTuple):
    email: str
    mobile: str
    full_name: str
    role: str


#: The owner role is minted by ``sync_permission_catalogue()`` with every permission;
#: these two are the narrower ones the demo needs to show a guard refusing something.
DEMO_ROLES: list[RoleSpec] = [
    RoleSpec(
        name=REVIEWER_ROLE,
        description="Works the risk queue and can override a decision.",
        codes=[
            PermissionCode.CHECK_RUN,
            PermissionCode.CHECK_READ,
            PermissionCode.RISK_ORDER_READ,
            PermissionCode.RISK_ORDER_REVIEW,
        ],
    ),
    RoleSpec(
        name=ANALYST_ROLE,
        description="Read-only access to checks, orders and configuration.",
        codes=[
            PermissionCode.CHECK_READ,
            PermissionCode.RISK_ORDER_READ,
            PermissionCode.CREDENTIAL_READ,
            PermissionCode.APIKEY_READ,
        ],
    ),
]

DEMO_ACCOUNTS: list[AccountSpec] = [
    AccountSpec(DEMO_EMAIL, DEMO_MOBILE, DEMO_FULL_NAME, DEFAULT_ROLE_NAME),
    AccountSpec("reviewer@fraudcheck.test", "01712345679", "Demo Reviewer", REVIEWER_ROLE),
    AccountSpec("analyst@fraudcheck.test", "01712345680", "Demo Analyst", ANALYST_ROLE),
]


async def schema_is_current() -> bool:
    """
    Whether the tables the seeder writes to actually exist.

    Without this, seeding a database that has not been migrated fails deep inside
    asyncpg with ``relation "permission" does not exist`` and a screenful of
    SQLAlchemy frames, which says nothing about the fix being `just migrate`.
    """
    statement = text("SELECT to_regclass(:qualified_name)")
    async with AsyncSessionLocal() as session:
        for table in (
            "user",
            "permission",
            "role",
            "userrolelink",
            "checkrequest",
            "providerresult",
        ):
            found = await session.scalar(statement, {"qualified_name": f"public.{table}"})
            if found is None:
                return False
    return True


async def sync_permissions() -> int:
    """
    Mint the permission catalogue and the owner role (FR-1.11). Idempotent.

    Also exposed as ``just permissions`` so a deployment can run it after ``just
    migrate`` without seeding demo data.
    """
    async with AsyncSessionLocal() as session:
        permissions = await AuthorizationService(session).sync_permission_catalogue()
        return len(permissions)


async def seed_demo_roles() -> list[Role]:
    """Create or refresh the demo roles so each holds exactly its declared codes."""
    async with AsyncSessionLocal() as session:
        return [await _upsert_role(session, spec) for spec in DEMO_ROLES]


async def _upsert_role(session: AsyncSession, spec: RoleSpec) -> Role:
    role = (await session.exec(select(Role).where(Role.name == spec.name))).first()

    if role is None:
        role = Role(name=spec.name, description=spec.description)
    else:
        role.description = spec.description
        role.is_active = True
    session.add(role)
    await session.commit()
    await session.refresh(role)

    permissions = (
        await session.exec(
            select(Permission).where(
                col(Permission.code).in_([code.value for code in spec.codes]),
                Permission.is_active,
            )
        )
    ).all()
    # assign_permissions replaces the role's grants wholesale, which is what makes a
    # re-run converge on the declared set instead of accumulating.
    await AuthorizationService(session).assign_permissions(
        [permission.public_id for permission in permissions], role
    )
    return role


async def seed_demo_accounts() -> list[tuple[User, str]]:
    """Create or refresh every demo account and its role. Returns ``(user, role)`` pairs."""
    async with AsyncSessionLocal() as session:
        owner = await _upsert_user(session, DEMO_ACCOUNTS[0])
        seeded = [(owner, DEMO_ACCOUNTS[0].role)]

        for spec in DEMO_ACCOUNTS[1:]:
            user = await _upsert_user(session, spec)
            seeded.append((user, spec.role))

        # Grants are recorded as made by the owner — the tenant's own account, so the
        # audit trail reads the way a real assignment would.
        authorization_service = AuthorizationService(session)
        for user, role_name in seeded:
            role = (
                await session.exec(select(Role).where(Role.name == role_name, Role.is_active))
            ).first()
            if role is None:
                print(f"  ! role {role_name!r} is missing — run `just permissions`")
                continue
            await authorization_service.assign_roles(owner, [role.public_id], user.public_id)

        return seeded


async def _upsert_user(session: AsyncSession, spec: AccountSpec) -> User:
    """
    Create the account, or reset an existing one to a known-good state.

    The password, status and lockout counters are all rewritten: a demo account that
    someone locked out by fat-fingering the password should come back with `just seed`.
    """
    user = (await session.exec(select(User).where(User.email == spec.email))).one_or_none()

    if user is None:
        user = User(
            email=spec.email,
            mobile=spec.mobile,
            password_hash=get_password_hash(DEMO_PASSWORD),
            full_name=spec.full_name,
            locale=Locale.EN,
            status=UserStatus.ACTIVE,
        )
    else:
        user.mobile = spec.mobile
        user.full_name = spec.full_name
        user.password_hash = get_password_hash(DEMO_PASSWORD)
        user.status = UserStatus.ACTIVE
        user.is_active = True
        user.failed_login_attempts = 0
        user.locked_until = None

    session.add(user)
    await session.commit()
    await session.refresh(user)
    return user


HISTORICAL_CHECK_COUNT = 50


def demo_phone_numbers(count: int = HISTORICAL_CHECK_COUNT) -> list[str]:
    """
    Build the number list the demo history is made of.

    Every reserved number comes first, so a reviewer opening the history immediately
    sees the interesting cases; the rest are filled in deterministically.
    """
    reserved = list(RESERVED_SCENARIOS)
    filler = [f"017{index:08d}" for index in range(10_000_000, 10_000_000 + count)]
    return (reserved + filler)[:count]


async def seed_historical_checks(merchant: User) -> int:
    """
    Give the demo merchant a check history (FR-11.5).

    Scored through the real scoring function over the real mock scenarios, so the
    seeded history and a live mock check of the same number agree — a demo where the
    stored score and the streamed score differ is worse than no demo.
    """
    async with AsyncSessionLocal() as session:
        already = await session.scalar(
            select(func.count())
            .select_from(CheckRequest)
            .where(CheckRequest.user_id == merchant.id)
        )
        if already:
            return 0

        now = datetime.now(UTC)
        for index, phone in enumerate(demo_phone_numbers()):
            stats_by_provider: dict[ProviderEnum, DeliveryStats] = {}
            legs: list[tuple[ProviderEnum, ProviderStatus, DeliveryStats | None]] = []

            for provider in ProviderEnum:
                scenario = scenario_for(phone, provider)
                if scenario.fails or scenario.times_out:
                    legs.append((provider, ProviderStatus.UNAVAILABLE, None))
                    continue
                stats = DeliveryStats(
                    total_orders=scenario.total_orders,
                    delivered=scenario.delivered,
                    returned=max(scenario.total_orders - scenario.delivered, 0),
                    cancelled=0,
                )
                stats_by_provider[provider] = stats
                legs.append((provider, ProviderStatus.OK, stats))

            score = score_courier_history(stats_by_provider)
            # Spread over the past few weeks so the history list has a shape to it.
            created_at = now - timedelta(hours=index * 7)
            check = CheckRequest(
                user_id=merchant.id,
                phone_normalized=phone,
                source=CheckSource.WEB if index % 3 else CheckSource.API,
                risk_score=score.score,
                risk_band=score.band,
                created_at=created_at,
                updated_at=created_at,
            )
            session.add(check)
            await session.flush()

            for provider, status, stats in legs:
                session.add(
                    ProviderResult(
                        check_request_id=check.id,
                        provider=provider,
                        status=status,
                        total_orders=stats.total_orders if stats else None,
                        delivered=stats.delivered if stats else None,
                        returned=stats.returned if stats else None,
                        cancelled=stats.cancelled if stats else None,
                        latency_ms=180 + index,
                        created_at=created_at,
                        updated_at=created_at,
                    )
                )

        await session.commit()
        return HISTORICAL_CHECK_COUNT


async def main() -> None:
    if settings.ENVIRONMENT is Environment.PRODUCTION:
        print("Refusing to seed a PRODUCTION environment.", file=sys.stderr)
        raise SystemExit(1)

    if not await schema_is_current():
        print(
            "Database schema is missing or out of date — run `just migrate` first.",
            file=sys.stderr,
        )
        raise SystemExit(1)

    permission_count = await sync_permissions()
    roles = await seed_demo_roles()
    accounts = await seed_demo_accounts()
    # The history hangs off the owner: it is the account the demo signs in as, and
    # tenant scoping means checks seeded anywhere else would be invisible from it.
    owner, _ = accounts[0]
    seeded_checks = await seed_historical_checks(owner)

    print(f"Synced {permission_count} permissions")
    print(f"Seeded {len(roles) + 1} roles: {DEFAULT_ROLE_NAME}, {', '.join(r.name for r in roles)}")
    print()
    print(f"Seeded {len(accounts)} demo accounts (password: {DEMO_PASSWORD})")
    for user, role_name in accounts:
        print(f"  {user.email:<28} {user.mobile:<12} {role_name:<14} {user.public_id}")
    print()
    print(f"  mode      : {'mock' if settings.MOCK_MODE else 'live'}")
    print(
        f"Seeded {seeded_checks} historical checks"
        if seeded_checks
        else "Historical checks already present, left alone"
    )
    print()
    print("Still to seed as its tables land:")
    print("  M6 — 200 OrderContext rows with labelled AssessmentOutcomes (FR-11.5)")


if __name__ == "__main__":
    asyncio.run(main())
