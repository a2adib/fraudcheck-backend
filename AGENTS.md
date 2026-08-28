# AGENTS.md

Backend module for **Fraud Checker BD** — a multi-tenant COD risk assessment service for
Bangladeshi e-commerce merchants. Spec: `../../fraud-checker-bd-spec.md`.

The architecture deliberately mirrors the sibling `erp-backend` and `govaly-backend` projects.
If you know those, you know this one.

## Prerequisites
- Python 3.13+
- Docker (tests spin up PostgreSQL 16 + Redis 7 via `docker-compose.test.yml`)
- [`uv`](https://github.com/astral-sh/uv), [`just`](https://github.com/casey/just)

## Setup
```bash
uv sync
cp .env.example .env      # then generate real JWT_SECRET_KEY and ENCRYPTION_KEY
just up                   # Postgres + Redis
just migrate
just run
```

## Key Commands

| Task                    | Command                                   |
|-------------------------|-------------------------------------------|
| Run dev server          | `just run`                                |
| Run all tests           | `just test`                               |
| Run a single test       | `just test tests/auth/test_login.py -k email` |
| Tests with coverage     | `just test-cov`                           |
| Lint + format           | `just lint`                               |
| Type check              | `just typecheck`                          |
| New migration           | `just mm "description"`                   |
| Apply migrations        | `just migrate`                            |
| Seed the demo dataset   | `just seed`                               |

## Architecture

```
src/
  main.py            # FastAPI app, CORS, lifespan, AuthAPIError handler, router registration
  config.py          # pydantic-settings Config (reads .env, or $ENV_FILE)
  constants.py       # Environment enum, DB naming convention
  database.py        # async SQLAlchemy engine + get_session dependency
  logging_config.py  # structlog: console locally, JSON when deployed
  common/            # shared base: mixins, response, exceptions, queries, filters, types, utils
  cache/             # redis clients + cache_result decorator
  auth/              # -> /auth         login, refresh, logout, OTP reset, roles, permissions
  users/             # -> /users        User (a merchant IS a tenant — there is no Company)
  credentials/       # -> /credentials  CourierCredential + AES-256-GCM vault
  logistics/         # providers/, token_manager, breaker — courier integration
  checks/            # -> /checks       orchestrator, SSE streaming, risk scoring
  behavior/          #                  BehaviorSnapshot, AnomalyFlag, the six detectors (FR-16)
  orders/            # -> /orders       OrderContext, RiskAssessment, decision engine (FR-15)
  bulk/              # -> /bulk         BulkJob, CSV intake, progress stream (FR-9)
  apikeys/           # -> /keys         ApiKey + sliding-window rate limit (FR-10)
  ml/                #                  features, registry, scorer, train CLI (FR-17, extra)
  securities/        # -> /securities   ActivityLog (audit)
```

Packages below `checks/` do not exist yet — they land with their milestones. The layout, and why
it diverges from the spec's §3.1 layer tree, is recorded in
[ADR-0001](docs/adr/0001-domain-packages-over-layer-packages.md), which also maps NFR-3's
coverage paths (`services/scoring`, `infra/breaker`, …) onto real files here.

Import direction, to keep the graph acyclic: `common/` and `cache/` depend on nothing; every
domain may import them; `orders/` may import `checks/` and `behavior/`; nothing imports `orders/`.

Each domain package follows `models.py`, `schemas.py`, `services.py`, `routes.py`, with optional
`enums.py`, `const.py`, `utils.py`, `queries.py`. Routers are registered in `src/main.py` with a
prefix + tags.

**Base model class**: `CommonFieldMixin` (`src/common/mixins.py`) provides `id`, `public_id`
(11-char URL-safe token), `created_at`, `updated_at`, `is_active`.
**URLs and payloads reference `public_id`, never the internal `id`.**

**Response envelope**: `StandardResponse[meta, pagination, detail, data]` — build it with
`create_response()` from `src/common/response.py`. Tests read `response.json()["data"]`.

**Auth errors** use `AuthAPIError` and render as `{"error", "message", **extra}` so the frontend
gets machine-readable codes. Everything else uses the `HTTP4xx` helpers from
`src/common/exceptions.py` and FastAPI's `{"detail": ...}` shape.

## Multi-tenancy — the load-bearing constraint

Every merchant queries with **their own** courier credentials and sees **only** what their own
courier account already exposes. Nothing is pooled across tenants.

Practically: every domain table carries `user_id`, every query filters on it, every cache key is
namespaced by `user_public_id`. A cross-tenant read is a bug, not a feature request. There is a
dedicated tenant-isolation test suite; do not weaken it.

## Database

- **ORM**: SQLModel (SQLAlchemy async) with asyncpg
- **Migrations**: Alembic reads `DATABASE_URL` from `settings` at runtime, NOT from `alembic.ini`
  (see `alembic/env.py`). The `alembic.ini` placeholder is ignored.
- **Session dep**: `get_session()` — rolls back on exception, always closes. Services commit
  explicitly.

## Testing

Integration tests against a real database (Docker, no local Postgres needed):

```bash
just test              # start test DB, run pytest -n auto, tear down
just test tests/auth/  # one domain
```

`tests/conftest.py` isolation strategy:
1. Session-scoped: create the test DB if missing, create all tables once.
2. Function-scoped: each test runs inside a savepoint transaction that is rolled back.
3. `get_session` is overridden to inject the test session.
4. Password hashing is downgraded to cheap Argon2 parameters for speed.

Under `pytest-xdist` each worker gets its own database (`fraudcheck_test_gw0`, …).

## Conventions

- New models extend `CommonFieldMixin`
- Soft delete via `is_active = False` (`delete_instance_or_404`); `hard_delete_instance_or_404`
  only for models with no `is_active`
- Reuse `src/common/queries.py` helpers rather than hand-rolling lookups
- Raise `HTTP4xx` from the **service** layer, not the route
- Protect mutating routes with `get_current_user` from `src.auth.utils`
- Side-effecting endpoints call `create_activity_log()` after the business logic succeeds
- Run `ruff check .` before finishing any change

## Things that will bite you

- **Timezone.** All customer-behaviour bucketing is `Asia/Dhaka`, not UTC. Pathao sends
  `updated_at` as Dhaka wall-clock with no offset while `timestamp` on the same payload is UTC;
  parsing naively lands every event 6 hours early.
- **Risk bands are risk-oriented.** `high` means *bad*. The upstream Govaly badge is inverted
  (`HIGH` renders green as a *good* customer) — never map those by name.
- **Never log** credentials, tokens, full phone numbers, order amounts, or addresses. Phones are
  masked `017****678`. There is a test that greps the captured log output.
