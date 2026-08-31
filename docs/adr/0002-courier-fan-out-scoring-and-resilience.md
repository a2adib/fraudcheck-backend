# ADR-0002 — Courier fan-out: scoring, tokens, and the breaker

- **Status:** Accepted
- **Date:** 2026-08-30
- **Requirements touched:** FR-2, FR-3, FR-4, FR-5, FR-6, FR-7, FR-11

## Context

M2–M4 port the customer-behaviour lookup that runs in `govaly-backend` today and turn it into a
multi-tenant, scored, streaming check. The upstream code is proven in production, but it was
written for a single-tenant service with one platform-wide courier account. Three of its
assumptions do not survive the move, and FR-7 introduces a scoring formula that has to be
defensible on its own.

This ADR records the four decisions that a reviewer would otherwise have to reverse-engineer from
the diff.

## Decision 1 — The risk score blends toward neutral, and says so

FR-7's formula, implemented in `src/checks/scoring.py`:

```
delivered    = Σ delivered across providers with status=ok
returned     = Σ returned + cancelled
total        = delivered + returned            # total == 0 → score 50, insufficient_data

return_ratio = returned / total
base_risk    = return_ratio * 100
confidence   = min(1.0, total / 10)
score        = base_risk * confidence + 50 * (1 - confidence)
```

The `confidence` term is the whole argument. A customer with two orders and one return has a 50%
return rate, and reporting that as 50 risk points would be arithmetic impersonating evidence. The
score is therefore pulled toward a neutral 50 in proportion to how much history exists, so a thin
file reads as *"we don't know"* rather than *"suspicious"*. Ten orders is where the blend reaches
full confidence — a round number chosen for explainability, not fitted to data, and one of the
things M6's outcomes should be used to calibrate.

Bands are risk-oriented: `low` 0–25, `medium` 26–60, `high` 61–100, where **high means bad**.
Govaly's `Customer.behavior` badge is quality-oriented and inverted — its `HIGH` renders green —
so any value imported from there must go through FR-15.12's explicit mapping table, never by name.

Two consequences are asserted in tests rather than left to good intentions: providers that did not
answer are excluded entirely (AC-7.7 — silence must never score as a clean history), and identical
inputs produce byte-identical output (AC-7.8).

## Decision 2 — Tokens are per tenant, and Redis is the only store

`govaly-backend/src/logistics/token_manager.py` caches one token per courier under a global key,
logging in with credentials from the environment. Here every merchant brings their own credential
(FR-2), so a manager is constructed per `(provider, merchant)` and the cache key is namespaced by
`user_public_id` (FR-4.6).

Upstream also mirrors every token to `/tmp/courier_token_cache/*.json` so a Redis outage cannot
stop deliveries. That is a reasonable trade for one platform account and an unreasonable one for
per-merchant secrets: it would scatter merchant session tokens across every worker's filesystem,
outside the vault and outside its lifecycle. **Dropped.** If Redis is gone, the provider leg
reports `unavailable` and the check still returns a score from whatever else answered.

The waiter loop is also changed. Upstream polls only for a *token*, so a worker that dies holding
the lock stalls every waiter for the full poll window. Here each tick looks for a token and then
re-attempts the lock, so the moment an abandoned lock expires a waiter takes over and logs in
(AC-4.4).

Kept verbatim, because it is already right: the process-local `asyncio.Lock` in front of the Redis
lock (it collapses same-worker stampedes before they reach the network), the Lua compare-and-delete
release, the safety margin, and the grace period that serves a stale token when a refresh fails.

## Decision 3 — A login failure and a portal outage are different facts

Upstream raises its `CourierAuthError` for every login failure, including connection errors and
5xx responses. Ported as-is, that would mark a merchant's credential `invalid` (FR-2.4) every time
Pathao had a bad afternoon, and every merchant's credential at once.

`_do_refresh` now maps 400/401/403 to `ProviderAuthError` — *these credentials are wrong* — and
everything else, including transport failures, to `ProviderError` — *this portal is not answering*.
The verify endpoint uses that distinction directly: a rejected password is a 200 carrying
`{"status": "invalid"}` (AC-2.5), while an unreachable portal is a 502 that leaves the stored
status untouched.

## Decision 4 — The breaker is keyed per provider **and** per tenant

FR-5.1 says "per-provider, per-instance"; FR-5.6 says auth failures count toward the breaker. Taken
together and keyed by provider alone, one merchant with a stale portal password would trip the
circuit for every other merchant on the platform — a cross-tenant effect in a system whose central
constraint (§1.3) is that tenants do not affect each other.

The breaker key is therefore `breaker:{provider}:{user_public_id}`. Every acceptance criterion in
FR-5 still holds; what changes is the blast radius of one bad credential. The cost is that a
genuine provider-wide outage is discovered once per tenant rather than once globally — five wasted
calls per merchant, bounded by the same threshold.

Two implementation notes: state transitions run as Lua scripts so that half-open cannot hand its
single probe slot to two instances at once, and every deadline is compared against a timestamp
passed in from Python rather than a key TTL, which is what lets the breaker be driven by a frozen
clock in tests.

## Consequences

- Adding a courier is one adapter file plus one `register()` call; the orchestrator, router and
  schemas never learn its name (AC-3.4).
- Mock mode covers all three providers even though only Pathao has a live adapter, so the demo
  shows the full fan-out with no credentials configured (FR-11.1).
- `MOCK_MODE` bypasses the credential requirement for a leg. That is deliberate — a reviewer who
  has connected nothing must still get results — and it means mock mode can never be enabled
  anywhere real. The `/health` endpoint reports `"mode": "mock"` and startup logs a warning.
- The check fan-out runs in a task that owns its own database session, so a client disconnecting
  mid-stream cancels the *streaming*, never the work (AC-6.10). That task is held in a module-level
  set; without a strong reference the event loop is free to collect a running check.
