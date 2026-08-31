# Fraud Checker BD — Backend

Multi-tenant COD risk assessment for Bangladeshi e-commerce merchants.

A merchant supplies their own courier portal credentials. The service fans out concurrent lookups
across Pathao, Steadfast, and RedX, streams each provider's result back as it resolves, combines it
with the customer's purchase-behaviour pattern, and returns an explainable risk decision — before
the order is confirmed.

## Quickstart (no credentials needed)

```bash
uv sync
cp .env.example .env
just up          # Postgres 16 + Redis 7
just migrate
just seed        # demo merchant + history
just run
```

Open http://localhost:8000/docs.

`MOCK_MODE=true` (the default in `.env.example`) swaps every real courier adapter for a
deterministic mock, so the whole service runs end to end with zero courier credentials.

## Reserved test numbers

| Number | Behaviour |
|---|---|
| `01700000001` | Clean history, low risk |
| `01700000002` | High return rate, high risk |
| `01700000003` | No records on any provider |
| `01700000004` | One provider times out |
| `01700000005` | All providers fail |
| `01700000006` | Providers disagree — triggers `uncertain` |
| `01700000007` | Order velocity anomaly |
| `01700000008` | Order amount spike |
| `01700000009` | Delivery-location shift |
| `01700000010` | First order, high value, COD |

## Checking a number

First, store the merchant's own courier credential — encrypted with AES-256-GCM, decrypted only at
call time. (In mock mode this is optional.)

```bash
curl -X POST localhost:8000/credentials -H "Authorization: Bearer $TOKEN" \
     -d '{"provider":"pathao","username":"...","password":"..."}'
```

Then check a customer. There are two ways, and they run the same code underneath.

**One call, one answer** — for a dashboard, a table row, or a checkout hook:

```bash
curl -X POST localhost:8000/checks/lookup -H "Authorization: Bearer $TOKEN" \
     -d '{"phone":"01712345678"}'
```

Returns the score, the band, the per-factor breakdown, every provider's numbers, and whether the
answer came from cache. It waits for the slowest courier, bounded by each adapter's timeout.

**Streamed** — for a UI that fills in courier cards as they resolve:

```bash
curl -X POST localhost:8000/checks -H "Authorization: Bearer $TOKEN" \
     -d '{"phone":"01712345678"}'                       # 202 + check_id + stream_url
curl -N localhost:8000/checks/<check_id>/stream -H "Authorization: Bearer $TOKEN"
```

The stream emits `started` → `provider_result` (one per courier, **in completion order**) →
`score` → `done`. A courier that times out, fails to authenticate, or sits behind an open circuit
emits its own `provider_result` with a status and an error code; it never delays or fails the
others.

Either way the result lands in the merchant's history, readable at `GET /checks/{check_id}`. The
phone number always travels in the request body — never a path or query string, where it would end
up in access logs and browser history.

### The score

```
delivered    = Σ delivered across providers that answered
returned     = Σ returned + cancelled
total        = delivered + returned          # 0 → score 50, insufficient_data

return_ratio = returned / total
base_risk    = return_ratio * 100
confidence   = min(1.0, total / 10)
score        = base_risk * confidence + 50 * (1 - confidence)
```

Bands: `low` 0–25, `medium` 26–60, `high` 61–100 — **risk-oriented, so `high` means bad.**

The `confidence` term is the part worth reading twice. A customer with two orders and one return
has a 50% return rate, and reporting that as 50 risk points would be arithmetic impersonating
evidence. Thin histories are pulled toward a neutral 50 instead, so they read as *"we don't know"*
rather than *"suspicious"*. Full reasoning in
[ADR-0002](docs/adr/0002-courier-fan-out-scoring-and-resilience.md).

Migrating from Govaly's `customer_purchase_behavior` check? See
[docs/migrating-from-govaly.md](docs/migrating-from-govaly.md) — note that the band vocabularies are
inverted.

## Design notes

**Tenant-scoped by construction.** Each merchant sees only what their own courier account already
exposes. Nothing is pooled or resold across tenants — which rules out the simple design (one shared
scraped table) and forces the interesting one (per-tenant credential vault + per-tenant cache
namespacing).

**Deterministic scoring.** The score that drives a merchant-visible decision is explainable
arithmetic with a per-factor breakdown, not a model output. A merchant declining a real customer's
order can be told exactly why.

**Asymmetric feedback.** Merchants see the score, band, reason codes, and every provider result.
Customers see a neutral outcome only — telling a customer they were flagged hands an attacker a
retry oracle.

See `../../fraud-checker-bd-spec.md` for the full specification and `docs/adr/` for the decision
records.

## Development

See [AGENTS.md](AGENTS.md).

## License

MIT
