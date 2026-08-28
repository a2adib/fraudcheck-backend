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
