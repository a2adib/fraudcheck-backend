FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

# Put the venv on PATH and run its binaries directly. `uv run` re-syncs on every
# start, which would pull the dev group into the container at boot and stall the
# healthcheck.
ENV PATH="/app/.venv/bin:$PATH"

RUN pip install --no-cache-dir uv

WORKDIR /app

# Dependencies first so a source-only change does not re-resolve the lockfile.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project --no-dev

COPY . .
RUN uv sync --frozen --no-dev

# Never run the API as root.
RUN useradd --create-home --uid 1000 app && chown -R app:app /app
USER app

EXPOSE 8000

CMD ["uvicorn", "src.main:app", "--host", "0.0.0.0", "--port", "8000"]
