# ADR-0001 — Domain packages, not layer packages

- **Status:** Accepted
- **Date:** 2026-08-28
- **Requirements touched:** FR-9, FR-10, FR-15, FR-16, FR-17, NFR-3

## Context

The specification's §3.1 sketches a layer-oriented tree: `models/`, `schemas/`, `routers/`,
`services/`, `infra/`, `providers/`, `ml/`. The repository was scaffolded the other way — one
package per domain (`auth/`, `users/`, `credentials/`, `logistics/`, `checks/`, `securities/`),
each holding its own `models.py` / `schemas.py` / `services.py` / `routes.py`. AGENTS.md states
that this deliberately mirrors the sibling `erp-backend` and `govaly-backend` projects, which is
also where most of the ported code comes from.

Two problems followed:

1. The domain layout was written for spec v1.0. It has **no home** for FR-15 (order intake and the
   decision engine), FR-16 (behavior analysis), FR-17 (the ML layer), FR-9 (bulk jobs), or FR-10
   (API keys and rate limiting).
2. NFR-3 demands 100% line coverage on `services/scoring`, `services/behavior`,
   `services/decision`, `infra/breaker`, and `infra/lock` — five paths that do not exist under
   either layout as written, so the requirement currently points at nothing.

## Decision

Keep domain packages, and extend them to cover the v2.0 requirements. A layer split would fight
both the ported upstream code and the existing `src/common/` helpers for no benefit.

New packages:

| Package | Owns | Requirement |
|---|---|---|
| `src/orders/` | `OrderContext`, `RiskAssessment`, `AssessmentOutcome`, the decision engine | FR-15 |
| `src/behavior/` | `BehaviorSnapshot`, `AnomalyFlag`, the six detectors | FR-16 |
| `src/ml/` | `features.py`, `registry.py`, `scorer.py`, `train.py` | FR-17 |
| `src/bulk/` | `BulkJob`, CSV intake, progress stream | FR-9 |
| `src/apikeys/` | `ApiKey`, the sliding-window rate limiter | FR-10 |

`src/ml/train.py` is a CLI module and must never be importable from the request path (FR-17.5,
AC-17.8). Keeping ML in its own package is what makes that assertion testable by walking imports
from `main.py`.

NFR-3's coverage paths bind to this layout as:

| NFR-3 path | This repository |
|---|---|
| `services/scoring` | `src/checks/scoring.py` |
| `services/behavior` | `src/behavior/services.py`, `src/behavior/detectors.py` |
| `services/decision` | `src/orders/decision.py` |
| `infra/breaker` | `src/logistics/breaker.py` |
| `infra/lock` | `src/logistics/token_manager.py` |

`score_return_ratio()` lives in `src/checks/scoring.py` and is imported by `src/behavior/` — one
implementation, per FR-16.2 and AC-16.17. It does not get copied.

## Consequences

- The spec's §3.1 tree is treated as intent, not as a literal file layout. This ADR is the record
  of that divergence; the mapping table above is what a reviewer reads instead.
- Coverage configuration can name real paths, so NFR-3's 100% requirement becomes enforceable
  rather than aspirational.
- Cross-domain imports need a rule to stay acyclic: `common/` and `cache/` depend on nothing;
  every domain may import them; `orders/` may import `checks/` and `behavior/`; nothing imports
  `orders/`. Violations show up as circular imports at startup.
- A future frontend module means restructuring into `apps/api` + `apps/web`. Doing that is a move,
  not a rewrite, and is deferred until the frontend actually starts (M5).
