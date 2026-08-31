"""Credential vault routes (FR-2)."""

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, status
from sqlmodel.ext.asyncio.session import AsyncSession

from src.auth.enums import PermissionCode
from src.auth.permissions import require_permission
from src.auth.utils import get_client_ip, get_current_user
from src.common.mixins import require_id
from src.common.response import MESSAGE_201, StandardResponse, create_response
from src.credentials.schemas import (
    CredentialCreate,
    CredentialOut,
    CredentialUpdate,
    CredentialVerifyOut,
)
from src.credentials.services import CredentialService, masked_username_for, to_out
from src.database import get_session
from src.logistics.enums import ProviderEnum
from src.securities.services import create_activity_log
from src.users.models import User

logger = logging.getLogger(__name__)

router = APIRouter()

SessionDep = Annotated[AsyncSession, Depends(get_session)]
CurrentUserDep = Annotated[User, Depends(get_current_user)]
ClientIPDep = Annotated[str, Depends(get_client_ip)]


@router.get(
    "",
    dependencies=[Depends(require_permission(PermissionCode.CREDENTIAL_READ))],
)
async def list_credentials(
    session: SessionDep, user: CurrentUserDep
) -> StandardResponse[list[CredentialOut]]:
    service = CredentialService(session)
    credentials = await service.list_credentials(user)
    return create_response(
        [
            to_out(credential, masked_username_for(service, user, credential))
            for credential in credentials
        ]
    )


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_permission(PermissionCode.CREDENTIAL_CREATE))],
)
async def create_credential(
    payload: CredentialCreate,
    session: SessionDep,
    user: CurrentUserDep,
    client_ip: ClientIPDep,
) -> StandardResponse[CredentialOut]:
    service = CredentialService(session)
    credential = await service.create(user, payload)
    await create_activity_log(
        session,
        user_id=require_id(user),
        ip_address=client_ip,
        description=f"Stored {payload.provider.value} courier credential",
        data={"provider": payload.provider.value, "credential": credential.public_id},
    )
    return create_response(
        to_out(credential, masked_username_for(service, user, credential)),
        message=MESSAGE_201,
    )


@router.patch(
    "/{provider}",
    dependencies=[Depends(require_permission(PermissionCode.CREDENTIAL_UPDATE))],
)
async def update_credential(
    provider: ProviderEnum,
    payload: CredentialUpdate,
    session: SessionDep,
    user: CurrentUserDep,
    client_ip: ClientIPDep,
) -> StandardResponse[CredentialOut]:
    service = CredentialService(session)
    credential = await service.update(user, provider, payload)
    await create_activity_log(
        session,
        user_id=require_id(user),
        ip_address=client_ip,
        description=f"Updated {provider.value} courier credential",
        data={"provider": provider.value, "credential": credential.public_id},
    )
    return create_response(to_out(credential, masked_username_for(service, user, credential)))


@router.delete(
    "/{provider}",
    dependencies=[Depends(require_permission(PermissionCode.CREDENTIAL_DELETE))],
)
async def delete_credential(
    provider: ProviderEnum,
    session: SessionDep,
    user: CurrentUserDep,
    client_ip: ClientIPDep,
) -> StandardResponse[None]:
    service = CredentialService(session)
    await service.delete(user, provider)
    await create_activity_log(
        session,
        user_id=require_id(user),
        ip_address=client_ip,
        description=f"Removed {provider.value} courier credential",
        data={"provider": provider.value},
    )
    return create_response(message="Deleted successfully")


@router.post(
    "/{provider}/verify",
    dependencies=[Depends(require_permission(PermissionCode.CREDENTIAL_UPDATE))],
)
async def verify_credential(
    provider: ProviderEnum,
    session: SessionDep,
    user: CurrentUserDep,
    client_ip: ClientIPDep,
) -> StandardResponse[CredentialVerifyOut]:
    """FR-2.4. Tests the stored credential against the live provider."""
    service = CredentialService(session)
    credential = await service.verify(user, provider)
    await create_activity_log(
        session,
        user_id=require_id(user),
        ip_address=client_ip,
        description=f"Verified {provider.value} courier credential",
        data={"provider": provider.value, "status": credential.status.value},
    )
    return create_response(
        CredentialVerifyOut(
            provider=credential.provider,
            status=credential.status,
            last_verified_at=credential.last_verified_at,
        )
    )
