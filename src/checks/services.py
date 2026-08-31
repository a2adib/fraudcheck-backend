"""
Check orchestration, streaming and persistence (FR-6).

The shape of this module is driven by one requirement: a provider that is slow must
not delay the ones that are fast (AC-6.3). So the fan-out is not "gather everything,
then respond" — each leg publishes to a queue the moment it resolves, and the SSE
generator forwards whatever is in that queue.

The second driver is AC-6.10: a merchant who closes the tab mid-check must still end
up with a stored result. The fan-out therefore runs in a task that owns its own
database session, not inside the response generator, so cancelling the response
cancels the *streaming*, never the work.
"""

import asyncio
import json
import logging
from collections.abc import AsyncGenerator, Awaitable, Callable, Mapping

from pydantic import BaseModel
from redis.exceptions import RedisError
from sqlmodel import col, select
from sqlmodel.ext.asyncio.session import AsyncSession
from sse_starlette import ServerSentEvent

from src.cache.redis_client import get_async_redis
from src.checks.enums import CheckEvent, RiskBand
from src.checks.keys import check_cache_key
from src.checks.models import CheckRequest, ProviderResult
from src.checks.schemas import (
    CachedCheck,
    CheckCreate,
    CheckDetailOut,
    CheckDoneOut,
    CheckResultOut,
    CheckStartedOut,
    ProviderResultOut,
)
from src.checks.scoring import RiskScore, score_courier_history
from src.common.exceptions import HTTP404, HTTP422
from src.common.mixins import require_id
from src.common.phone import InvalidPhoneNumberError, mask_phone, normalize_bd_mobile
from src.config import settings
from src.credentials.services import CredentialService
from src.database import session_scope
from src.logistics.breaker import CircuitBreaker
from src.logistics.enums import ProviderEnum, ProviderErrorCode, ProviderStatus
from src.logistics.exceptions import CircuitOpenError, ProviderError
from src.logistics.providers.base import CourierAdapter
from src.logistics.providers.mock import mock_credential
from src.logistics.providers.registry import get_adapters
from src.logistics.schemas import DecryptedCredential, DeliveryStats
from src.users.models import User

logger = logging.getLogger(__name__)

#: Tasks are held here for their lifetime. Without a strong reference the event loop is
#: free to garbage-collect a running task, which is exactly how "the check vanished
#: when the client disconnected" bugs happen.
_RUNNING: set[asyncio.Task[None]] = set()


#: Called with each provider result as it lands. The streaming endpoint passes one; the
#: dashboard lookup passes nothing, because there is nobody watching yet.
Publish = Callable[[ProviderResultOut], Awaitable[None]]


class CheckOutcome(BaseModel):
    """A completed check: what every provider said, and what it scored."""

    providers: list[ProviderResultOut]
    score: RiskScore
    cached: bool


class LegOutcome(BaseModel):
    """One provider's leg: what the client is told, plus what gets stored."""

    result: ProviderResultOut
    stats: DeliveryStats | None = None
    raw_payload: dict[str, object] | None = None


def normalize_or_422(raw_phone: str) -> str:
    """FR-6.9 / AC-6.7. The check path answers a bad number with 422, not an auth error."""
    try:
        return normalize_bd_mobile(raw_phone)
    except InvalidPhoneNumberError as exc:
        raise HTTP422(detail=str(exc)) from exc


# ── One provider leg ─────────────────────────────────────────────────────────


async def execute_leg(
    provider: ProviderEnum,
    adapter: CourierAdapter,
    credential: DecryptedCredential | None,
    phone: str,
    user_public_id: str,
) -> LegOutcome:
    """
    Run one provider end to end, converting every failure into a reportable result.

    Nothing raises out of here. A leg failing is ordinary (FR-6.4), and the orchestrator
    above wants a value it can stream and store, not an exception to interpret.
    """
    if credential is None and settings.MOCK_MODE:
        # AC-11.1. A reviewer running the demo has connected nothing; the mock does not
        # authenticate, so requiring a credential here would make mock mode useless.
        credential = mock_credential(provider, user_public_id)

    if credential is None:
        # AC-2.7. A provider the merchant never connected, or disconnected since.
        return LegOutcome(
            result=ProviderResultOut(
                provider=provider,
                status=ProviderStatus.UNAVAILABLE,
                error_code=ProviderErrorCode.NO_CREDENTIAL,
            )
        )

    breaker = CircuitBreaker(provider, user_public_id)
    try:
        await breaker.allow()
    except CircuitOpenError:
        # AC-5.1. No HTTP call, no latency, no failure recorded — the breaker already
        # knows this provider is sick, and re-counting a call we never made would keep
        # it open forever.
        return LegOutcome(
            result=ProviderResultOut(
                provider=provider,
                status=ProviderStatus.UNAVAILABLE,
                error_code=ProviderErrorCode.CIRCUIT_OPEN,
            )
        )

    try:
        raw = await adapter.fetch(phone, credential)
        stats = adapter.normalize(raw)
    except ProviderError as exc:
        await breaker.record_failure()
        logger.info(
            "Provider %s failed for tenant %s: %s", provider.value, user_public_id, exc.error_code
        )
        return LegOutcome(
            result=ProviderResultOut(
                provider=provider, status=exc.status, error_code=exc.error_code
            )
        )
    except Exception:
        await breaker.record_failure()
        logger.exception("Provider %s raised unexpectedly", provider.value)
        return LegOutcome(
            result=ProviderResultOut(
                provider=provider,
                status=ProviderStatus.UNAVAILABLE,
                error_code=ProviderErrorCode.PROVIDER_ERROR,
            )
        )

    await breaker.record_success()
    return LegOutcome(
        result=ProviderResultOut(
            provider=provider,
            status=ProviderStatus.OK,
            total_orders=stats.total_orders,
            delivered=stats.delivered,
            returned=stats.returned,
            cancelled=stats.cancelled,
            customer_rating=stats.customer_rating,
            latency_ms=raw.latency_ms,
        ),
        stats=stats,
        raw_payload=dict(raw.payload),
    )


# ── Cache (FR-6.8) ───────────────────────────────────────────────────────────


async def read_cached(user_public_id: str, phone: str) -> CachedCheck | None:
    try:
        raw = await get_async_redis().get(check_cache_key(user_public_id, phone))
    except RedisError:
        logger.warning("Check cache unreadable, running the providers instead")
        return None
    if not raw:
        return None
    try:
        return CachedCheck.model_validate_json(raw)
    except ValueError:
        logger.warning("Discarding an unreadable cached check")
        return None


async def write_cached(user_public_id: str, phone: str, cached: CachedCheck) -> None:
    try:
        await get_async_redis().set(
            check_cache_key(user_public_id, phone),
            cached.model_dump_json(),
            ex=settings.CHECK_CACHE_TTL_SECONDS,
        )
    except RedisError:
        logger.warning("Could not cache the check result")


# ── Orchestration ────────────────────────────────────────────────────────────


async def _fan_out(
    phone: str,
    user_public_id: str,
    credentials: Mapping[ProviderEnum, DecryptedCredential],
    publish: Publish | None = None,
) -> list[LegOutcome]:
    """
    FR-6.3. Every provider at once; each result published the instant it lands.

    ``gather(return_exceptions=True)`` is belt and braces over ``execute_leg``'s own
    handling: a bug in a leg must degrade that provider, never the whole check.
    """

    async def run(provider: ProviderEnum, adapter: CourierAdapter) -> LegOutcome:
        outcome = await execute_leg(
            provider, adapter, credentials.get(provider), phone, user_public_id
        )
        if publish:
            await publish(outcome.result)
        return outcome

    adapters = get_adapters()
    results = await asyncio.gather(
        *(run(provider, adapter) for provider, adapter in adapters.items()),
        return_exceptions=True,
    )

    outcomes: list[LegOutcome] = []
    for provider, result in zip(adapters, results, strict=True):
        if isinstance(result, BaseException):
            logger.error(
                "Provider leg %s crashed outside its own error handling",
                provider.value,
                exc_info=result,
            )
            outcomes.append(
                LegOutcome(
                    result=ProviderResultOut(
                        provider=provider,
                        status=ProviderStatus.UNAVAILABLE,
                        error_code=ProviderErrorCode.PROVIDER_ERROR,
                    )
                )
            )
            continue
        outcomes.append(result)
    return outcomes


def score_from(outcomes: list[LegOutcome]) -> RiskScore:
    """AC-7.7. Only providers that answered get a vote."""
    return score_courier_history(
        {
            outcome.result.provider: outcome.stats
            for outcome in outcomes
            if outcome.stats is not None and outcome.result.status is ProviderStatus.OK
        }
    )


async def _persist(
    session: AsyncSession,
    check: CheckRequest,
    outcomes: list[LegOutcome],
    score: RiskScore,
) -> None:
    """FR-6.7. Everything is stored *before* ``done`` is emitted."""
    for outcome in outcomes:
        result = outcome.result
        session.add(
            ProviderResult(
                check_request_id=require_id(check),
                provider=result.provider,
                status=result.status,
                total_orders=result.total_orders,
                delivered=result.delivered,
                returned=result.returned,
                cancelled=result.cancelled,
                customer_rating=result.customer_rating,
                latency_ms=result.latency_ms,
                error_code=result.error_code,
                raw_payload=outcome.raw_payload,
            )
        )

    check.risk_score = score.score
    check.risk_band = score.band
    session.add(check)
    await session.commit()


async def execute_check(
    session: AsyncSession,
    user: User,
    check: CheckRequest,
    publish: Publish | None = None,
) -> CheckOutcome:
    """
    Run one check to completion: cache, fan-out, score, persist, cache.

    The single path both entry points take. ``POST /checks/lookup`` calls it and waits;
    the SSE stream calls it from a background task with a ``publish`` callback. Sharing
    it is what keeps a dashboard lookup and a streamed check from ever disagreeing about
    the same phone number.
    """
    phone = check.phone_normalized
    user_public_id = user.public_id

    cached = await read_cached(user_public_id, phone)
    if cached:
        # AC-6.8. A cache hit changes where the numbers came from, not what the client
        # or the stored history sees.
        outcomes = [LegOutcome(result=result) for result in cached.providers]
        if publish:
            for outcome in outcomes:
                await publish(outcome.result)
        score = cached.score
    else:
        credentials = await CredentialService(session).decrypted_by_provider(user)
        outcomes = await _fan_out(phone, user_public_id, credentials, publish)
        score = score_from(outcomes)

    await _persist(session, check, outcomes, score)

    if not cached and any(outcome.result.status is ProviderStatus.OK for outcome in outcomes):
        # Never cache a result nobody answered: 15 minutes of "all providers down" would
        # outlive the outage that caused it.
        await write_cached(
            user_public_id,
            phone,
            CachedCheck(providers=[outcome.result for outcome in outcomes], score=score),
        )

    return CheckOutcome(
        providers=[outcome.result for outcome in outcomes], score=score, cached=bool(cached)
    )


async def _run_check(
    queue: asyncio.Queue[ServerSentEvent | None],
    user_public_id: str,
    check_public_id: str,
) -> None:
    """
    Run the whole check in a task that survives the client hanging up (AC-6.10).

    The ``finally`` is load-bearing: whatever happens, the stream is closed. A consumer
    blocked on the queue with no sentinel coming is a hung connection, and would be a
    far worse failure than the one that caused it.
    """
    try:
        async with session_scope() as session:
            user = (
                await session.exec(select(User).where(User.public_id == user_public_id))
            ).first()
            check = (
                await session.exec(
                    select(CheckRequest).where(CheckRequest.public_id == check_public_id)
                )
            ).first()
            if user is None or check is None:
                logger.warning("Check %s vanished before it could run", check_public_id)
                return

            async def publish(result: ProviderResultOut) -> None:
                await queue.put(_event(CheckEvent.PROVIDER_RESULT, result))

            outcome = await execute_check(session, user, check, publish)

            await queue.put(_event(CheckEvent.SCORE, outcome.score, cached=outcome.cached))
            await queue.put(
                _event(
                    CheckEvent.DONE,
                    CheckDoneOut(
                        check_id=check_public_id,
                        risk_score=outcome.score.score,
                        risk_band=outcome.score.band,
                    ),
                )
            )
    except Exception:
        logger.exception("Check %s failed", check_public_id)
        await queue.put(_event(CheckEvent.DONE, CheckDoneOut(check_id=check_public_id)))
    finally:
        await queue.put(None)


async def wait_for_running_checks() -> None:
    """
    Wait for in-flight fan-outs to finish.

    Checks outlive the response that started them (AC-6.10), so "the request is over"
    is not "the work is over". Shutdown and tests both need to be able to ask.
    """
    while _RUNNING:
        await asyncio.wait(set(_RUNNING))


def _event(name: CheckEvent, payload: BaseModel, **extra: object) -> ServerSentEvent:
    data = payload.model_dump(mode="json")
    data.update(extra)
    return ServerSentEvent(event=name.value, data=json.dumps(data))


# ── Service ──────────────────────────────────────────────────────────────────


class CheckService:
    def __init__(self, session: AsyncSession) -> None:
        """Check operations for one request."""
        self.session = session

    async def create(self, user: User, payload: CheckCreate) -> CheckRequest:
        """FR-6.1. Records the intent; the providers are called when the stream opens."""
        check = CheckRequest(
            user_id=require_id(user),
            phone_normalized=normalize_or_422(payload.phone),
            source=payload.source,
        )
        self.session.add(check)
        await self.session.commit()
        await self.session.refresh(check)
        logger.info(
            "Check %s created for tenant %s (%s)",
            check.public_id,
            user.public_id,
            mask_phone(check.phone_normalized),
        )
        return check

    async def lookup(self, user: User, payload: CheckCreate) -> CheckResultOut:
        """
        Create and run a check in one call, for a dashboard that just wants the answer.

        This blocks for as long as the slowest courier takes, bounded by each adapter's
        own timeout (FR-3.4) — a few seconds in the worst case, and typically a cache
        hit. Anything that wants to render results as they arrive should use the SSE
        stream instead; this exists because most dashboards would only buffer the whole
        stream anyway, and then have to reassemble it.
        """
        check = await self.create(user, payload)
        outcome = await execute_check(self.session, user, check)
        return CheckResultOut(
            check_id=check.public_id,
            phone_masked=mask_phone(check.phone_normalized),
            source=check.source,
            created_at=check.created_at,
            risk_score=outcome.score.score,
            risk_band=outcome.score.band,
            cached=outcome.cached,
            score=outcome.score,
            providers=outcome.providers,
        )

    async def get_or_404(self, user: User, check_public_id: str) -> CheckRequest:
        """AC-8.4. Another tenant's check is *absent*, not forbidden."""
        check = (
            await self.session.exec(
                select(CheckRequest).where(
                    CheckRequest.public_id == check_public_id,
                    CheckRequest.user_id == require_id(user),
                    CheckRequest.is_active,
                )
            )
        ).first()
        if check is None:
            raise HTTP404(detail="Check not found")
        return check

    async def detail(self, user: User, check_public_id: str) -> CheckDetailOut:
        check = await self.get_or_404(user, check_public_id)
        results = (
            await self.session.exec(
                select(ProviderResult)
                .where(ProviderResult.check_request_id == require_id(check))
                .order_by(col(ProviderResult.provider))
            )
        ).all()
        return CheckDetailOut(
            check_id=check.public_id,
            phone_masked=mask_phone(check.phone_normalized),
            source=check.source,
            created_at=check.created_at,
            risk_score=check.risk_score,
            risk_band=RiskBand(check.risk_band) if check.risk_band else None,
            providers=[
                ProviderResultOut(
                    provider=result.provider,
                    status=result.status,
                    error_code=result.error_code,
                    total_orders=result.total_orders,
                    delivered=result.delivered,
                    returned=result.returned,
                    cancelled=result.cancelled,
                    customer_rating=result.customer_rating,
                    latency_ms=result.latency_ms,
                )
                for result in results
            ],
        )

    async def event_stream(
        self, user: User, check: CheckRequest
    ) -> AsyncGenerator[ServerSentEvent]:
        """FR-6.5. ``started`` -> ``provider_result`` xN in completion order -> ``score`` -> ``done``."""
        yield _event(
            CheckEvent.STARTED,
            CheckStartedOut(
                check_id=check.public_id,
                phone_masked=mask_phone(check.phone_normalized),
                providers=list(get_adapters()),
            ),
        )

        queue: asyncio.Queue[ServerSentEvent | None] = asyncio.Queue()
        task = asyncio.create_task(_run_check(queue, user.public_id, check.public_id))
        _RUNNING.add(task)
        task.add_done_callback(_RUNNING.discard)

        while True:
            event = await queue.get()
            if event is None:
                return
            yield event
