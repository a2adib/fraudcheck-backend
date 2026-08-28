"""
Sync the permission catalogue (FR-1.11).

Run with ``just permissions``, after ``just migrate``, on every environment — unlike
``just seed`` it creates no demo data, so it is safe in production.
"""

import asyncio

from scripts.seed_demo import sync_permissions


async def main() -> None:
    count = await sync_permissions()
    print(f"Synced {count} permissions and the owner role")


if __name__ == "__main__":
    asyncio.run(main())
