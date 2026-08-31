"""Check routes (FR-6)."""

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, status
from sqlmodel.ext.asyncio.session import AsyncSession
from sse_starlette.sse import EventSourceResponse

from src.auth.enums import PermissionCode
from src.auth.permissions import require_permission
from src.auth.utils import get_client_ip, get_current_user
from src.checks.schemas import CheckCreate, CheckCreatedOut, CheckDetailOut, CheckResultOut
from src.checks.services import CheckService
from src.common.mixins import require_id
from src.common.phone import mask_phone
from src.common.response import StandardResponse, create_response
from src.database import get_session
from src.securities.services import create_activity_log
from src.users.models import User

logger = logging.getLogger(__name__)

router = APIRouter()

SessionDep = Annotated[AsyncSession, Depends(get_session)]
CurrentUserDep = Annotated[User, Depends(get_current_user)]
ClientIPDep = Annotated[str, Depends(get_client_ip)]


@router.post(
    "",
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(require_permission(PermissionCode.CHECK_RUN))],
)
async def create_check(
    payload: CheckCreate,
    session: SessionDep,
    user: CurrentUserDep,
    client_ip: ClientIPDep,
) -> StandardResponse[CheckCreatedOut]:
    """
    AC-6.1. Accepted, not completed — the work happens when the stream is opened.

    202 is the honest status: nothing has been checked yet, and the body says where to
    watch it happen.
    """
    check = await CheckService(session).create(user, payload)
    await create_activity_log(
        session,
        user_id=require_id(user),
        ip_address=client_ip,
        description="Ran a customer check",
        # The audit trail gets the masked number: FR-14.5 forbids full phone numbers in
        # logs, and an audit row is a log that happens to live in Postgres.
        data={"check": check.public_id, "phone": mask_phone(check.phone_normalized)},
    )
    return create_response(
        CheckCreatedOut(
            check_id=check.public_id,
            stream_url=f"/checks/{check.public_id}/stream",
            phone_masked=mask_phone(check.phone_normalized),
        ),
        message="Check accepted",
    )


@router.post(
    "/lookup",
    dependencies=[Depends(require_permission(PermissionCode.CHECK_RUN))],
)
async def lookup_customer(
    payload: CheckCreate,
    session: SessionDep,
    user: CurrentUserDep,
    client_ip: ClientIPDep,
) -> StandardResponse[CheckResultOut]:
    """
    Check a customer and return the whole result in one response.

    The dashboard endpoint. ``POST /checks`` + the SSE stream exist for a UI that wants
    to fill in courier cards as each one resolves; this one waits for all of them and
    answers once, which is what a table row, a server-side render, or a checkout hook
    actually wants.

    The number goes in the body, never the path or a query string — a phone number in a
    URL ends up in access logs, proxy logs and browser history.
    """
    result = await CheckService(session).lookup(user, payload)
    await create_activity_log(
        session,
        user_id=require_id(user),
        ip_address=client_ip,
        description="Looked up a customer",
        data={
            "check": result.check_id,
            "phone": result.phone_masked,
            "band": result.risk_band.value,
            "cached": result.cached,
        },
    )
    return create_response(result, message="Check completed")


@router.get(
    "/{check_public_id}/stream",
    dependencies=[Depends(require_permission(PermissionCode.CHECK_READ))],
)
async def stream_check(
    check_public_id: str,
    session: SessionDep,
    user: CurrentUserDep,
) -> EventSourceResponse:
    """FR-6.2. Server-sent events, one per provider as it resolves."""
    service = CheckService(session)
    check = await service.get_or_404(user, check_public_id)
    return EventSourceResponse(service.event_stream(user, check))


@router.get(
    "/{check_public_id}",
    dependencies=[Depends(require_permission(PermissionCode.CHECK_READ))],
)
async def get_check(
    check_public_id: str,
    session: SessionDep,
    user: CurrentUserDep,
) -> StandardResponse[CheckDetailOut]:
    """Return the stored result, for a client that missed the stream or reloaded."""
    return create_response(await CheckService(session).detail(user, check_public_id))
