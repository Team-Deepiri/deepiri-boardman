FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir "poetry==2.2.1"

ENV POETRY_VIRTUALENVS_CREATE=false \
    POETRY_NO_INTERACTION=1 \
    POETRY_CACHE_DIR=/tmp/poetry_cache

# Dependency layer (cached when lockfile unchanged)
COPY pyproject.toml poetry.lock ./
RUN poetry install --without dev --no-root \
    && rm -rf "$POETRY_CACHE_DIR"

COPY . .
RUN poetry install --without dev \
    && rm -rf "$POETRY_CACHE_DIR"

CMD ["python", "-m", "boardman.main"]
