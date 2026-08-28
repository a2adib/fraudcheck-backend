"""Skeleton smoke tests — the harness itself, plus the pieces M0 delivers."""

import pytest
from httpx import AsyncClient
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from src.common.phone import InvalidPhoneNumberError, mask_phone, normalize_bd_mobile
from src.users.models import User


class TestHealth:
    async def test_health_reports_every_component(self, client: AsyncClient):
        response = await client.get("/health")

        assert response.status_code == 200
        body = response.json()
        assert body["api"] == "up"
        assert body["postgres"] == "up"
        assert body["redis"] == "up"

    async def test_ac_11_5_health_reports_mock_mode(self, client: AsyncClient):
        """AC-11.5 — the API advertises that it is running on mocked adapters."""
        response = await client.get("/health")

        assert response.json()["mode"] == "mock"


class TestPhoneNormalization:
    @pytest.mark.parametrize(
        "raw",
        ["01712345678", "+8801712345678", "8801712345678", "017-1234-5678", "1712345678"],
    )
    def test_ac_6_6_every_accepted_form_normalizes_identically(self, raw: str):
        """AC-6.6 — all accepted input forms collapse to one canonical number."""
        assert normalize_bd_mobile(raw) == "01712345678"

    @pytest.mark.parametrize("raw", ["01212345678", "0171234567", "not-a-number", ""])
    def test_ac_6_7_invalid_numbers_are_rejected(self, raw: str):
        """AC-6.7 — an invalid operator prefix or length is refused, not coerced."""
        with pytest.raises(InvalidPhoneNumberError):
            normalize_bd_mobile(raw)

    def test_mask_hides_the_subscriber_digits(self):
        """FR-14.5 — logs may carry a masked phone, never the full number."""
        masked = mask_phone("01712345678")

        assert masked == "017*****678"
        assert "1234" not in masked


class TestHarness:
    async def test_savepoint_isolation_rolls_back(self, async_session: AsyncSession):
        """A row committed inside a test must not survive into the next one."""
        async_session.add(User(email="harness@example.com", password_hash="x", full_name="Harness"))
        await async_session.commit()

        found = (
            await async_session.exec(select(User).where(User.email == "harness@example.com"))
        ).first()
        assert found is not None

    async def test_previous_test_left_no_rows(self, async_session: AsyncSession):
        """Proves the savepoint from the previous test was rolled back."""
        found = (
            await async_session.exec(select(User).where(User.email == "harness@example.com"))
        ).first()

        assert found is None
