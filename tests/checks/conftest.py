"""Fixtures for the check pipeline."""

import json
from collections.abc import AsyncGenerator, Callable
from contextlib import asynccontextmanager

import pytest
from httpx import AsyncClient
from sqlmodel.ext.asyncio.session import AsyncSession

import src.checks.services


@pytest.fixture(autouse=True)
def check_session_scope(monkeypatch: pytest.MonkeyPatch, async_session: AsyncSession) -> None:
    """
    Point the orchestrator's own session at the test transaction.

    The fan-out deliberately runs outside the request, on a session it opens itself
    (AC-6.10) — which would otherwise write to the real database, outside the savepoint
    every test is rolled back with.
    """

    @asynccontextmanager
    async def scope() -> AsyncGenerator[AsyncSession]:
        yield async_session

    monkeypatch.setattr(src.checks.services, "session_scope", scope)


async def create_check(
    client: AsyncClient,
    phone: str = "01712345678",
    headers: dict[str, str] | None = None,
    **extra: object,
):
    return await client.post("/checks", json={"phone": phone, **extra}, headers=headers)


async def consume_stream(
    client: AsyncClient,
    url: str,
    stop_after: str | None = None,
    headers: dict[str, str] | None = None,
) -> list[tuple[str, dict]]:
    """
    Read an SSE stream into ``(event_name, payload)`` pairs.

    ``stop_after`` closes the connection as soon as that event arrives, which is how
    AC-6.10 simulates a merchant closing the tab mid-check.
    """
    events: list[tuple[str, dict]] = []
    event_name: str | None = None
    async with client.stream("GET", url, headers=headers) as response:
        assert response.status_code == 200, await response.aread()
        async for line in response.aiter_lines():
            if line.startswith("event:"):
                event_name = line.split(":", 1)[1].strip()
            elif line.startswith("data:") and event_name:
                events.append((event_name, json.loads(line.split(":", 1)[1].strip())))
                if stop_after and event_name == stop_after:
                    return events
    return events


def events_named(events: list[tuple[str, dict]], name: str) -> list[dict]:
    return [payload for event_name, payload in events if event_name == name]


@pytest.fixture
def stub_registry(monkeypatch: pytest.MonkeyPatch) -> Callable[[dict], None]:
    """Swap the adapter registry the orchestrator reads, for tests that need exact timing."""

    def install(adapters: dict) -> None:
        monkeypatch.setattr(src.checks.services, "get_adapters", lambda: adapters)

    return install
