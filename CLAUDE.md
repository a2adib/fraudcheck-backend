# CLAUDE.md

This file guides Claude Code in this repo. The full project guide lives in
`AGENTS.md` and is imported below — keep `AGENTS.md` as the single source of truth.

@AGENTS.md

## Claude notes
- Gate: run `ruff check .` before finishing any change (lint must pass).
- Tests need Docker; use `just test` (spins up Postgres 16 + Redis 7, tears down).
- `../../govaly/` is **read-only reference**. Read `erp-backend` and `govaly-backend` freely for
  patterns; never write anything into them.
- The spec at `../../fraud-checker-bd-spec.md` is authoritative. Each requirement carries
  Given/When/Then acceptance criteria — implement against those, and reference the AC id in the
  test name (e.g. `test_ac_1_5_wrong_password_is_constant_time`).
