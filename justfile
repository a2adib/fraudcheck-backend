default:
  just --list

run *args:
  uv run uvicorn src.main:app --reload {{args}}

# ── Migrations ────────────────────────────────────────────────────────────────
mm *args:
  uv run alembic revision --autogenerate -m "{{args}}"

migrate:
  uv run alembic upgrade head

downgrade *args:
  uv run alembic downgrade {{args}}

# ── Lint ──────────────────────────────────────────────────────────────────────
# Checks the whole tree, not just src: alembic/ and tests/ are linted too, and
# CLAUDE.md's gate is `ruff check .`. Keep the two identical.
ruff *args:
  uv run ruff check {{args}} .

lint:
  uv run ruff format .
  just ruff --fix

typecheck:
  uv run mypy src

# Everything CI enforces, in CI's order. Run before pushing.
check:
  uv run ruff check .
  uv run ruff format --check .
  uv run mypy src

pre-commit:
  git add .
  uv run pre-commit run --all-files

# ── Dev services ──────────────────────────────────────────────────────────────
up:
  docker compose up -d --wait

down:
  docker compose down

ps:
  docker compose ps

# ── Demo ──────────────────────────────────────────────────────────────────────
seed:
  uv run python -m scripts.seed_demo

# ── Tests (local — requires uv) ───────────────────────────────────────────────
test *args:
  docker compose -f docker-compose.test.yml down -v
  docker compose -f docker-compose.test.yml up -d --wait postgres_test redis_test
  -uv run --group test pytest -n auto {{args}}
  docker compose -f docker-compose.test.yml down -v

test-cov *args:
  docker compose -f docker-compose.test.yml down -v
  docker compose -f docker-compose.test.yml up -d --wait postgres_test redis_test
  -uv run --group test pytest -n auto --cov=src --cov-report=term-missing {{args}}
  docker compose -f docker-compose.test.yml down -v

# ── Tests (docker) ────────────────────────────────────────────────────────────
test-docker *args:
  docker compose -f docker-compose.test.yml run --build --rm test_runner uv run --group test pytest {{args}}
  docker compose -f docker-compose.test.yml down -v
