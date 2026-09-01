# Plan — port the RedX customer-behaviour lookup

Mirrors the Pathao port (`claude/pathao-customer-behavior-system-93d820`, merged into `dev` as
`fb08a3e`). Same shape: one token manager, one adapter, one registry line, plus config and tests.
Nothing in `checks/`, `credentials/` routes, or the schemas changes — AC-3.4 is the whole point of
the registry.

Baseline: `origin/dev` @ `6811a87`.

## Upstream facts (verified in `govaly-backend`)

| Concern | Value | Source |
|---|---|---|
| Login | `POST {REDX_API_BASE_URL}/v4/auth/login`, body `{"phone", "password"}` | `src/logistics/token_manager.py:357` |
| Login response | `{"isError": bool, "message": str, "data": {"accessToken": "<JWT>"}}` | `RedXTokenManager._parse_login_response` |
| Token expiry | read `exp` off the JWT, unverified; fall back to a default TTL | `_jwt_expiry()` |
| Behaviour lookup | `GET {REDX_PANEL_BASE_URL}/api/redx_se/admin/parcel/customer-success-return-rate?phoneNumber=…`, `Authorization: Bearer <token>` | `src/logistics/services.py:217` |
| Response mapping | `data.totalParcels` → `total_orders`, `data.deliveredParcels` → `delivered`, `data.customerSegment` → `customer_rating` | same; matches spec §FR-3 table |
| Auth failure statuses | `{401, 403}` | `const.REDX_AUTH_FAILURE_STATUS_CODES` |
| Not-found status | `404` | `const.REDX_NOT_FOUND_STATUS_CODE` |

Four things differ from Pathao and drive the work below: **two base URLs, not one**; **login is
`phone`, not `username`**; **failure is signalled in the body (`isError`) at HTTP 200**; **the
lookup is a GET with the phone in the query string**.

## Step 1 — config: split `REDX_BASE_URL` in two

`src/config.py:90` currently has a single `REDX_BASE_URL = "https://redx.com.bd"`. Login lives on a
different host (`api.redx.com.bd`), so it cannot serve both.

- Replace with `REDX_API_BASE_URL = "https://api.redx.com.bd"` and
  `REDX_PANEL_BASE_URL = "https://redx.com.bd"`.
- Extend the existing trailing-slash `field_validator` to cover both (upstream needs it —
  `.env` there carries `https://api.redx.com.bd/`).
- Update `.env.example:54` and `.env.test:32` to the two names (`https://redx-api.test` and
  `https://redx.test` for the test env, so `respx` routes are unambiguous).

No other setting is needed: `PROVIDER_TIMEOUT_SECONDS`, `TOKEN_*` and the breaker settings are
already provider-agnostic.

## Step 2 — `RedxTokenManager` in `src/logistics/token_manager.py`

Subclass `CourierTokenManager` alongside `PathaoTokenManager`. Everything hard (the Redis lock, the
grace window, per-tenant keys, the local-lock stampede collapse) is inherited untouched.

- `provider: ClassVar = ProviderEnum.REDX`.
- `_login_request()` → `(f"{settings.REDX_API_BASE_URL}/v4/auth/login",
  {"phone": self.credential.username.get_secret_value(), "password": …})`.
  The vault stores one `username` column; for RedX that column holds the login phone. Say so in the
  docstring — it is the only place the mapping exists.
- `_parse_login_response()`:
  - `data.get("isError")` truthy → `ProviderAuthError`. Raise with a **fixed** message; do not
    interpolate `data["message"]`, which upstream does — FR-2.5/AC-2.6 forbid a courier's echo of
    the submitted phone reaching a log line.
  - `data["data"]["accessToken"]` missing → `ProviderAuthError`.
  - expiry: `_jwt_expiry(token) or now + REDX_DEFAULT_TOKEN_TTL_SECONDS`.
- Add a module-private `_jwt_expiry(token) -> float | None` ported from upstream: `jwt.decode` with
  `verify_signature=False, verify_exp=False`, returning `exp` when numeric. `pyjwt>=2.12.1` is
  already a dependency (`pyproject.toml:22`). Guard the `token.count(".") != 2` case first so a
  non-JWT never reaches the decoder.
- Add `REDX_DEFAULT_TOKEN_TTL_SECONDS` — put it next to the manager, or in a new
  `src/logistics/const.py` if a second constant appears.
- Register in `_MANAGERS`. That single line is what flips `has_token_manager(REDX)` to true, which
  in turn makes `POST /credentials/redx/verify` stop returning 501 (`credentials/services.py:144`)
  — no change needed in `credentials/`.

**`isError` at HTTP 200 is already handled correctly.** `_do_refresh` calls
`_parse_login_response` *outside* the `try`, so a `ProviderAuthError` raised there propagates
un-rewrapped. Worth an explicit test rather than a comment.

## Step 3 — `src/logistics/providers/redx.py`

Mirror `pathao.py`: subclass `HttpCourierAdapter`, implement `_call` and `normalize`. Timeouts,
latency measurement and `ProviderTimeout` are inherited (AC-3.5).

`_call`:
- `GET {settings.REDX_PANEL_BASE_URL}/api/redx_se/admin/parcel/customer-success-return-rate`
  with `params={"phoneNumber": phone}` and a bearer token from `token_manager_for(credential)`.
- Same one-shot retry as Pathao: on `401/403`, `await manager.invalidate()` and retry once, then
  `ProviderAuthError`. `_MAX_ATTEMPTS = 2`.
- **404 → an empty history**, i.e. return a payload that normalises to
  `DeliveryStats(0, 0, 0, 0, None)`. This is safe here and not the failure the Pathao adapter's
  docstring warns about: `score_from()` (`checks/scoring.py:90`) treats `total == 0` as
  `insufficient_data=True` at the neutral score, so "never seen" does not read as "low risk".
  A *malformed* body still raises `ProviderParseError`.
- Any other `response.is_error` → `ProviderError`; non-JSON or non-dict body →
  `ProviderParseError`.
- **Never log the request URL or `params`.** The phone is in the query string, and the log-grep
  test in the NFR suite will catch it. Log the status code only, as Pathao does.

`normalize`:
- `raw.payload["data"]` must be a dict, else `ProviderParseError`.
- `as_int(data.get("totalParcels"), "totalParcels", self.name)` and the same for
  `deliveredParcels` — reuse the shared helper so `bool` and junk types fail identically
  across providers (AC-3.3).
- `returned = derive_returned(total, delivered)`, `cancelled = 0` (AC-3.6).
- `customer_rating = str(customerSegment)` when present and non-empty, else `None`. Upstream
  defaults it to `""`; an empty string here would render as a rating in the API response.

## Step 4 — registry

One line in `src/logistics/providers/registry.py`: `register(RedxAdapter())`, plus the import.
Nothing downstream. `get_adapters()` in mock mode already covers RedX through `MockAdapter`, so
mock behaviour is unchanged.

## Step 5 — tests

Extend the existing files; no new suite.

`tests/logistics/conftest.py` — add `REDX_API_URL`, `REDX_PANEL_URL`, `REDX_LOGIN_URL`,
`REDX_BEHAVIOR_URL` next to the Pathao ones, and let `credential_for()` keep its `provider`
argument (it already takes one).

`tests/logistics/test_adapters.py`
- `TestRedxNormalisation`, mirroring `TestPathaoNormalisation`:
  - `test_ac_3_2_a_real_shaped_payload_normalises_exactly` — `{"data": {"totalParcels": 10,
    "deliveredParcels": 7, "customerSegment": "Good"}}` → `DeliveryStats(10, 7, 3, 0, "Good")`.
  - `test_ac_3_3_a_malformed_payload_raises_provider_parse_error`, parametrised over: no `data`,
    `data` not a dict, missing `totalParcels`, `deliveredParcels` a string that is not a number,
    a `bool` count.
  - `customerSegment: ""` → `customer_rating is None`.
- `TestRedxFetch`, with `respx`:
  - logs in, then queries, and the phone travels as `phoneNumber`;
  - a `401` invalidates the token and the retry succeeds;
  - a persistent `401` raises `ProviderAuthError`;
  - a `404` yields zeroed stats, not an exception;
  - a `500` raises `ProviderError`;
  - AC-3.5: a slow route raises `ProviderTimeout` inside `timeout + 500ms`.
- `TestRegistry` needs no edit — `AC-3.1` / `AC-3.7` iterate the registry and pick RedX up for
  free. That is the assertion that the port is properly wired.

`tests/logistics/test_token_manager.py`
- `isError: true` at HTTP 200 raises `ProviderAuthError` (the case Pathao has no analogue for).
- `accessToken` absent raises `ProviderAuthError`.
- a JWT `exp` is honoured; a non-JWT token falls back to the default TTL.
- the login body carries `phone`, not `username`.
- one tenant-isolation case: two merchants, two `token:redx:<tenant>` keys (AC-4.5).

`tests/credentials/test_credentials.py`
- verifying a `redx` credential no longer 501s (it currently would, via
  `has_token_manager`). Check whether an existing test asserts the 501 for RedX and update it —
  the natural place to move that assertion is `steadfast`.

## Step 6 — docs

- `docs/migrating-from-govaly.md:23` — flip the RedX row from *not yet ported* to
  `src/logistics/providers/redx.py`.
- `AGENTS.md` — the architecture note says "`logistics/` currently ships the Pathao adapter";
  add RedX and leave Steadfast as outstanding.
- `README.md` if it enumerates live providers.
- No new ADR. This is ADR-0002's design being applied a second time, which is the claim ADR-0002
  makes; a second courier arriving with no new decisions is the evidence for it.

## Open questions to settle against the live portal

1. **The 404 contract.** Upstream has a `REDX_NOT_FOUND_STATUS_CODE` constant but its behaviour
   function swallows every exception and returns `None`, so what RedX actually answers for an
   unknown phone is unconfirmed. If it is a `200` with zeroed counts, Step 3's 404 branch is dead
   code and should be dropped rather than kept on speculation.
2. **Whether the panel token and the API token are the same token.** Upstream logs in at
   `api.redx.com.bd` and presents that bearer to `redx.com.bd`. It works in production there, so
   the plan assumes one token — but that is one merchant account's behaviour, and a per-merchant
   portal may differ.
3. **Header name.** `create_redx_store()` sends `x-access-token: Bearer …` while the behaviour
   lookup sends `Authorization: Bearer …`. The plan follows the behaviour lookup, since that is
   the call being ported.

## Not in scope

Steadfast, and everything in `govaly-backend`'s RedX surface that is order fulfilment rather than
customer history — store creation, assign/edit/cancel, parcel-status webhooks. This service reads
history; it does not ship parcels.
