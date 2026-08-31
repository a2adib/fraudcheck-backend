"""
Seed the demo dataset (FR-11.5).

Run with ``just seed``. Idempotent — re-running updates the demo merchant rather
than creating a second one, so it is safe to run after every migration.

Scope grows with the milestones. Today that is the permission catalogue (FR-1.11), the
demo merchant, and 50 historical checks. The rest of FR-11.5 (200 ``OrderContext`` rows
with labelled outcomes — enough to train the demo model for AC-11.8) lands with the
tables it needs, at M6.
"""

import asyncio
import sys
from datetime import UTC, datetime, timedelta

from sqlalchemy import func
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from src.auth.associations import UserRoleLink
from src.auth.services import AuthorizationService
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

DEMO_EMAIL = "demo@fraudcheck.test"
DEMO_MOBILE = "01712345678"
DEMO_PASSWORD = "DemoPassword123"  # noqa: S105 — a demo credential, printed on purpose.
DEMO_FULL_NAME = "Demo Merchant"


async def sync_permissions() -> int:
    """
    Mint the permission catalogue and the owner role (FR-1.11). Idempotent.

    Also exposed as ``just permissions`` so a deployment can run it after ``just
    migrate`` without seeding demo data.
    """
    async with AsyncSessionLocal() as session:
        permissions = await AuthorizationService(session).sync_permission_catalogue()
        return len(permissions)


async def seed_demo_merchant() -> User:
    """Create or refresh the demo merchant. Returns the persisted row."""
    async with AsyncSessionLocal() as session:
        existing = (await session.exec(select(User).where(User.email == DEMO_EMAIL))).one_or_none()

        authorization_service = AuthorizationService(session)

        if existing is not None:
            existing.password_hash = get_password_hash(DEMO_PASSWORD)
            existing.status = UserStatus.ACTIVE
            existing.is_active = True
            existing.failed_login_attempts = 0
            existing.locked_until = None
            session.add(existing)
            await session.commit()
            await session.refresh(existing)
            await _ensure_owner_role(session, existing)
            return existing

        merchant = User(
            email=DEMO_EMAIL,
            mobile=DEMO_MOBILE,
            password_hash=get_password_hash(DEMO_PASSWORD),
            full_name=DEMO_FULL_NAME,
            locale=Locale.EN,
            status=UserStatus.ACTIVE,
        )
        session.add(merchant)
        await session.commit()
        await session.refresh(merchant)
        await authorization_service.grant_default_role(merchant)
        return merchant


async def _ensure_owner_role(session: AsyncSession, merchant: User) -> None:
    """Grant the owner role to an already-seeded merchant that has no roles yet."""
    held = (
        await session.exec(select(UserRoleLink).where(UserRoleLink.user_id == merchant.id))
    ).first()
    if held is None:
        await AuthorizationService(session).grant_default_role(merchant)


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

    permission_count = await sync_permissions()
    merchant = await seed_demo_merchant()
    seeded_checks = await seed_historical_checks(merchant)

    print(f"Synced {permission_count} permissions and the owner role")
    print("Seeded demo merchant")
    print(f"  public_id : {merchant.public_id}")
    print(f"  email     : {DEMO_EMAIL}")
    print(f"  mobile    : {DEMO_MOBILE}")
    print(f"  password  : {DEMO_PASSWORD}")
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
