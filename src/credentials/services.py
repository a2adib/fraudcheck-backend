"""Credential vault services (FR-2)."""

import logging
from datetime import UTC, datetime

from pydantic import SecretStr
from redis.exceptions import RedisError
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from src.common.exceptions import HTTP404, HTTP409, HTTP501, HTTP502
from src.common.mixins import require_id
from src.config import settings
from src.credentials.enums import CredentialStatus
from src.credentials.models import CourierCredential
from src.credentials.schemas import CredentialCreate, CredentialOut, CredentialUpdate
from src.credentials.vault import VaultError, build_aad, decrypt, encrypt, mask_username
from src.logistics.enums import ProviderEnum
from src.logistics.exceptions import ProviderAuthError, ProviderError
from src.logistics.schemas import DecryptedCredential
from src.logistics.token_manager import has_token_manager, token_manager_for
from src.users.models import User

logger = logging.getLogger(__name__)

USERNAME_FIELD = "username"
PASSWORD_FIELD = "password"  # noqa: S105 — a field name used as GCM associated data


class CredentialService:
    def __init__(self, session: AsyncSession) -> None:
        """Vault operations for one request."""
        self.session = session

    # ── Reads ───────────────────────────────────────────────────────────────

    async def list_credentials(self, user: User) -> list[CourierCredential]:
        rows = await self.session.exec(
            select(CourierCredential)
            .where(
                CourierCredential.user_id == require_id(user),
                CourierCredential.is_active,
            )
            .order_by(CourierCredential.provider)
        )
        return list(rows.all())

    async def get_or_404(self, user: User, provider: ProviderEnum) -> CourierCredential:
        credential = await self._find(user, provider)
        if credential is None:
            raise HTTP404(detail=f"No {provider.value} credential stored")
        return credential

    async def _find(self, user: User, provider: ProviderEnum) -> CourierCredential | None:
        rows = await self.session.exec(
            select(CourierCredential).where(
                CourierCredential.user_id == require_id(user),
                CourierCredential.provider == provider,
                CourierCredential.is_active,
            )
        )
        return rows.first()

    # ── Writes ──────────────────────────────────────────────────────────────

    async def create(self, user: User, payload: CredentialCreate) -> CourierCredential:
        """AC-2.3. One credential per provider per merchant, while it is active."""
        if await self._find(user, payload.provider) is not None:
            raise HTTP409(detail=f"A {payload.provider.value} credential already exists")

        credential = CourierCredential(
            user_id=require_id(user),
            provider=payload.provider,
            username_encrypted=self._seal(user, payload.provider, USERNAME_FIELD, payload.username),
            password_encrypted=self._seal(user, payload.provider, PASSWORD_FIELD, payload.password),
            key_version=settings.ENCRYPTION_KEY_VERSION,
            status=CredentialStatus.UNTESTED,
        )
        self.session.add(credential)
        await self.session.commit()
        await self.session.refresh(credential)
        logger.info("Stored %s credential for tenant %s", payload.provider.value, user.public_id)
        return credential

    async def update(
        self, user: User, provider: ProviderEnum, payload: CredentialUpdate
    ) -> CourierCredential:
        credential = await self.get_or_404(user, provider)

        if payload.username is not None:
            credential.username_encrypted = self._seal(
                user, provider, USERNAME_FIELD, payload.username
            )
        if payload.password is not None:
            credential.password_encrypted = self._seal(
                user, provider, PASSWORD_FIELD, payload.password
            )

        if payload.username is not None or payload.password is not None:
            # Whatever we knew about the old secret says nothing about the new one.
            credential.status = CredentialStatus.UNTESTED
            credential.last_verified_at = None
            credential.key_version = settings.ENCRYPTION_KEY_VERSION

        self.session.add(credential)
        await self.session.commit()
        await self.session.refresh(credential)
        logger.info("Updated %s credential for tenant %s", provider.value, user.public_id)
        return credential

    async def delete(self, user: User, provider: ProviderEnum) -> None:
        """
        Soft delete (AC-2.7).

        The row survives so the audit trail still resolves, but every read filters on
        ``is_active``, so a check immediately reports ``no_credential``.
        """
        credential = await self.get_or_404(user, provider)
        credential.is_active = False
        self.session.add(credential)
        await self.session.commit()

        # A cached session token outlives the credential that minted it otherwise, and
        # would keep answering checks for a provider the merchant has disconnected.
        await self._invalidate_token(user, credential)
        logger.info("Removed %s credential for tenant %s", provider.value, user.public_id)

    # ── Verification (FR-2.4) ───────────────────────────────────────────────

    async def verify(self, user: User, provider: ProviderEnum) -> CourierCredential:
        """
        Test a stored credential against the live provider.

        A rejected login is recorded and returned as ``invalid`` with a 200 (AC-2.5); a
        provider that is merely unreachable leaves ``status`` untouched and raises 502,
        because "we could not tell" and "these are wrong" must not look the same to a
        merchant staring at a red badge. A provider with no login implemented yet is a
        501 for the same reason — it is a gap in this service, not a bad password.

        In mock mode nothing is contacted and the credential is reported valid, which is
        what makes the demo work with invented credentials (FR-11.1).
        """
        credential = await self.get_or_404(user, provider)
        if not settings.MOCK_MODE and not has_token_manager(provider):
            raise HTTP501(detail=f"{provider.value} credentials cannot be verified yet")

        decrypted = self.decrypt(user, credential)

        status = CredentialStatus.VALID
        if not settings.MOCK_MODE:
            manager = token_manager_for(decrypted)
            # Force a real login: a cached token proves the credential worked *once*,
            # which is not what a merchant clicking "verify" is asking.
            await manager.invalidate()
            try:
                await manager.get_token()
            except ProviderAuthError:
                status = CredentialStatus.INVALID
            except ProviderError as exc:
                logger.warning("Could not verify %s credential: %s", provider.value, exc)
                raise HTTP502(detail=f"{provider.value} is unreachable, try again") from exc

        credential.status = status
        credential.last_verified_at = datetime.now(UTC)
        self.session.add(credential)
        await self.session.commit()
        await self.session.refresh(credential)
        return credential

    # ── Decryption ──────────────────────────────────────────────────────────

    def decrypt(self, user: User, credential: CourierCredential) -> DecryptedCredential:
        """Decrypt in memory for exactly one call — nothing here is ever persisted."""
        return DecryptedCredential(
            provider=credential.provider,
            user_public_id=user.public_id,
            username=SecretStr(
                decrypt(
                    credential.username_encrypted,
                    build_aad(user.public_id, credential.provider, USERNAME_FIELD),
                    credential.key_version,
                )
            ),
            password=SecretStr(
                decrypt(
                    credential.password_encrypted,
                    build_aad(user.public_id, credential.provider, PASSWORD_FIELD),
                    credential.key_version,
                )
            ),
        )

    async def decrypted_by_provider(self, user: User) -> dict[ProviderEnum, DecryptedCredential]:
        """
        Every usable credential this merchant has, in one query.

        The check orchestrator calls this once per check; doing it per provider would
        put a database round-trip inside the fan-out for no reason.
        """
        return {
            credential.provider: self.decrypt(user, credential)
            for credential in await self.list_credentials(user)
        }

    # ── Internals ───────────────────────────────────────────────────────────

    def _seal(self, user: User, provider: ProviderEnum, field: str, secret: SecretStr) -> str:
        return encrypt(secret.get_secret_value(), build_aad(user.public_id, provider, field))

    async def _invalidate_token(self, user: User, credential: CourierCredential) -> None:
        try:
            decrypted = self.decrypt(user, credential)
            await token_manager_for(decrypted).invalidate()
        except (ProviderError, VaultError, RedisError):
            logger.warning(
                "Could not drop the cached %s token for tenant %s",
                credential.provider.value,
                user.public_id,
            )


def to_out(credential: CourierCredential, username_masked: str) -> CredentialOut:
    return CredentialOut(
        public_id=credential.public_id,
        provider=credential.provider,
        username_masked=username_masked,
        status=credential.status,
        last_verified_at=credential.last_verified_at,
        created_at=credential.created_at,
        updated_at=credential.updated_at,
    )


def masked_username_for(
    service: CredentialService, user: User, credential: CourierCredential
) -> str:
    """Decrypt just far enough to show the merchant which account this is (AC-2.1)."""
    return mask_username(service.decrypt(user, credential).username.get_secret_value())
