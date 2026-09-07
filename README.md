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

The importer publishes KNKV data into the normal application models: `club.Club`,
global `team.Team`, one `team.TeamData` per team/season, `schedule.SeasonPool` and
`schedule.Match`. Imported records use the existing admin, search and detail pages.
Indoor and outdoor provider teams share a global team and season roster while
retaining separate Sportlink IDs and sport-specific poules.

The `competition` tables retain provider identities, official standings, source
snapshots, request checkpoints and result revisions. They are sync metadata, not a
second user-facing club/team directory. No player profiles or login payloads are
stored in these tables. Exact case/whitespace normalization preserves age groups
and team numbers; uncertain identities remain in the publication report.

Normal `sync_competition` batches publish their saved changes automatically. To
publish an already downloaded catalogue after deployment, run:

```bash
uv run python apps/django_projects/korfbal/manage.py publish_competition
```

This command makes no provider requests and is safe to repeat. Run existing-record
reconciliation with reviewed aliases first when clubs such as DTS have abbreviated
local names. A custom import runner that bypasses `sync()` must call
`publish_catalogue()` after releasing its batch lease. The publisher uses the same
lease to avoid reading another worker's unfinished batch.

Existing clubs, rosters, permissions, attendance and tracked scores are retained.
New or untouched scheduled matches can receive official scores in `MatchData` with
`score_source="knkv"`; no shots or player events are invented. Tracker activity and
manual score changes stop provider updates to that match's local score. Source
score corrections remain available in the provider revision history.

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

### Link existing clubs, teams, poules and matches

The competition catalogue can link to existing application records without
creating replacement clubs, moving players or changing recorded scores,
attendance, permissions or tracker events. The additive linking migration leaves
all links empty until reconciliation is run. It scans every imported season and
makes no provider requests.

Preview the proposed links and missing counterparts from the repository root:

```bash
uv run python apps/django_projects/korfbal/manage.py reconcile_competition > reconciliation.json
```

The JSON report contains source IDs, external IDs, seasons, club cities, candidate
local UUIDs, decisions and `unlinked_local` records. Unique normalized club names
produce candidates; team names match within linked clubs (allowing a club prefix
on the source label). A joint team can also match a local partnership club when
the full partner names and team designation match exactly and KNKV registers the
team under one of those partners. Partner order may differ; age groups, town
qualifiers and team numbers remain significant. The clubs stay separate and only
the team/match identities link. Poules need the same season, name and fully linked member
set. Matches require linked home/away teams in the same order, the same season
and exact kickoff. Either-side ambiguity prevents automatic linking. Already
saved links are retained; inconsistent parent links stop the operation for review.

For aliases such as local DTS and source DTS Enkhuizen, verify the source city
and KNKV external ID, then provide a reviewed JSON array in `links.json`:

```json
[
    {
        "kind": "club",
        "source_id": 123,
        "local_id": "00000000-0000-0000-0000-000000000001"
    }
]
```

Those IDs are placeholders. `source_id` is the catalogue database ID from the
report, not the external KNKV ID. Kinds are `club`, `team` (shared team group),
`pool` and `match`. Explicit selections must still respect existing club/team and
season relationships; they can resolve name differences, duplicate candidates or
rescheduled kickoff times. They cannot replace an existing link or assign the
same local identity twice within its scope.

```bash
uv run python apps/django_projects/korfbal/manage.py reconcile_competition --links-file links.json
uv run python apps/django_projects/korfbal/manage.py reconcile_competition --links-file links.json --apply
```

Review the preview first. `--apply` recalculates decisions under database locks and
saves mutually unique plus explicit mappings atomically. It refuses to run during
an active import. Repeat after later import batches to link newly discovered
counterparts; imports preserve existing links. Local historical games without an
imported counterpart stay in `unlinked_local`; unavailable historical seasons
cannot be inferred from current-season KNKV feeds.

Catalogue APIs expose `local_club`, `local_team`, `local_pool` and `local_match`
on their corresponding records. Query `matches/?local_club=<uuid>`,
`matches/?local_team=<uuid>` or `matches/?local_match=<uuid>` using existing app
identifiers; team groups also support `local_club` and `local_team` filters. These
links are backend support; existing roster/tracker screens retain their current
behavior.

Club logo references are retained from the KNKV club list and fixture responses.
The `club_logo` queue downloads each new hash through the shared request gate,
validates and caches a PNG in normal club image storage, and publishes it to the
existing club logo field. Existing manual uploads are preserved. Requests to the
public binary host never receive the saved OAuth credentials.

The logo migration requeues only existing `clubs` feeds (clearing their ETags),
so already-imported clubs gain logo references without restarting the full crawl.
Deploy the new importer image as well as the web image before resuming that queue;
the custom production runner must use the updated image to recognize `club_logo`.
Changed logo hashes are queued again; unchanged cached hashes require no image HTTP
request. Image failures use the normal retry checkpoints and provider backoff.

Publication repairs reversed joint-club source names when the partner names,
team designation, source club and season match exactly. It preserves the linked
team/roster and moves source variants from unlinked duplicate groups; it never
merges two independently linked native teams. Unnamed poules use `KNKV-poule <id>`
labels, and different IDs with identical labels receive an ID suffix. This keeps
incomplete provider metadata from collapsing distinct poules. Run
`publish_competition` after deployment to repair existing publication conflicts
without additional provider requests.

Feeds stop being selected automatically after six consecutive failed attempts
(one initial attempt and five retries). Their `SyncResource` error and failure
count remain available for review; sync reports their count as `exhausted`. A
successful attempt before exhaustion clears the failure streak. After fixing an
exhausted feed, an operator can explicitly reset its failure count and retry
deadline. Restarting an importer does not reset the cap.
