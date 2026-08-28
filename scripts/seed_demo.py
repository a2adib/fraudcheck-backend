"""
Seed the demo dataset (FR-11.5).

Run with ``just seed``. Idempotent — re-running updates the demo merchant rather
than creating a second one, so it is safe to run after every migration.

Scope grows with the milestones. Today only ``User`` exists, so only the demo
merchant is seeded. The rest of FR-11.5 (50 historical checks, 200 ``OrderContext``
rows with labelled outcomes — enough to train the demo model for AC-11.8) lands with
the tables it needs, at M4 and M6.
"""

import asyncio
import sys

from sqlmodel import select

from src.auth.utils import get_password_hash
from src.config import settings
from src.constants import Environment
from src.database import AsyncSessionLocal
from src.users.enums import Locale, UserStatus
from src.users.models import User

DEMO_EMAIL = "demo@fraudcheck.test"
DEMO_MOBILE = "01712345678"
DEMO_PASSWORD = "DemoPassword123"  # noqa: S105 — a demo credential, printed on purpose.
DEMO_FULL_NAME = "Demo Merchant"


async def seed_demo_merchant() -> User:
    """Create or refresh the demo merchant. Returns the persisted row."""
    async with AsyncSessionLocal() as session:
        existing = (await session.exec(select(User).where(User.email == DEMO_EMAIL))).one_or_none()

        if existing is not None:
            existing.password_hash = get_password_hash(DEMO_PASSWORD)
            existing.status = UserStatus.ACTIVE
            existing.is_active = True
            existing.failed_login_attempts = 0
            existing.locked_until = None
            session.add(existing)
            await session.commit()
            await session.refresh(existing)
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
        return merchant


async def main() -> None:
    if settings.ENVIRONMENT is Environment.PRODUCTION:
        print("Refusing to seed a PRODUCTION environment.", file=sys.stderr)
        raise SystemExit(1)

    merchant = await seed_demo_merchant()

    print("Seeded demo merchant")
    print(f"  public_id : {merchant.public_id}")
    print(f"  email     : {DEMO_EMAIL}")
    print(f"  mobile    : {DEMO_MOBILE}")
    print(f"  password  : {DEMO_PASSWORD}")
    print(f"  mode      : {'mock' if settings.MOCK_MODE else 'live'}")
    print()
    print("Still to seed as their tables land:")
    print("  M4 — 50 historical CheckRequest + ProviderResult rows")
    print("  M6 — 200 OrderContext rows with labelled AssessmentOutcomes (FR-11.5)")


if __name__ == "__main__":
    asyncio.run(main())
