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

## Sportlink competition catalogue

The `competition` app imports source clubs, season-specific teams and poules,
fixtures, final scores, official standings and score revision history. These
source identities are separate from locally managed clubs/teams. Indoor and
outdoor teams retain their distinct Sportlink IDs but share a `TeamGroup` when
club, season and normalized full team name match. Matching ignores case and extra
spaces; it preserves age groups and team numbers and does not guess at spelling
variants. Existing imported teams are grouped by migration without provider
requests. Once linked, imports preserve the group link. This catalogue grouping
does not merge locally managed rosters. No player profiles or login payloads are
stored in these tables.

After migrating, create/select a `schedule.Season` with the current season dates.
Run a bounded import from the repository root:

```bash
uv run python apps/django_projects/korfbal/manage.py sync_competition \
    --season 2026-2027 --session-file /secure/location/sportlink-session.json --max-requests 100
```

The session file must have mode `600` and contains `client_id`, `refresh_token`,
`access_token`, `user_agent` from the same app session, optional `secret` (if issued by the client), and optional Unix
`expires_at`. Import only these fields from an authorized login session; no
password is needed or stored. `--token-file` containing a bare access token and
`SPORTLINK_ACCESS_TOKEN` remain available without automatic renewal; both require
`SPORTLINK_USER_AGENT` from the originating app request. Sportlink rejects a
generic HTTP client User-Agent with a server error.

The verified OAuth refresh flow renews access before known expiry or once after
an HTTP 401, and atomically saves rotated tokens to the same private file. The
observed access lifetime is one hour. Invalid/revoked refresh credentials stop the
run with `reauth_required`; sign in again and replace the session file to resume.
Keep credentials outside Git and avoid sharing one refresh session with other
processes/devices because refresh-token rotation can invalidate older copies.

Rerun the command to resume; a database lease prevents overlapping importers.
Each batch plans from a local snapshot; newly discovered feeds enter the next
run. No recurring scheduler is installed by this command: invoke it regularly
(for example every 15 minutes) in the deployment scheduler to enable polling.

All actual HTTP attempts, including OAuth and authentication retries, share a
durable provider budget: at most 120 per hour, 1,000 per day, and five seconds
between requests. `--max-requests` further bounds each run. Exhaustion defers work
without marking a feed failed. The summary's `requests` counts selected resources;
`http_requests` counts actual reserved wire attempts. ETags avoid unchanged bodies.
Provider 401/403 and 429 stop the batch globally; numeric and HTTP-date Retry-After
values are respected, including rate limits on token renewal.

Club directories, team lists and poule assignments are refreshed weekly; fixture
programs and poule standing audits daily; club result discovery audits weekly.
Known matches can request earlier shared result checks: starting 90 minutes after
kickoff, pending results are checked every 15 minutes for three hours, hourly until
48 hours, daily thereafter, and weekly after 30 days. Completed results get daily
correction checks for seven days, weekly until day 28 and monthly thereafter.
These are priorities subject to the global budget and collection audit schedule,
not freshness guarantees. A complete healthy poule feed covers its matches;
filtered or failing poules fall back to club feeds, preferring the club that covers
the most due matches. Successful checks suppress overlapping opponent checks in
the batch. `results_checked_at` distinguishes a scope check from an actual result
observation; absence never fabricates a score or deletes a fixture.

All discovered feeds are queued once per season. National discovery takes multiple
batches and can span multiple days under the conservative provider budget.
The source exposes currently published collections: historical pagination,
withdrawn fixtures, unpublished poules and completeness beyond those collections
are not established. An absent row does not delete existing history.

Authenticated, paginated GET APIs live under `/api/competition/`:
`seasons/`, `clubs/`, `team-groups/`, `teams/`, `pools/`, `pools/{id}/standings/`, `matches/`,
and `sync-resources/`. Filter teams by `season` (UUID), `club` (catalogue ID),
`sport`, `team_group`; poules by `season`, `sport`, `team`; matches by `season`, `pool`, `team`,
`club`, `team_group`, `sport`, `status`, `date_from`, `date_to`. The latter two accept ISO
timestamps. `page_size` is capped at 200. Club/team/poule lists support `search`.

Use `team-groups/?season=<uuid>&club=<id>` to show indoor/outdoor variants as one
application team. Each group includes its source `variants`; use
`matches/?team_group=<id>` for their combined match history, optionally adding
`sport` to show indoor or outdoor only. Different names remain separate groups.

Use `sync-resources/` timestamps/errors to identify incomplete or stale imports;
`pools/` also exposes `results_filtered` and `standings_synced_at`. Official points
and deductions are preserved rather than recalculated from a potentially partial
match list. Fixtures never erase final scores; corrected result observations
create revision rows. Ratings are a separate feature.

### Provisional team-strength ratings

`GET /api/competition/ratings/?season=<season-uuid>` exposes a separate `elo-v1`
read model, with optional `sport`/`club` filters and normal pagination. Each season
starts at 1500; K=24, logistic scale=400, and fewer than ten recorded games is
marked provisional. Ten is a display threshold, not a statistical confidence
claim. These defaults are an initial baseline, not a fitted forecasting model.

Only observed `FINAL` results with both scores and no automatic/awarded result
contribute. Indoor/outdoor sports are isolated. Compare scores only within the
same `comparison_group`: disconnected schedules have no evidence linking their
absolute strengths. Games at the same timestamp use pre-batch ratings. Home
advantage and score-margin multipliers are not assumed.

Scores rebuild chronologically from corrected results; they are cached for up to
five minutes and invalidated when match observations or the team/sport population
change. Team labels are loaded fresh. No extra provider traffic, rating migrations
or permanent rating-update job is required. History is limited to the imported
season data, so incomplete imports yield incomplete ratings.
