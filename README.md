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
local names. A custom import runner that bypasses `sync()` should call
`publish_catalogue(lease_owner=owner, overrides=reviewed_links)` before releasing
its batch lease. Publication applies reviewed aliases and automatic matches in
one reconciliation pass; an additional `reconcile(apply=True)` is unnecessary.
The returned `links` counts describe those reconciliation decisions. The publisher
uses the same lease to avoid reading another worker's unfinished batch.

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
durable provider budget: by default at most 120 per hour, 1,000 per day, and five seconds
between requests. `--max-requests` further bounds each run. Exhaustion defers work
without marking a feed failed. The summary's `requests` counts selected resources;
`http_requests` counts actual reserved wire attempts. ETags avoid unchanged bodies.
Provider 401/403 and 429 stop the batch globally; numeric and HTTP-date Retry-After
values are respected, including rate limits on token renewal.

Club directories, team lists and poule assignments are refreshed weekly; fixture
programs and poule standing audits daily; club result discovery audits weekly.
Within a batch, actual results returned by one feed also satisfy score checks
planned through another feed. This does not suppress a due standings/discovery
audit or count missing rows in a filtered response as observed. First-time team
assignment discovery remains necessary: one poule cannot prove all assignments.
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

### Historical competition imports

Historical discovery uses the same source models, global teams, season rosters,
Django admin and native publication flow. It has its own durable work queue because
an old match must **not** enqueue today's club/team feeds under an old season.
Ratings/Elo are not part of this importer.

Create the appropriate `schedule.Season` and its real date boundaries first. Seed
only identifiers obtained from a provider response, a saved link, or an attributed
archive. App IDs (`M…`, `T…`) and numeric Dataservice codes are different namespaces;
the importer never guesses a conversion or scans numeric ID ranges.

```bash
# A known app match reveals its poule; a known poule can also be seeded directly.
uv run python apps/django_projects/korfbal/manage.py import_competition_history seed \
  --season '2025-2026' --provider app --kind match --source-id M123456 \
  --reference 'saved-match-link'

uv run python apps/django_projects/korfbal/manage.py import_competition_history run \
  --session-file /private/competition/session.json --max-requests 20

uv run python apps/django_projects/korfbal/manage.py import_competition_history status
# Recheck a specific checkpoint after correcting access, evidence or a mapping.
uv run python apps/django_projects/korfbal/manage.py import_competition_history retry \
  --resource 123
```

The IDs above are examples. No working previous-season app discovery filter has
been verified. App date-window seeds are rejected; empty/current-season responses
are not evidence that a historical competition was fully imported. Direct match
responses must match the supplied identity, season and interval. A poule must have
dated results inside the selected season before its standings are imported.

For optional Club.Dataservice access, put **that club's** client ID in a separate
mode-600 file. App OAuth is never sent to Dataservice. The `source-id` for a club
window is its Sportlink club relation code, and the results are checked against
that scope. Use an explicitly verified sport ID; absent club identities or sport
mappings are held for review instead of guessing them.

```bash
uv run python apps/django_projects/korfbal/manage.py import_competition_history seed \
  --season '2025-2026' --provider dataservice --kind window --source-id CLUB_CODE \
  --start 2026-05-01 --end 2026-05-31 --sport KORFBALL-VE-WK
uv run python apps/django_projects/korfbal/manage.py import_competition_history run \
  --dataservice-file /private/competition/dataservice-id --max-requests 20
```

Dataservice club `uitslagen` documents a 52-week lookback. Older club windows are
recorded as inaccessible without spending requests. Known poules use the separate
`pouleuitslagen` endpoint, whose documented week offset has no such stated limit;
actual historical retention and subscription access still have to be established
from its responses. The importer follows returned match codes to `poulecode`, then
prioritizes bulk poule results before remaining match details. Previously imported
match/poule identities are reused. Dataservice source IDs use the `ds:` prefix to
avoid collisions with app IDs; ambiguous native links remain publication conflicts.

Seed the next older interval/season using its existing season record. Runs process
newer seasons first, with bulk poule results ahead of individual match requests
within each season. Windows longer than 240 days are split into disjoint inclusive
intervals. Club results split at the documented 500-row cap; poule results use a
conservative 1,000-row split threshold and independent completeness checks because
that endpoint has no documented row-limit parameter. See Sportlink's
[parameter limits](https://sportlinkservices.freshdesk.com/nl/support/solutions/articles/9000211117-lijst-met-parameters-in-club-dataservice)
and [endpoint contracts](https://sportlinkservices.freshdesk.com/nl/support/solutions/articles/9000062942).
A saturated single day remains blocked/partial; it is never silently counted as complete.
Relative week requests include a small alignment margin, which is discarded during
normalization. Dates outside the actual wire interval block the resource as an
ignored-filter response. HTTP success, interval exhaustion and complete poule
coverage are separate facts.

`HistoricalResource` in Django admin shows state, coverage, date bounds, attempts,
errors and evidence. `HistoricalDiscovery` retains source references and parent
edges; repeated discovery widens the retained date scope without repeating completed
requests. Exact duplicate result rows are applied once; conflicting copies of the
same source identity are rejected atomically. Coverage is `unknown`,
`partial`, `empty`, `inaccessible` or `complete`. Complete poules require unfiltered
results with final scores and per-team played counts matching their official
standings, with unique membership identities and uninterrupted exhausted date
coverage for Dataservice poules. Missing scores, withdrawals, incomplete membership,
date gaps or count differences remain partial. Completed resources are cached
indefinitely; use an explicit retry
for a targeted correction check. ETags are retained for such rechecks.

For archives without provider access, normalize **actual recorded matches** into
JSON using the existing app-shaped fields (`PublicMatchId`, `MatchDateTime` with an
offset, `Status`, `HomeTeam`, `AwayTeam`, optional `Pool`, `HomeResult.Score` and
`AwayResult.Score`). Each team supplies `PublicTeamId`, `TeamName`, `SportId`, and a
`Club` with a known `ClubId`. Wrap the rows as:

```json
{ "namespace": "clubbook", "source": "https://example.org/archive.pdf", "matches": [] }
```

Run `import_competition_history archive --season '2025-2026' --file archive.json`,
then `publish_competition`. Archive IDs are namespaced, raw documents/player payloads
are not stored, missing scores remain missing, and provenance remains accessible.
Archive publication labels scores `archive` and cannot overwrite an existing native
match's scores. Final standings alone cannot reconstruct individual matches: there
is deliberately no automatic score fabrication or general PDF scraper.

Invoke `run` periodically through the existing deployment scheduler (for example,
every 15 minutes). Credential paths may instead come from
`SPORTLINK_HISTORY_SESSION_FILE` / `SPORTLINK_HISTORY_DATASERVICE_FILE`. Each run is
bounded, yields while current-season discovery/refresh work is due, and claims the
same provider lease **before loading the latest rotated session**. All HTTP attempts,
including refresh/retry calls, count toward the shared traffic budget. `--no-publish`
retains source records for inspection before native publication.

App matches with missing scores remain eligible for detail enrichment. Dataservice
details are used to establish the poule link; numeric detail scores alone do not
prove a match was played. Recorded result scores retain their regulation/extra-time
value when a separate shootout score is present in parentheses.

Default limits remain 120/hour, 1,000/day and five-second spacing. Configurable
`SPORTLINK_HOURLY_LIMIT`, `SPORTLINK_DAILY_LIMIT` and `SPORTLINK_REQUEST_SPACING` are
capped at 3,600/hour, 86,400/day and at least one second. An actual HTTP 429 (including
OAuth) persists `TrafficState.rate_limited`, returning both import paths to at most
the conservative defaults. Other failures get per-resource backoff and at most six
attempts; they do not switch the global rate policy. Auth/access errors and missing
historical resources require an explicit retry after the cause is resolved.

These workflows are tested with synthetic historical responses. An end-to-end live
previous-season fetch remains unverified until an accessible historical seed or
Dataservice subscription is supplied; the importer reports that limitation rather
than promising all previous seasons are available.

Rollout: apply migrations before starting these workers. Custom deployment runners
must use `apps.competition.services.traffic.TrafficGate` for the same persistent
fallback policy; an external/custom rate-gate module does not automatically adopt
these settings. Keep historical scheduling disabled until that cutover is complete.
The PR does not install a production schedule or copy credentials.

### Official competition classification and KNKV allocation files

Competition editions belong to an annual season and separately record zaal/veld,
phase and mixed/dames. Classes retain category, standard/reserve/youth context,
colour and playing format. Poule codes are identifiers, not strength ranks.
J-numbers express average-age order within a club; they do not encode a fixed age.
Historical A/B/C youth names remain season-specific.

The two-column KNKV autumn CSV exports can be staged without creating duplicate
clubs or teams. A-category exports contain class headings; B-category exports also
contain aggregate ages and original KNKV points. Points are retained as source data;
this import does not calculate ratings. UTF-8 and Windows-1252, decimal commas,
both independent columns, zero values and empty values are supported. The explicit
`Midweek zaal` heading retains its indoor context within an otherwise outdoor file.

Run against an isolated local database first (normal `manage.py` settings may point
to another database). Select the actual file scope explicitly; CSV exports lose
worksheet names:

```sh
uv run python manage.py import_knkv_allocations --season 2026-2027 \
    --edition outdoor-autumn --file /path/to/allocations.csv \
    --label 'KNKV veld najaar 2026 B-cat gemengd' --gender mixed \
    --output /path/to/review.json
```

Add `--apply` after reviewing the report. Use `--gender women` for dames files.
Imports retain file digests, publication dates and row/column provenance. Unique
season, poule, team-name and discipline matches link provider memberships; town
resolves remaining duplicates. Verified code punctuation/zero-padding and the
M/MW midweek aliases are normalized without fuzzy team-name matching;
unmatched or ambiguous allocations remain staged and can be inspected through
`/api/competition/allocations/`. Rerun after catalogue discovery to resolve newly
available identities. No fuzzy matching or upstream requests occur.

Audit/backfill already imported poules with:

```sh
uv run python manage.py map_competition --season 2026-2027 --output /path/to/review.json
```

Add `--apply` to persist decisions, and repeat `--pool <source-id>` to narrow scope.
Optional `--overrides /path/to/reviewed.json` accepts a map of source pool IDs to
`{"values": {"gender": "mixed", "phase": "autumn", "age_group": "senior",
"team_kind": "standard"}, "reason": "Reviewed official worksheet"}`.
Overrides retain source evidence and survive imports; changed evidence marks the
mapping conflicted until reviewed again. Missing context remains explicit. Coverage
reports describe discovered local records, not completeness of the national feed.

The existing competition APIs expose classifications and support season, entity,
class/category, phase, discipline, gender and age-group filters with pagination.
Native season-pool responses expose the same linked classification. Annual-season
catalogue responses include the separate competition editions. No additional web
screens are required.

### Local KNKV baseline rating experiments

After mapping, `preview_allocation_ratings` exports a read-only report. It does not
change the existing ratings API, club records, source points or match results.
Choose source IDs explicitly from `AllocationSource` in the isolated database;
multiple snapshots for the same team/class are rejected instead of silently reseeding.

```sh
uv run python manage.py preview_allocation_ratings --season 2026-2027 \
    --source 1 --source 2 --source 3 --source 4 \
    --effective-at 2026-09-01T00:00:00+02:00 \
    --through 2026-09-08T00:00:00+02:00 \
    --b-scale 20 --b-k-factor 1.2 --output /path/to/rating-preview.json
```

Source IDs in this example are placeholders for the selected local snapshots.
The effective date is an explicit assertion that their points represent the start
of competition. The 3 September publication date does not prove which results the
baseline already contains. The supplied files were described as starting scores;
the example includes early 2 September fixtures on that assumption. If that is
incorrect, select an earlier baseline or a later effective date before publication.

B-category teams start at their exact KNKV points. Missing scores remain excluded;
zero is valid. The report retains the original points, aggregate age, baseline,
updated score, change, games, source digests, window and result fingerprint. It
replays only completed, observed, non-awarded fixtures from the allocated poule.
Corrections rebuild from the original baseline. Edition, gender, age/colour,
playing format and class remain separate; connected schedules define comparison
scope. Unknown or conflicting mappings remain excluded and are reported.

The example parameters are experimental, not KNKV rules or a calibrated prediction
model. A 20-point difference gives a 10:1 expected-result ratio, and an update is
bounded by 1.2 points per match (the existing 400/24 Elo parameters divided by 20).
Validate that scale and update rate on separate historical competitions before
using predictions or publishing rankings. No goal-margin or home bonus is added.
KNKV's own end-of-competition formula differs from this Elo experiment; see the
[official strength-indication explanation](https://www.knkv.nl/kennisbank/sterkte-indicatie/).
Their scores describe relative strength in one competition, not an absolute scale
that can be carried unchanged across indoor/outdoor editions or seasons.

A/top-category teams use a provisional 1500 start within their official class,
with the existing 400 scale and 24 update factor. The report also carries the
ordinal class level. Calibrating Elo gaps between classes requires historical
promotion/relegation and results connecting those classes; one opening round
cannot establish those gaps. Club-level comparisons should retain separate
senior/youth, standard/reserve and mixed/dames contexts rather than averaging
these incompatible scores into one number. No new screens are introduced.
