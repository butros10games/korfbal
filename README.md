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

Preview the current local request workload without credentials, HTTP, or checkpoint
writes by adding `--dry-run` (omit `--session-file`). The JSON reports eligible
feeds in `by_kind`, their total `candidate_feed_requests`, and the budget-capped
`batch_feed_requests_upper_bound`. These are snapshot estimates before successful
response deduplication; OAuth, retries, fallback after failures, and newly discovered
feeds can change the actual workload. Shared quotas and cooldowns can defer it.

A schedule refresh costs one GET per due club program, covering all matches in that
response. Result checks choose between healthy poule and club feeds by how many
still-due matches they can cover, with a healthy poule winning ties. For example, twelve due matches in one
healthy poule plus one due club program need two feed GETs, rather than twelve match
GETs. Directory, team, roster, and standings discovery/audits add their own requests.
A 304 still consumes a request; OAuth renewal and retries also consume the batch cap.
Newer programs can reschedule unscored result-feed placeholders, including postponed
matches; scored and finished records remain protected from scoreless summaries.

The application's existing Celery Beat checks for due work every 30 seconds.
Idle heartbeats do not open the OAuth session or make provider requests. Enable it once with `SPORTLINK_SYNC_ENABLED=true`, set
`SPORTLINK_SYNC_SEASON` to the active source season (for example `2026-2027`), and
set `SPORTLINK_SYNC_SESSION_FILE` to the private OAuth JSON session. Automatic runs
need the worker and Beat services running; they do not depend on page views or CLI
invocations. The CLI remains available for manual imports and previews.

The checked-in production Compose mounts the persistent `sportlink-sessions`
volume at `/run/sportlink` in the worker. Provision `session.json` there with mode
`600`, owned by the worker user (default UID/GID 1000), and make the directory
writable by that user. OAuth rotation atomically replaces the file, so mount a
writable directory rather than an individual read-only secret file. Custom
production Compose installations need the equivalent persistent directory mount.
Never commit the session. The scheduler defaults to disabled until configured and
skips an expired source season; update the scope at season rollover.

Automatic runs drain due shared feeds until the snapshot is complete or their
worker window expires. `SPORTLINK_SYNC_MAX_SECONDS` defaults to 240 seconds (at
most four minutes). Overlapping heartbeats skip while the shared lease is held.
HTTP already in progress and final publication may finish afterward. There is no default
numeric cap on automatic requests and no two-feed maintenance allowance.
`SPORTLINK_SYNC_MAX_REQUESTS=0` disables that optional cap; positive values up to
10,000 impose an operator-selected cap. Remaining work resumes on a later tick.
Recently ended matches across the whole catalogue retain priority; discovery and
older corrections continue whenever due work and time permit.

All import paths share a database lease and serial, durable request pacing.
`SPORTLINK_REQUEST_SPACING` defaults to five seconds and is configurable down to
one second. At five-second spacing, a four-minute window has room for roughly
48 HTTP attempts before network and processing overhead; it may send zero when
nothing is due. This is a throughput estimate, not a batch quota or a guarantee
that the entire catalogue is refreshed every five minutes.

`SPORTLINK_HOURLY_LIMIT=0` and `SPORTLINK_DAILY_LIMIT=0` disable optional operator
quotas by default. Positive values remain enforced across automatic, manual and
historical imports, including OAuth and retries. Existing deployments with explicit
120/1,000 values retain those settings until the operator changes them. Those
numbers are not established provider allowances. Provider HTTP 429 and OAuth
cooldowns still stop the shared importer for `Retry-After`; a rate-limit observation
does not silently impose different permanent hourly/daily quotas.

Automatic sync acquires the provider lease before reading the latest rotated
session and closes its client afterward. Overlapping ticks skip, and queued ticks
expire after 30 seconds. Each batch updates known fixtures after responses; newly discovered
feeds enter the next run. Manual CLI imports still have an explicit `--max-requests`
budget (default 100), and the credentials-free preview remains available.

Task summaries include HTTP counts by feed kind, elapsed milliseconds, distinct
matches checked, actual result observations, newly observed final results, and
result-delay totals/maxima in seconds measured from the estimated finish. Delay is
when this importer saw the score, not a claim about when the provider first
published it. Already-final corrections are excluded from new-final delay metrics.
The backlog reports eligible feeds by kind, overdue pending matches, and the oldest
overdue result check. Each HTTP reservation logs its scheduled timestamp and shared
hour/day counters, allowing daily volume and peak minute demand to be measured
without credentials or match identifiers. Reservations are conservative: a worker
interrupted after reserving capacity may send fewer actual requests. ETags save
response bodies but still consume HTTP attempts.

Club directories, team lists and poule assignments are refreshed weekly; fixture
programs and poule standing audits daily; club result discovery audits weekly.
For unscored fixtures in the next 48 hours (or unresolved in the previous 24 hours),
club programs are eligible hourly, or every 15 minutes within six hours of kickoff.
Per-match schedule freshness survives worker turns and suppresses opponent checks;
every club still receives its daily discovery audit. Failed feeds keep their
retry deadline, and these program checks never advance result-check timestamps.
Within a batch, actual results returned by one feed also satisfy score checks
planned through another feed. This does not suppress a due standings/discovery
audit or count missing rows in a filtered response as observed. First-time team
assignment discovery remains necessary: one poule cannot prove all assignments.
Known matches request shared result checks from their estimated finish. Pending
results are checked every three minutes for three hours, hourly until
48 hours, daily thereafter, and weekly after 30 days. Completed results get daily
correction checks for seven days, weekly until day 28 and monthly thereafter.
These are priorities subject to the global budget and collection audit schedule,
not freshness guarantees. Recent nonempty response membership also guides feed selection: feeds that
omitted due matches receive less coverage credit for up to one day, without
removing those matches from the queue. Unknown and older scopes remain eligible.
Exact response membership is persisted for HTTP 304 reuse;
a successful empty/partial response never certifies an absent match. The migration
clears only collection ETags so existing checkpoints refill their membership once.
One in five selections is reserved for discovery or audits overdue by six hours,
when such work exists; very small optional batch caps may still delay maintenance.

Finish estimates prefer each match’s imported `playing_time_minutes`, adding a 30-minute
heuristic allowance for breaks and reporting. Otherwise, the versioned 2026/27
[KNKV rules](https://www.knkv.nl/kennisbank/wedstrijdinformatie/) distinguish known
40-, 50- and 60-minute formats using mapped classes, colours and playing formats.
Unknown/stopped-clock formats retain the 90-minute elapsed-time fallback; J-team
numbers never imply age. Estimates do not establish that a match has finished,
and extra time/penalties can delay its final score.

Set `SPORTLINK_BACKFILL_REQUEST_SPACING=0` to temporarily remove artificial spacing
while eligible metadata is queued. Requests remain serial and provider cooldowns
still apply. A blank value (the default) disables this override. Once metadata
is completed or deferred, the next batch uses normal `SPORTLINK_REQUEST_SPACING`
again. Provider cooldowns and configured quotas still apply; batch summaries report
`request_spacing_seconds`.

New match discovery queues missing metadata in bulk per response, preserving
existing retry deadlines. It queues three one-time metadata components using the captured
KNKV app requests:

| Component    | GET endpoint                                   | Stored fields                                                                                 |
| ------------ | ---------------------------------------------- | --------------------------------------------------------------------------------------------- |
| Playing time | `match/MatchResultDetails?PublicMatchId=…&v=8` | `Duration`, `EventTimeResolution=MINUTE/NONE`, and `MatchPeriod` descriptions/playing minutes |
| Venue        | `match/MatchFacility?PublicMatchId=…&v=3`      | Facility, address, pitch/surface and dressing-room metadata                                   |
| Rules        | `match/MatchInfo?PublicMatchId=…&v=1`          | The provider's structured match rules                                                         |

Live `NONE` event resolution is accepted only when regulation-period minutes
sum to `Duration`; optional extra time and the untimed `Strafworpserie` penalty
phase do not extend regulation duration or prove those phases were played.

Import runs retain bounded, payload-free failure codes, exception types, stages,
partial counters and an importer code fingerprint. Heartbeats older than ten minutes
are marked `interrupted` only when their matching provider lease is no longer live.
Run statuses distinguish `retrying`, `exhausted` and `deferred` from completed work.
Metadata backfills exit unsuccessfully when exhausted or unable to make progress.

After correcting a failure, preview a narrow retry scope before applying it:

```bash
uv run python manage.py retry_competition_resources --season 2026-2027 \
    --kind match_timing --error-code invalid_response
uv run python manage.py retry_competition_resources --season 2026-2027 \
    --kind match_timing --error-code invalid_response --apply
```

Use repeatable `--resource-id` arguments to narrow the scope further. Retry leaves
successful data and shared traffic budgets intact, refuses an active lease/cooldown,
and clears selected ETags so the fixed parser receives a full response. Legacy
failures may have the code `invalid_response_or_transport`.

When every available owner feed successfully omits an overdue match, the importer
records `results_attempted_at` and `missing_result_attempts` separately from confirmed
coverage. These attempts use the normal age-based retry intervals. Alternate feeds
still get a chance to supply the result, and a later observation clears the missing
streak. The monitoring dashboard flags missing provider results for reconciliation;
HTTP success alone never advances an absent match's confirmed freshness.

Both `v` and `X-Navajo-Version` use the endpoint's version. All calls retain the
session's originating User-Agent and `X-Navajo-Instance: KNKV`. Venue data comes
from `MatchFacility`, not nullable `MatchResultDetails.Location`. Timing-only
requests discard private lineup fields and do not enable roster imports.
When lineup importing is enabled, its existing v8 response also imports timing.
The competition match API exposes playing minutes, periods, venue and rules.
Each component has a separate observed timestamp; completed components do not
need another detail GET during routine score/schedule polling. Recent unambiguous
class observations can inform a temporary timing estimate while a fixture's own
details are still missing, but are never copied into that fixture's imported data.

Backfill existing matches with the same pacing, lease, OAuth renewal and resumable
checkpoints. This only fills missing metadata; it does not overwrite scores or
change native tracker state. Known archive/Dataservice identifiers are excluded
because these endpoints use Sportlink app identities.

```bash
# From apps/django_projects/korfbal: no credentials, HTTP or checkpoint writes.
uv run python manage.py update_competition_match_details --season 2026-2027 --dry-run

# At most 100 HTTP attempts (including OAuth/retries); rerun to resume.
uv run python manage.py update_competition_match_details --season 2026-2027 \
    --session-file /secure/location/sportlink-session.json --max-requests 100
```

For **N matches missing all three components**, the base backfill cost is **3 × N
GETs**. If duration is already imported, that match needs only venue and rules
(two GETs). Fully enriched matches need zero. The command prints exact local
`missing_by_kind` counts before a dry-run and `remaining_detail_requests` after
execution. OAuth and retries add attempts; source permission failures retain their
backoff and exhausted checkpoints are not silently reset. Missing or malformed
metadata is not reported as successfully imported.

After at least 12 tightly bracketed final observations in a mapped class, polling
can learn a reporting allowance from the lower decile, capped at 15 minutes.
Only a final within three minutes of an actual unscored observation is eligible;
backlog-delayed first observations, rescheduled matches, and changes to playing
time or competition class between observations cannot train it.
This is an observed reporting estimate, not a provider publication timestamp.

The dry-run also reports `due_match_feed_estimate`, `due_results`, `due_schedules`,
`schedules_never_checked` and `oldest_schedule_check_age_seconds`. The greedy feed
estimate assumes complete responses and excludes independent discovery/audits,
OAuth and retries; actual HTTP counts remain authoritative. A synthetic regression
checks 12 enriched due matches in 12 pools through four club requests (67% fewer), with no
claim that production has the same overlap.

All discovered feeds are queued once per season. National discovery takes multiple
batches; completion depends on catalogue size, pacing, provider responses and any
operator-selected quotas.
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

Historical imports share the same configurable request spacing and optional
hour/day quotas as live sync. Zero disables an operator quota; a provider HTTP 429
(including OAuth) persists `TrafficState.rate_limited` for diagnostics and applies
its cooldown without imposing hidden numeric ceilings. Historical commands retain
explicit per-batch budgets. Other failures get per-resource backoff and at most six
attempts. Auth/access errors and missing historical resources require an explicit
retry after the cause is resolved.

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

### Activate KNKV baselines in the live ratings API

After deploying and applying migrations, use `publish_allocation_ratings` to select
which imported allocation snapshots should drive a season's existing
`/api/competition/ratings/?season=<season UUID>` endpoint. Deployment alone leaves
existing ratings unchanged. Use the exact season name and source IDs from the
target database; they need not match a local preview database.

```sh
uv run python manage.py migrate
uv run python manage.py publish_allocation_ratings --season '2026-2027' \
    --source 1 --source 2 --source 3 --source 4 \
    --effective-at 2026-09-01T00:00:00+02:00 \
    --b-scale 20 --b-k-factor 1.2 --output /path/to/publication.json
```

Review the dry-run report, then repeat with `--apply` to persist the configuration.
The API immediately selects `knkv-seeded-elo-v1` for that season. Ratings include
baseline, change, original points and competition context alongside the existing
team identifiers, sport, club, sample size and comparison group. Pagination and
club/sport filtering remain supported. Response metadata includes selected sources,
parameters and exclusion counts. Teams without a usable selected allocation are
excluded; their default Elo is not blended into the seeded population.

New results and score corrections trigger chronological replay on the next API
read. Source, classification, team and parameter changes also invalidate the cache;
unchanged provider polling does not. B ratings retain original KNKV point units;
A/top use 1500/400/24 within their own class. All seeded ratings remain provisional
and the scale remains uncalibrated, as described above. The configured effective
instant must represent the start of the supplied baseline, independently of the
file's publication date. There is no frozen cutoff for live ratings.

Repeating the same activation makes no database change. Invalid source selections,
empty usable populations or invalid parameters fail before activation. A failed
command report write rolls back its configuration change. For rollback, preview
and then apply:

```sh
uv run python manage.py publish_allocation_ratings --season '2026-2027' --disable
uv run python manage.py publish_allocation_ratings --season '2026-2027' --disable --apply
```

This restores `elo-v1` while retaining the configuration and original snapshots.

### Match win probabilities from seeded ratings

The single-match summary now includes a `prediction` when its native/provider
team identities and allocated class agree and the season has active baselines.
Starting strength is replayed from the selected baseline through the earlier of
kickoff and now. Only results observed before that instant can contribute. The
latest `ResultRevision` at that time takes precedence over today's corrected
score; without revision history, a current score is usable only if its observation
predates kickoff. The target match and simultaneous/later starts are excluded.
Late-imported historical results are not fabricated as previously known scores.

The existing match graph converts Elo's expected result (win plus half a draw)
into a Poisson scoring split and retains separate win/draw/loss outcomes. It uses
the mapped indoor/outdoor format and falls back to equal team strength when a
compatible prior is unavailable or a different format is selected manually.
A/top priors remain within-class, with no fabricated class gaps. The existing
public-data pace model is retained; player impact attribution is a separate model.

The fixed B-category starting model was evaluated on 553 held-out matches from
287 whole poules, without tuning on their labels: three-way Brier error 0.53595
versus neutral 0.55880, and log loss 0.86826 versus 0.88908. These are kickoff
checks on the supplied autumn 2026 data, not calibration of live event times,
every youth group, A-category matches, or the final seconds. Reproduce the fixed
comparison with `scripts/python/korfbal_seeded_prediction_evaluation.py`; its
input contract and source provenance are in the module docstring. Source data
and production exports are not committed.

### Team standings and schedule alerts

The web team's **Stand** tab reads official poules and standings from the local
catalogue for the selected team and playing season. It shows the last standings
refresh, preserves unknown values, and hides standings when the provider suppresses
results. It does not derive official points from locally tracked goals. The tab
loads only while active: one paginated team/season request embeds up to 100 standings
rows per pool, with subsequent team pages fetched on demand.

After migration, publication keeps a separate schedule baseline. Subsequent changes
to future `SCHEDULED`/`CANCELLED` provider-created fixtures notify active accounts
following either team or club through the existing Web Push/Expo transports. Initial
imports and historical corrections stay silent; local tracking/manual score changes
retain the existing publication protection. Existing fully published snapshots are
baselined during migration, while pending snapshots establish a silent baseline on
the next publication.

Migration 0017 adds a publication event ID claimed with a conditional update before
delivery. Repeated schedule values and duplicate jobs cannot replay a claimed event.
Expo messages are batched in groups of at most 100; browser delivery uses four
concurrent requests with ten-second HTTP timeouts and batches expired-subscription
updates. A failing destination does not prevent attempts to the remaining devices.

Delivery is best effort after the database commit through the existing Celery worker.
A worker crash after claiming an event may lose delivery; claimed events are not retried.
A broker outage is logged without rolling back imported data; this change does not
add a durable notification outbox or replay missed alerts. The importer still needs
its deployment-owned recurring invocation; this PR does not install a new scheduler.

### Security remediation rollout

Apply migrations before starting the updated backend. Release the matching mobile client: old JWTs without a refresh-session identifier require sign-in again, and old unverified admin sessions must complete MFA. Ensure staff have a working email address or passkey before rollout.

Media must use a bucket separate from static files. From the configured Korfbal runtime, `python manage.py check_media_privacy` verifies the MinIO bucket policy without changing it. To remove public allow statements while retaining authenticated grants, an operator can run `python manage.py check_media_privacy --repair --probe`. The probe creates and deletes a uniquely named synthetic object; it does not read user files. Test the signed download endpoint and verify the old media hostname no longer serves raw objects after deploying the proxy configuration. Do not treat a successful local probe as production verification.

Spotify track imports now require both `SPOTIFY_CLIENT_ID` and `SPOTIFY_CLIENT_SECRET`. Metadata comes from Spotify's official API; the Celery worker uses yt-dlp, its packaged JavaScript solver, Node, and ffmpeg to find and convert one audio result. Search matching can differ from spotDL. Direct MP3 uploads remain available without Spotify credentials. The existing `SPOTDL_DOWNLOAD_TIMEOUT_SECONDS` setting continues to bound each download attempt for deployment compatibility. The worker accepts only a successful, nonempty MP3 within the upload size limit and kills decoder descendants on timeout.
