# Migrating from Govaly's `customer_purchase_behavior`

`govaly-backend` already runs a fraud check. This project is its successor, not a greenfield
build, and this document is the map between the two: what carried over, what changed, and what a
migrating tenant should expect.

## What Govaly does today

`upsert_customer_purchase_behavior()` (`src/logistics/services.py:942`) fans out to Pathao and RedX
concurrently for a phone number and caches `{total_delivery, successful_delivery, customer_review}`
per courier into a `CustomerPurchaseBehavior` row. It refreshes only when the row is older than six
months or a courier field is null (`should_update()`). `GET /customers/{public_id}/behavior` serves
it, and an operator reads the two panels and decides by eye.

There is no score, no threshold, and no decision. `Customer.behavior` — the `HIGH` / `MEDIUM` /
`ALERT` / `REGULAR` badge — is set by hand; no code path writes it.

## What carried over

| Govaly | Here | Notes |
|---|---|---|
| Pathao `POST /api/v1/user/success` lookup | `src/logistics/providers/pathao.py` | Same contract; now per-merchant credentials |
| RedX `customer-success-return-rate` lookup | *not yet ported* | FR-3.6 requires an async rewrite |
| `PathaoTokenManager` | `src/logistics/token_manager.py` | Per tenant, Redis-only — see ADR-0002 |
| `asyncio.gather` fan-out | `src/checks/services.py` | Now streams each leg as it resolves |
| `cache_result` decorator | `src/cache/cache_decorator.py` | Ported in M0 |
| Six-month staleness rule | `CHECK_CACHE_TTL_SECONDS` (15 min) | See "Why the TTL shrank" |

## What is new

- **A computed score with a stated formula and a per-factor breakdown** (FR-7, ADR-0002). The
  operator's eyeball judgement becomes a number anyone can recompute.
- **Per-tenant credentials.** Govaly queries with one platform account. Here each merchant supplies
  their own, sees only what their own courier account already exposes, and nothing is pooled.
- **Resilience the operator never had**: a circuit breaker per provider, a distributed login lock,
  and a check that completes even when every provider is down.
- **Streaming.** Results arrive card by card instead of after the slowest courier.

## Why the TTL shrank from six months to fifteen minutes

Govaly's six-month window is a *storage* decision — the row is the record. Here the cache is only
an optimisation in front of a live lookup, and the stored `CheckRequest` is the record. A merchant
re-checking a number at checkout wants today's answer, not a number from a previous quarter, so the
cache exists to stop a burst of identical checks hammering the portal (FR-6.8) and nothing more.

## The band vocabulary is inverted — map it explicitly

**Do not port Govaly's `behavior` enum by name.** Its badge is quality-oriented and rendered from
the customer's point of view: `HIGH` is a *good* customer, and it renders green. This project's
bands are risk-oriented, where `high` means bad.

| Govaly `Customer.behavior` | Risk band here |
|---|---|
| `HIGH` | `low` |
| `REGULAR` | `low` |
| `MEDIUM` | `medium` |
| `ALERT` | `high` |

FR-15.12 requires this mapping to go through an explicit table, and AC-15.11 asserts that `HIGH`
does **not** map to `high`. An imported band is advisory only: it seeds nothing and is overwritten
by the first real assessment.

Whether it is worth importing at all is an open question (spec §8) — the value is hand-set, has no
writer in code, and carries whichever operator's judgement typed it.

## What a migrating tenant gets on day one

- Courier history: immediately, from their own Pathao credential.
- A courier risk score: immediately, with `insufficient_data` set for numbers no courier has seen.
- Internal-history scoring and the anomaly detectors (FR-16): only once orders start flowing
  through `/orders/assess`, or a backfill lands (FR-16.10).
