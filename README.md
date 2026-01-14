# Korfbal (Django API)

Django backend for the Korfbal Web Tool.

- Serves the **REST API** (DRF) consumed by the React SPA (`apps/node_projects/frontend/korfbal-web`).
- Hosts the **Django admin**.
- Runs background jobs via **Celery** (and real-time features via **Channels**).

The public UI is owned by the SPA; this Django project does not host app templates.

## Requirements

- Python 3.12+
- `uv`

## Local setup (minimal)

This is the quickest “I want tests + API running” setup.

1. Create env file:

- Copy `apps/django_projects/korfbal/.env.example` to `apps/django_projects/korfbal/.env`
- At minimum configure: `POSTGRES_*`, `VALKEY_*`, and (optionally) `MINIO_*`

2. Install deps:

- `uv sync`

3. Run migrations:

- `uv run python apps/django_projects/korfbal/manage.py migrate`

4. Create admin user:

- `uv run python apps/django_projects/korfbal/manage.py createsuperuser`

5. Run server:

- `uv run python apps/django_projects/korfbal/manage.py runserver 0.0.0.0:8000`

The API will typically be served behind nginx at `https://api.korfbal.<domain>/api/`.

## Docker dev stack (recommended)

From repo root (uses the shared compose files):

1. One-time:

- `docker network create monorepo_test-net`

2. Start services:

- `docker compose -f docker-compose.base.yaml -f docker-compose.kwt-dev.yaml --profile korfbal up --build`

This brings up Postgres/Valkey/MinIO plus the korfbal services.

## Project quality (Nx)

Prefer Nx targets (faster/more consistent in this monorepo):

- Tests: `corepack pnpm nx run korfbal-django:test`
- Lint: `corepack pnpm nx run korfbal-django:lint`
- Typecheck: `corepack pnpm nx run korfbal-django:typecheck`

Fallback (from `apps/django_projects/korfbal/`):

- `uv run pytest -q`
- `uv run ruff format --check . && uv run ruff check .`

## Operational notes

**External services** (production-like):

- Postgres (required)
- Valkey/Redis (required: cache, sessions, Channels, Celery broker/backend)
- S3-compatible storage (MinIO in dev) for media/static (required in production)

**Integrations** (optional, but used by features):

- Spotify OAuth (`SPOTIFY_*`) for player goal songs
- Web Push (`WEBPUSH_*`) for PWA notifications

**Performance/observability knobs** (all env-driven, off by default):

- Slow SQL logging: `KORFBAL_LOG_SLOW_DB_QUERIES=true`
- Slow requests buffer: `KORFBAL_LOG_SLOW_REQUESTS=true` (see `/api/debug/slow-requests/`, staff-only)

See `apps/django_projects/korfbal/korfbal/settings.py` for the full list of configuration flags.
