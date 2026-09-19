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

## Isolated backend experiments

For a disposable API/PostgreSQL/Valkey stack, use the
[capacity experiment target](#repeatable-capacity-experiments). For interactive
development, use the local setup above. The former `docker-compose.base.yaml`
and `docker-compose.kwt-dev.yaml` files are no longer present in this repository.

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

See [settings](korfbal/settings/) for the full list of configuration flags.

### Repeatable capacity experiments

Run from the repository root after `uv sync --all-packages --dev` and
`corepack pnpm install`. Requires a local Docker daemon; no existing database,
provider credentials, or running Korfbal instance is used.

```bash
# Compare identical workloads on fresh databases, running sequentially.
corepack pnpm nx run korfbal-django:loadtest --viewers=10,50,100 --seconds=30 --shots=100 --reconnect --db-pool-size=0
corepack pnpm nx run korfbal-django:loadtest --viewers=10,50,100 --seconds=30 --shots=100 --reconnect --db-pool-size=8

# Profile command stages separately, without HTTP load or worker consumption.
corepack pnpm nx run korfbal-django:loadtest --profile-commands --shots=100

# Spread spectators and one writer per match across four matches.
corepack pnpm nx run korfbal-django:loadtest --matches=4 --viewers=100 --seconds=60 --shots=100 --db-pool-size=8
```

The runner creates PostgreSQL and Valkey containers with random loopback ports,
starts four Granian workers and a two-process Celery projection worker, applies
real migrations, and seeds synthetic players, lineups, and match commands. Each
data container is limited to two CPUs and 1 GiB RAM; PostgreSQL uses its default
100-connection limit. Only owned containers/processes are removed on exit, including
failure. Email is in-memory, storage is in-memory, and provider import scheduling
is disabled. No production target URL option exists.

Each viewer maintains SSE plus live/summary and one events, shots, or stats tab.
Only invalidated, mounted resources refetch, timeline reads request deltas, and
updates received during a read coalesce. One authenticated writer per match sends
a goal every fifth action and a missed shot otherwise, every three seconds by
default. Reads and commands use normal permissions, CSRF, revisions and idempotency.
`--reconnect` disconnects all spectators halfway through the phase. Statistics jobs
run through the real durable job dispatcher; `--no-background-jobs` is available
for a deliberate comparison, not a production-capacity claim.

`--profile-commands` runs 20 sequential shot/goal service calls on the first seeded
match instead of starting HTTP traffic. `command-profile.json` records SQL calls
and elapsed time, aggregate-lock acquisition/body time, direct command-stage
timers, and diagnostic cProfile function costs. Prefer direct timers for attribution;
async publication callbacks can distort cProfile parent-function totals. Lock-body time excludes commit callbacks; SQL lock statements include their
roundtrip time. Profiling adds overhead, so compare these stage measurements only
with equivalent profile runs and use unprofiled HTTP runs for latency budgets.

Reports and synthetic logs go under `reports/korfbal-loadtest/` at the repository
root (`--output=/absolute/new/directory` overrides it). JSON includes p50/p95/p99
latencies, HTTP outcomes, SSE readiness and delivery, generator scheduling lag,
unfinished requests, stale revisions, sampled database connections/lock waits,
SQL totals and background job progress. No session cookies or response payloads
are written to reports. The default gate requires command p95 <= 1,000 ms and live-read p95 <= 100 ms,
successful samples for both, no request/stream errors, no stale final state and no
missed write intervals; `--max-p95-ms` changes the command budget. Other read
latencies and job backlog are reported separately, so a passing gate is not a
general SLA.

These are local capacity experiments, not production user limits: the API, worker,
and generator share host CPU, and TLS, proxies, WAN latency, uploads and external
providers are excluded. Routes are warmed before measurement; later viewer levels
have warmer caches and longer timelines. Compare fresh runs with identical inputs,
repeat them, and inspect generator lag before attributing a ceiling to the server.

Production Compose bounds web database usage with `KORFBAL_WEB_DB_POOL_MAX_SIZE`
(default 8 connections per Granian process, up to 32 for four processes). Include
all replicas, Celery, administration and monitoring when budgeting PostgreSQL
connections. The pool waits up to five seconds instead of opening unlimited new
connections. Set the Compose variable to `0` to disable pooling. Outside Compose,
set `KORFBAL_DB_POOL_MAX_SIZE` on the web process only; do not put it in a shared
worker environment because durable worker jobs hold session advisory locks.

Public live endpoints now read the latest committed snapshot from the dedicated
`public_live` cache before accessing PostgreSQL. Every recorded match revision
persists a coalescing snapshot job on the existing `projections` queue. After
commit, a revision fence rejects older snapshots. Changes affecting the live score
prepare the public snapshot before notifying spectators, so a notification does
not send every viewer into cache recovery. The durable worker remains responsible
for retrying publication and refreshing other revisions. Anonymous, unfiltered warm
`live/` and `live/poll/` requests need no database queries; other match resources
and authenticated request middleware can still access the database. Cache clients
and Redis connection pools are shared across request threads. Anonymous JSON cache
hits return through the existing security middleware before full view dispatch;
credentials, filters, other formats and cache misses retain the regular API path.

Publication compares revisions atomically in Redis. Deleted tracker data fences
in-flight readers, and reads inside caller-owned transactions bypass shared state.
A missing snapshot or a client ahead of the cached revision triggers authoritative
recovery, with a bounded 100 ms wait to coalesce concurrent rebuilds. Snapshots
expire for reading 30 seconds after their database read began, independently of
worker completion time. This bounds staleness if a commit callback or job is lost;
Redis failures fall back to the database and worker storage failures remain
retryable. Cache failure recovery is a degraded mode, not a promise of unchanged
capacity. Run API and worker releases together; older workers do not register the
new snapshot task. No migration or additional infrastructure is required.

Public match notifications now carry the prepared score/clock snapshot when its
revision exactly matches the event. Clients apply that snapshot directly, cancel
older live HTTP reads, and retain HTTP recovery for missing payloads or skipped
revisions. Larger resources still use coalesced invalidations.

Each ASGI event loop shares one Channels receiver and one subscription per match.
Public frames are serialized once for local viewers; each viewer retains at most
one pending update per subscribed match, combining invalidations when updates are
coalesced. Connection handshakes batch concurrent revision reads, and periodic
shared revision checks recover lost broker messages. Viewer event dispatch does
not perform Django database-connection cleanup.

The harness accepts `--sse-workers=2` to move event connections to a separate ASGI
process pool, independently of `--live-workers`. A deployed equivalent needs a
separately supervised `granian --interface asgi --host 0.0.0.0 --port 1666
--workers 2 korfbal.asgi:application` process and streaming proxy routing for
`/api/live/events/` to that pool. All pools use the same database and Channels
configuration. The web image now supervises separate API, live-read, public-read and SSE pools;
proxy routing must be updated with the image to use that isolation.

The `snapshot_publish_to_receive` metric measures from public snapshot broker
publication to receipt by the synthetic viewer. It uses the shared host wall clock
and excludes the preceding database transaction and snapshot preparation;
`write_start_to_sse` includes the broader tracker action path. The additional
`--max-push-p95-ms` gate defaults to 100 ms and rejects missing delivery samples.
Historical pre-shared-resource [HTTP/SSE measurements](loadtest/results/shared-live-fanout.json) on
2026-09-15 used one synthetic match, sequential 180-second phases, a halfway
reconnect, four API workers, two SSE workers, two WSGI live-read workers with two
threads each, an eight-connection database pool per web worker, and background
projection workers. This precedes the shared-resource pools described below.

| Viewers | Live HTTP p95                    | Published snapshot → receipt p95 | Tracker command p95              | Overall result                                                                    |
| ------- | -------------------------------- | -------------------------------- | -------------------------------- | --------------------------------------------------------------------------------- |
| 100     | 99 ms                            | 42 ms                            | 676 ms                           | Failed: one missed write slot; no HTTP/SSE errors or stale final views            |
| 1,000   | 1,495 ms                         | 391 ms                           | 5,186 ms                         | Failed: 9,655 HTTP errors, 14 SSE errors, 32 missed write slots, one stale viewer |
| 10,000  | Not a valid capacity measurement | Not a valid capacity measurement | Not a valid capacity measurement | Overload: 8,976 stale viewers and substantial generator stalls                    |

At 10,000 viewers the generator's event-loop lag reached 897 ms p95 and 26 seconds
maximum. Granian 2.8.2's default backlog is 1,024; its default per-worker
backpressure is backlog divided by workers. This SSE configuration therefore
admits roughly 1,024 concurrent streams, and recorded only 2,048 ready connections
across the initial/reconnect waves. A useful 10,000-viewer capacity experiment needs
explicit stream admission sizing and load generation isolated from the application.
The raw overloaded results are retained, including errors; their successful-request
percentiles must not be interpreted as service-wide latency.

At 1,000 viewers summary, timeline and stats reads still took 6.6–6.9 seconds p95,
and the API logged database-pool timeouts. Publishing score/clock snapshots removes
one refetch path but leaves these other reads competing with tracker writes.
Those results motivated the shared public reads, protected write capacity and
explicit admission limits described below. The 100 ms goal is
not met for the complete tracker-action-to-viewer path.

### Shared spectator reads and protected writes

The next iteration shares public summaries, statistics, event timelines and shot
lists in the same dedicated Redis cache. One non-locking consistent database read
builds the resource envelopes before their revision notification. The existing
durable publication task also refreshes these envelopes. Publication and deletion
use revision fences, and freshness remains bounded to 30 seconds from read start.
Non-match metadata changes that do not record a live revision can therefore take
up to that freshness bound to appear in cached summaries.

Timeline envelopes retain bounded revision metadata, so equivalent viewers receive
upserts, deletions and ordering without rebuilding the timeline in SQL. Missing
history, incompatible identities and future cursors recover through the existing
full-response contract. Cold recovery briefly coalesces builders. Cache outages
retain authoritative reads; overload performance in that degraded mode is not
promised. Caller-owned transactions bypass the shared path.

Anonymous, unfiltered JSON GETs use the existing security middleware and can return
warm responses without SQL. Authenticated and filtered requests still authenticate
and resolve the match before reusing a public payload. No permissions, session
fields, private tracker state or audit history are added to these envelopes.

Committed SSE notifications now optionally carry `public_reads`: full public
summary/statistics payloads and event/shot timeline deltas. The publisher reads the
already prepared envelopes only when their revision matches the notification.
The combined resource payload budget is 64 KiB; omitted or oversized resources
retain ordinary HTTP invalidation and recovery.

A timeline delta starts at the previous revision that changed that resource, so
unrelated statistics revisions do not force another timeline GET. Clients apply
it only when they have a compatible identity and a sufficiently recent base, and
when every ordered event ID can be reconstructed. Deletions and ordering remain
authoritative. Gaps, malformed optional payloads and coalesced notifications that
lack the needed update fall back to HTTP. Public updates cancel older in-flight
reads and cannot overwrite a newer pushed revision. Permission/editor queries and
private tracker data keep their existing invalidations.

Only existing client queries consume pushed public resources, avoiding cache
entries for every unmounted tab. Web route loaders and hooks now join one bounded
startup wait per match. Opt-in `snapshot=1` SSE connections receive full cached
public bases (256 KiB combined resource budget) before subsequent deltas. Reads
and encoded resource frames are shared per worker/match/revision for at most five
seconds, without extending the underlying 30-second snapshot freshness bound.
Live clocks are rendered separately for each connection so `server_time` remains
current. No SQL or cache rebuild runs in this handshake.

Missing resources and unavailable connections recover through HTTP after a
1.5–2-second staggered wait. Recovery can request a fresh SSE connection, with
250–750 ms jitter and a five-second cooldown. Same-revision snapshots can repair
missing query data, while older snapshots cannot replace newer state. Newly
mounted score views reuse a live snapshot for five seconds; explicit invalidation
and disconnected polling still refresh it. Periodic revision reconciliation tries
a full shared snapshot before publishing an invalidation, allowing one extra
reconciliation cycle for broker publication before issuing recovery invalidations,
even when some public caches are already prepared. Advancing revisions do not
restart this grace period, so a broker outage cannot starve recovery.

When a viewer still has a pending update, replacement frames are shared by the
union of affected resources instead of being serialized separately for each viewer.
At most 32 distinct invalidation sets plus one all-resource fallback are encoded
per update; each viewer retains one pending frame per match. Older payloads are
never relabelled with newer revisions, and existing HTTP recovery remains intact.

Older clients keep their ready-only handshake and HTTP recovery. Unconfigured
shared-query clients, including Expo, retain their HTTP startup. API and web
updates should be released together to realize the request reduction.

`python -m korfbal.serve` now supervises four Granian pools in the existing web
image. It forwards shutdown signals and exits if any pool fails, allowing the
container restart policy to recover the complete deployment unit.

| Port | Responsibility                            | Default process allocation                                         |
| ---- | ----------------------------------------- | ------------------------------------------------------------------ |
| 1664 | Existing API, mutations and admin         | `GRANIAN_WORKERS=4`, ASGI                                          |
| 1665 | Summary, stats, events and shots GET/HEAD | `KORFBAL_PUBLIC_WORKERS=2`, WSGI, `KORFBAL_PUBLIC_THREADS=2`       |
| 1666 | `/api/live/events/`                       | `KORFBAL_SSE_WORKERS=2`, ASGI                                      |
| 1667 | Score/clock `live/` and `live/poll/`      | `KORFBAL_LIVE_READ_WORKERS=2`, WSGI, `KORFBAL_LIVE_READ_THREADS=2` |

Live-read, public-read and SSE admission are explicit and independent:
`KORFBAL_LIVE_READ_BACKPRESSURE=16384`, `KORFBAL_PUBLIC_BACKPRESSURE=16384`
and `KORFBAL_SSE_BACKPRESSURE=8192` are per-worker limits. Admission is not a throughput guarantee. Size process counts,
file descriptors, proxy connection limits and the combined database connection
budget for the deployment; all pools still share the host CPU and database.

The checked-in production Compose exposes those ports. The tracked Nginx example
routes exact GET/HEAD score/clock paths to the live pool, other public resources
to the public pool, and keeps other methods on the API pool. **The external Hetzner Caddy configuration is not managed by this
repository's image release script.** Its API-site routing needs the equivalent
ordered rules before isolation is active there, for example inside the existing
site block:

```caddyfile
@match_sse path /api/live/events/
@match_live {
    method GET HEAD
    path_regexp match_live ^/api/matches/[0-9a-fA-F-]+/live(/poll)?/$
}
@match_reads {
    method GET HEAD
    path_regexp match_reads ^/api/matches/[0-9a-fA-F-]+/(summary|stats|events|shots)/$
}
route {
    reverse_proxy @match_sse kwt-uwsgi:1666 {
        flush_interval -1
    }
    reverse_proxy @match_live kwt-uwsgi:1667
    reverse_proxy @match_reads kwt-uwsgi:1665
    reverse_proxy /api/* kwt-uwsgi:1664
}
```

Retain the site's existing TLS, admin, media and other routing controls. Release the
image and proxy configuration together; roll back proxy upstreams to port 1664
before rolling back the image. Applying this code does not deploy or modify Caddy.

The load harness accepts `--public-workers`, `--public-backpressure`,
`--live-backpressure`, `--sse-backpressure` and `--generator-shards`. Shards use
independent processes and loopback source addresses, preserving one coordinated
writer per match and combining raw
latency samples and final revision checks. This avoids one Python event loop or
one ephemeral-port range being mistaken for the application's capacity; generators
still share host CPU with the application.

```bash
corepack pnpm nx run korfbal-django:loadtest --viewers=100,1000,10000 \
    --matches=1 --seconds=180 --db-pool-size=8 --public-workers=2 \
    --live-workers=2 --live-threads=2 --sse-workers=2 --generator-shards=16 --reconnect
```

For a local Caddy routing comparison, add `--proxy`. The owned proxy mirrors the
production API pool matchers and flushes SSE immediately, but uses loopback
HTTP/1.1 without TLS. It does not modify the hosted Caddy installation. Direct
and proxied measurements must use the same worker counts, fixtures and duration.

On Linux, supply both `--server-cpus` and `--generator-cpus` as disjoint lists of
available CPU numbers. For example, on a 16-CPU host:

```bash
corepack pnpm nx run korfbal-django:loadtest --viewers=1000,10000 --seconds=90 \
    --db-pool-size=8 --public-workers=2 --live-workers=2 --live-threads=2 \
    --sse-workers=2 --generator-shards=16 --reconnect --proxy \
    --server-cpus=0,1,2,3,4,5,6,7 --generator-cpus=8,9,10,11,12,13,14,15
```

API/read/SSE/projection processes, data containers and Caddy use the server
partition. The writer, database monitor and spectator generators use the generator
partition. This separates CPU scheduling, not physical machines, shared memory,
networking or unrelated host workloads; CPU numbers may also share physical cores.
Image identities and selected partitions are recorded with the results.

`sse_response_bytes` counts received SSE body bytes, including comments and frame
delimiters. It is separate from ordinary HTTP `response_bytes`; neither includes
TCP, HTTP headers or TLS overhead. Calculate average received SSE Mbit/s as
`bytes * 8 / elapsed_seconds / 1_000_000`. Starting snapshots and reconnects can
produce much larger bursts than this average. A bandwidth-limited generator can
make application capacity appear lower, so measure network utilization before
interpreting a remote run as a server latency limit.

The [CPU-partitioned proxy comparison](loadtest/results/proxy-capacity.json) used
90-second phases with 30 writes and a halfway reconnect. These are single runs
per configuration, not a statistical attribution of latency causes:

| Route / SSE workers | Viewers | Live GET p95 | Summary GET p95 | Published update p95 | SSE average Mbit/s |
| ------------------- | ------- | ------------ | --------------- | -------------------- | ------------------ |
| Direct / 2          | 1,000   | 650 ms       | 307 ms          | 65 ms                | 48.5               |
| Caddy / 2           | 1,000   | 11 ms        | 30 ms           | 108 ms               | 49.2               |
| Caddy / 4           | 1,000   | 11 ms        | 17 ms           | 62 ms                | 48.9               |
| Direct / 2          | 10,000  | 1,520 ms     | 3,882 ms        | 667 ms               | 535.3              |
| Caddy / 2           | 10,000  | 2,760 ms     | 7,351 ms        | 625 ms               | 511.2              |
| Caddy / 4           | 10,000  | 2,737 ms     | 4,437 ms        | 643 ms               | 530.8              |

All phases completed 30 writes and every initial/reconnect ready event, without
HTTP/SSE errors, stale final live views or pending/failed jobs. Only Caddy/four
workers at 1,000 viewers passed every configured gate; all three commands exited
1 because their 10,000-viewer phase failed. More workers helped the smaller
phase but did not improve 10,000-viewer delivery, so production defaults remain
unchanged. Generator lag was 63–83 ms p95 at 10,000 viewers and remains a possible
measurement bottleneck. A 100 Mbit/s remote receiver cannot sustain this measured
10,000-viewer workload. Reducing HTTP requests has not removed SSE bandwidth cost.

Compact SSE is enabled by new browser clients with
`/api/live/events/?match_ids=<uuid>&snapshot=1&compact=1&resources=live,summary,events`.
Legacy clients retain `match.changed`; the new client accepts legacy responses
so the backend can be rolled back independently. Public resource subscriptions
follow active queries and route-loader fetches. Visited inactive match tabs stop
requesting their resources; private resources still receive invalidation metadata
without their data entering the public stream.

`match.compact` version 1 carries a match ID, dictionary epoch, stream sequence,
base sequence, durable match revision, resource codes, dictionary additions,
operations, snapshot flag and clock anchors. The bounded match/subscription
mapping interns JSON field names, player/event UUIDs and enum strings as integer
references. Tagged values preserve arrays, objects, booleans, numbers and null.
Operations replace/delete fields or splice a changed array interval, so normal
shot additions do not resend the full timeline ordering and statistics updates
only send changed fields. Django still computes authoritative state; clients do
not duplicate scoring/statistics rules.

Each serving worker encodes once per match and public resource mask (at most 32
masks). Slow viewers receive one shared full reset instead of an unbounded patch
queue. A missing sequence, dictionary mismatch or invalid reconstructed domain
payload triggers bounded reconnect and authoritative HTTP recovery. Dictionary
size is capped at 8,192 entries and a frame at 512 KiB; oversized documents fall
back to invalidations. Compact cache bootstraps have a 1 MiB input budget before
encoding. Epoch resets and cold cache recovery are explicit; late cache/broker
messages cannot roll back newer state, and incomplete handshakes cannot discard
an established group while publication catches up. Stream sequences also distinguish later
background projections at the same durable match revision.

Native EventSource reconnects send `Last-Event-ID` using the dictionary epoch and
stream sequence. Workers use one canonical dictionary/sequence per match/resource
cohort in the existing `public_live` Redis cache. Atomic compare-and-swap prevents
concurrent writers from losing updates; duplicate publications share a sequence.
A replacement worker can restore that state and replay up to 32 retained frames,
bounded to 64 KiB of wire bytes (not per viewer). A current cursor needs no compact
frame. Unknown/expired cursors, epoch resets or a database revision ahead of
publication retain snapshot recovery. Local state is released with the last
subscriber; the shared checkpoint survives for up to 120 seconds after its last
update, including a complete serving-worker restart. It is bounded to 2 MiB per
match/resource cohort, including the document, dictionary and replay history.

Redis operations run per worker/cohort publication and coalesced bootstrap, outside
viewer delivery loops. Cache errors, contention after three attempts, oversized
state or a full same-revision deduplication budget fall back to local encoding with
a new epoch and shared reset. Recovery rejoins canonical state through a reset;
local fallbacks cannot fork the same shared cursor. The
`korfbal_sse_shared_compact_total` counter exposes `success` and `fallback`
results. This adds backend Redis traffic and state serialization per update; it
does not claim lower steady-state latency. Multiplexed connections resume only
the match identified by the last cursor; other matches get snapshots. A new EventSource
instance still starts from a snapshot, since it has a new decoder.

The [100-shot reconnect profile](loadtest/results/compact-sse-replay.json) measured
one missed shot publication at 267 bytes versus an 8,257-byte full compact snapshot
for live/summary/events (96.8% less); a missed goal was 720 versus 8,618 bytes (91.6%
less). All-public subscriptions used 940 versus 37,224 bytes for that shot (97.5%
less). The cursor adds 39 bytes to every compact frame at these sequences. Figures
exclude ready/HTTP/TCP/TLS overhead and do not establish network latency or capacity.
Chromium and WebKit verify native reconnect cursors, zero compact bytes when current,
and a missed update replayed into the existing decoder without public GETs.

The [cross-worker replay profile](loadtest/results/shared-sse-replay.json) uses the
production Redis-backed encoder on a 100-shot fixture. One missed shot used 266 B
instead of an 8,256 B full compact snapshot for live/summary/events (96.8% less);
a missed goal used 721 B instead of 8,618 B (91.6% less). These savings now survive
a worker change while the shared checkpoint and cursor history remain available.
This does not eliminate the last-cursor limitation for multiplexed connections.

A [local capacity rerun](loadtest/results/shared-sse-capacity.json) with two SSE
workers, Caddy HTTP/1.1, sixteen generator shards and separate CPU allocations on
one host completed all 30 writes and initial/reconnect handshakes per phase, with
zero public GETs, HTTP/SSE errors or stale final views:

| Viewers | SSE body traffic | Publication-to-receive p95 |
| ------- | ---------------- | -------------------------- |
| 1,000   | 3.93 Mbit/s      | 118.71 ms                  |
| 10,000  | 44.84 Mbit/s     | 381.19 ms                  |

The 100 ms gate remains unmet. These single-host runs do not establish a latency
improvement; earlier 1,000-viewer results were sometimes faster. The generator
creates fresh decoders at reconnect, so this capacity run checks ordinary shared
bootstrap/delivery; real-Redis regressions separately verify cursor replay across
worker replacement. Compare reconnect recovery savings separately from steady
traffic and monitor shared-store fallback counts during rollout.

The [event-by-event audit](loadtest/results/sse-event-audit.json) exercises every
registered tracker command on a 100-shot synthetic match, with additional clock
and period transitions. The reserve-read command emits no public notification.
It compares the same payloads and emission timestamps with/without the redundant
`timer.server_time` field: existing decoders reconstruct that clock anchor from
the frame timestamp, so it no longer needs a dictionary entry or patch operation.

| Live/summary/events update | Before  | After   |
| -------------------------- | ------- | ------- |
| Goal                       | 720 B   | 663 B   |
| Pause                      | 772 B   | 715 B   |
| Resume                     | 525 B   | 468 B   |
| Timeout                    | 773 B   | 716 B   |
| Undo event                 | 371 B   | 314 B   |
| Initial snapshot           | 8,247 B | 8,185 B |

Shot-only and possession updates are unchanged. Bootstrap sending also discards
already-covered pending frames before awaiting socket writes, preventing a second
copy of the snapshot without dropping publications that arrive during a send.
Match and tournament heartbeats now use an empty SSE comment (3 B versus 13 B),
saving about 0.053 Mbit/s of body bytes at 10,000 connections and a 15-second interval.

Further savings remain: the first measured 267 B shot update consists of 68 B of
SSE framing, 147 B of packet metadata/separators, 2 B for an empty dictionary list,
and 50 B of patch operations. Reducing that metadata requires a protocol tradeoff;
this audit preserves native reconnect and existing decoders. Representative ready,
tournament and legacy invalidation frames are 168 B, 105 B and 116 B respectively,
with their exact synthetic payloads in the report. These figures exclude any
follow-up GETs. Editor, lineup, provider and asynchronous projection publications
are not exhaustively profiled, and no new latency/capacity claim is made.

The Python/TypeScript [shared contract fixture](../../../fixtures/korfbal/compact-sse.json)
covers inserts, goals, edits, reordering, deletion and dictionary reset. The
browser journey uses the production Python encoder and verifies initial loading,
reconnect and timeline edits without public GETs in Chromium and WebKit.
Use `--compact` on the load test for the same three-resource-per-viewer workload
as legacy measurements. The load test includes initial dictionaries, resets,
heartbeats and asynchronous updates in received SSE bytes; TCP/TLS overhead is
still excluded.

The initial [compact proxy comparison](loadtest/results/compact-sse-capacity.json)
uses the same two SSE workers, CPU partitions and 90-second reconnect workload:

| Protocol | Viewers | Public HTTP reads | SSE average Mbit/s | Published update p95 |
| -------- | ------- | ----------------- | ------------------ | -------------------- |
| Legacy   | 1,000   | 306               | 49.1               | 119 ms               |
| Legacy   | 10,000  | 17,151            | 499.8              | 613 ms               |
| Compact  | 1,000   | 0                 | 3.8                | 132 ms               |
| Compact  | 10,000  | 0                 | 42.6               | 526 ms               |

Each phase completed 30 writes and all initial/reconnect handshakes, with no
HTTP/SSE errors, stale final views, pending jobs or job failures. Compact phases
made only 60 HTTP requests: 30 tracker state reads and 30 commands. At 10,000
viewers, received SSE bytes fell 91.5%, but delivery p95 remains above 100 ms.
Both commands exited 1 on the latency gates; compact also has no live-GET samples
for that gate because no such requests were needed. Average traffic is below
100 Mbit/s, but startup/reconnect bursts, transport overhead and WAN latency were
not constrained or measured. This is not a production capacity guarantee.

Compact-only broadcasts also skip serialization of the unused legacy JSON frame.
Mixed-client groups serialize that frame once and share it among legacy viewers;
compact patches and slow-viewer resets retain their existing shared encoding.

SSE connections now use persistent request-receive and mailbox-send loops.
They wait on connection completion once instead of creating a task and a new
`asyncio.wait()` cycle per notification. Socket backpressure still leaves only
one pending frame per match; disconnects and server cancellation can stop a blocked
sender before releasing its shared subscription. Fixed Prometheus labels for
update and heartbeat deliveries are resolved once per process. Their counters
still increment after every successful ASGI write, excluding failed writes.

Compact load runs additionally report three stages for live-bearing publications:
`publication_to_compact_encode` (publisher timestamp to frame-construction
timestamp), `compact_encode_to_receive` (that timestamp to parsed SSE/JSON
receipt), and `compact_live_decode` (reconstructing compact state). The frame
timestamp precedes final JSON serialization, and receipt includes generator
scheduling. These stages locate delay without claiming separate broker, proxy,
socket or browser-rendering measurements. Their p95 values must not be added as
though they describe the same observation.

The subsequent [persistent-loop comparison](loadtest/results/sse-dispatch-capacity.json)
repeats that workload with unchanged compact resource scopes:

| Connection dispatch | SSE workers | 1,000 viewers: update p95 | 10,000 viewers: update p95 |
| ------------------- | ----------- | ------------------------- | -------------------------- |
| Per-message tasks   | 2           | 132 ms                    | 526 ms                     |
| Persistent loops    | 2           | 44 ms                     | 413 ms                     |
| Persistent loops    | 4           | 42 ms                     | 476 ms                     |

All phases retained zero public GETs and completed without HTTP/SSE errors,
stale final views or failed/pending jobs. The two-worker run used approximately
3.8/42.9 Mbit/s at 1,000/10,000 viewers. At 10,000 viewers its publication-to-frame
p95 was 6.92 ms, frame-to-receipt p95 407.51 ms, and compact reconstruction p95
0.10 ms. Most measured delay is after frame construction; the generator also
recorded scheduling lag, so a separate load-generator host is needed to narrow
attribution. Four workers did not improve that phase in this single-run comparison;
production defaults remain unchanged. The 1,000-viewer push p95 is below 100 ms,
but the full gates still fail for missing live-GET samples and the 10,000-viewer
push latency. These figures measure published updates, not tracker write latency
or browser rendering.

A subsequent [metric-label control comparison](loadtest/results/sse-metrics-capacity.json)
keeps lazy legacy encoding enabled in both runs and varies only fixed-label lookup:

| Metric lookup | 1,000 viewers: update p95 | 10,000 viewers: update p95 |
| ------------- | ------------------------- | -------------------------- |
| Cached labels | 42 ms                     | 557 ms                     |
| Per delivery  | 121 ms                    | 508 ms                     |

The cached-label run preceded the control. Both completed all writes and
handshakes, with zero public GETs and no HTTP/SSE errors, stale final views or
failed/pending jobs. Both failed the overall latency gates. These single local
runs do not establish a delivery-latency improvement; counter lookup savings
measured in isolation must not be presented as a network latency gain.

The [implemented compact payload profile](loadtest/results/compact-sse-payloads.json)
measures real production encoder output. With 100 initial shots:

| Frame                  | Legacy SSE | Compact, all public resources | Compact, live/summary/events |
| ---------------------- | ---------- | ----------------------------- | ---------------------------- |
| Initial snapshot       | 76,507 B   | 36,811 B                      | 8,208 B                      |
| New missed shot        | 12,882 B   | 901 B                         | 228 B                        |
| New goal               | 14,825 B   | 1,185 B                       | 682 B                        |
| Subsequent missed shot | 13,001 B   | 775 B                         | 212 B                        |

The compact initial snapshot includes live state; the legacy profile excludes
its separately delivered ready clock. These frame measurements include mapping
additions and SSE delimiters, but are not whole-stream bandwidth measurements.

With 500 initial shots, compact missed-shot updates were 626 B and 594 B
(all public resources), versus 31,623 B and 31,740 B in legacy SSE. The compact
goal was 1,459 B versus 33,567 B. The larger initial compact snapshot was
160,766 B and included the complete shots base; legacy's 68,203 B starting frame
omitted shots under its smaller cache budget. Initial history remains proportional
to match size, while ordinary updates no longer resend that history.

Payload research is reproducible against owned synthetic services with
`corepack pnpm nx run korfbal-django:loadtest --profile-payloads --shots=100`
(or `--shots=500`, with a distinct `--output` directory). The profiler records
sizes, not identities or raw payloads, and refuses non-disposable databases.
[Actual payload measurements](loadtest/results/payload-profile.json) show:

| Starting history | New missed-shot SSE frame | Statistics JSON | Timeline order JSON | Frame for live/summary/events subscribers only |
| ---------------- | ------------------------- | --------------- | ------------------- | ---------------------------------------------- |
| 100 shots        | 12,880 B                  | 6,777 B         | 4,721 B             | 1,307 B                                        |
| 500 shots        | 31,622 B                  | 6,797 B         | 23,441 B            | 4,427 B                                        |

These are individual synchronous publications, not the complete asynchronous
statistics stream or traffic-weighted averages. They demonstrate that unmounted
resources and complete timeline order lists are substantial costs. A missed shot
should not require retransmitting the entire known ordering once a versioned
incremental ordering contract exists. Keep edits/deletes and ordering changes
explicit; do not assume every update is an append.

A reversible tagged-JSON UUID-reference prototype reduced the 100-shot update
JSON from 12,851 B to 8,904 B, including new dictionary entries and their envelope.
At 500 shots, the first update needed 402 new dictionary entries and only shrank
from 31,593 B to 30,747 B; a subsequent missed-shot update shrank from 31,710 B to
15,186 B. Mapping setup/recovery cost matters. The 500-shot starting SSE snapshot
omitted the oversized shots resource under the existing 256 KiB budget, which is
why those shot references were not already in the dictionary. This prototype is
not an adopted wire protocol; typed reference tags and mapping versions require
an explicit negotiated contract.

An [illustrative compact goal record](loadtest/results/compact-event-formats.json)
containing schema/mapping versions, revision/base, event identity/operation/type,
player reference, team, period, relative milliseconds, score and clock-running
state encoded as 39 B positional JSON, 22 B CBOR or 32 B Protobuf. These figures
exclude dictionaries, bootstrap state, other resource updates and transport
framing. They are not equivalent to replacing an entire public match update.
The experiment round-tripped the record with the recorded codec versions.

[SSE is UTF-8 text](https://html.spec.whatwg.org/multipage/server-sent-events.html),
so binary requires text encoding or a different transport. In that small example,
Base64 produces 32 B CBOR or 44 B Protobuf, versus 39 B plain positional JSON;
changing transport is not a prerequisite for substantial savings.
[CBOR](https://www.rfc-editor.org/rfc/rfc8949.html) and
[Protobuf](https://protobuf.dev/programming-guides/encoding/) support compact
integer representations. One-byte player slots can cover a match roster, but not
an unlimited global user/event namespace; use extensible integers and never reuse
slots within a mapping version. Match-relative time can be smaller than an
absolute timestamp, but running clocks still require server clock anchors.

The compact implementation above adopts resource subscriptions, incremental
ordering/statistics patches and match-scoped dictionaries. Binary formats remain
an optional future comparison. Keep Django authoritative for
scores/statistics instead of copying all business rules into clients. Negotiate
the schema, bind references to the match/mapping version, reject duplicate/stale
operations, detect revision gaps, and retain snapshot recovery for reconnects,
expired replay and unknown mappings. Preserve the current protocol for older
clients until the new contract has cross-runtime regression and load coverage.
Offline gzip also shrank sampled frames, but those figures do not establish
streaming compression latency, CPU cost or flushing correctness.

The baseline [shared public read measurements](loadtest/results/shared-public-reads.json),
before public resource pushes, ran each phase for three minutes with the allocation above. All three phases failed
the overall latency gate; these are measured limits, not a production capacity claim.

| Viewers | Live HTTP p95 | Published snapshot → receipt p95 | Tracker command p95 | HTTP errors      |
| ------- | ------------- | -------------------------------- | ------------------- | ---------------- |
| 100     | 172 ms        | 12 ms                            | 419 ms              | 0                |
| 1,000   | 772 ms        | 132 ms                           | 690 ms              | 0                |
| 10,000  | 10,383 ms     | 976 ms                           | 1,092 ms            | 341,241 timeouts |

All phases committed 60 commands with no missed write slots, SSE errors or stale
final viewers recorded. The 10,000-viewer phase recorded 20,000 ready events across
initial connections and reconnects. Statistics calculations encountered concurrent
revision changes; durable retries recovered, leaving no pending or failed jobs at
phase end. Successful-request percentiles exclude timeouts and deadline cancellations.

Publication-to-receipt excludes the write transaction and snapshot preparation.
Write-start-to-SSE p95 was 518 / 1,025 / 2,565 ms respectively. At 1,000 viewers,
summary and events HTTP p95 remained 5,499 / 4,808 ms despite application middleware
timing of roughly 10 ms p95: most measured latency was outside that application
interval, consistent with request queueing. SQL calls grew from 32,234 to 38,205
between 100 and 10,000 viewers while attempted HTTP requests grew from 10,808 to
389,849. Shared reads bound database work but do not remove per-viewer HTTP work.

Generator lag p95 was 1 / 3 / 92 ms. Sixteen generator processes still share this
16-logical-CPU host with the application; a physically separate generator and the
real proxy are needed for production sizing. Earlier evidence used different
worker and generator allocations, so comparisons are not single-variable trials.
The subsequent [public-push measurement](loadtest/results/pushed-public-resources.json)
used the same configured topology and three-minute phases:

| Viewers | HTTP requests before → after | Reduction | Live HTTP p95 | Snapshot → receipt p95 | Command p95 | HTTP timeouts |
| ------- | ---------------------------- | --------- | ------------- | ---------------------- | ----------- | ------------- |
| 100     | 10,808 → 3,409               | 68%       | 243 ms        | 20 ms                  | 396 ms      | 0             |
| 1,000   | 85,833 → 19,730              | 77%       | 1,017 ms      | 70 ms                  | 444 ms      | 0             |
| 10,000  | 389,849 → 281,866            | 28%       | 9,897 ms      | 785 ms                 | 532 ms      | 203,743       |

All 60 writes committed in each phase, without missed write slots, SSE errors or
stale final live views. Projection retries recovered with no pending or failed
jobs at phase end. Write-start-to-SSE p95 was 501 / 567 / 1,159 ms; this includes
more of the operation than publication-to-receipt. Generator lag p95 was
0.9 / 1.3 / 67.8 ms. **Every phase still failed the overall latency gate.**

At 1,000 viewers summary/events HTTP p95 fell to approximately 3.4 seconds, but
remaining HTTP requests still queue. At 10,000 viewers the harness recorded 328,030
unusable timeline deltas alongside initial/recovery HTTP overload: viewers without
a usable initial timeline cannot apply incremental changes. There were also 6,667
HTTP requests cancelled at the workload deadline. Further work should address
initial/reconnect snapshot delivery and recovery bursts, then measure through the
real proxy with physically separate generators.

The [SSE startup snapshot follow-up](loadtest/results/sse-bootstrap.json) used the
same configured allocation and duration, including reconnects:

| Viewers | HTTP requests before → after | Reduction | Live HTTP p95 | Published update → receipt p95 | Command p95 | HTTP timeouts |
| ------- | ---------------------------- | --------- | ------------- | ------------------------------ | ----------- | ------------- |
| 100     | 3,409 → 562                  | 84%       | 8 ms          | 12 ms                          | 391 ms      | 0             |
| 1,000   | 19,730 → 2,562               | 87%       | 8 ms          | 56 ms                          | 418 ms      | 0             |
| 10,000  | 281,866 → 81,950             | 71%       | 1,626 ms      | 541 ms                         | 448 ms      | 46,858        |

The 100/1,000 phases passed the configured gates (100 ms live-read and published
update p95, 1,000 ms command p95, no errors/stale live views). These gates do not
require every endpoint or the full write path to take 100 ms. Summary HTTP p95 was
458 / 1,545 / 9,877 ms. Live HTTP samples shrank to only 14 / 130 / 1,320 because
pushes replaced most reads. SSE-ready p95 was 45 / 302 / 4,783 ms.

All phases recorded the full 200 / 2,000 / 20,000 initial/reconnect ready events,
60 committed writes, no missed writes or SSE errors, no stale final live views,
and no pending/failed jobs after projection retries. The 10,000 phase still failed:
besides timeouts, 4,927 requests were cancelled at the deadline and 113,433 deltas
required recovery. Generator lag p95 was 0.8 / 1.1 / 38.0 ms. This remains an overloaded local experiment, not production sizing.

This harness models starting snapshots and periodic shared recovery, but does not
model the web client's proactive snapshot reconnect cooldown after an unusable
delta. Its write-start-to-SSE metric now also observes initial/reconnect snapshots
of already committed revisions, so that sample population differs from the earlier
run. Published-update delivery excludes those bootstrap frames. Browser tests
separately verify zero initial live/summary/events GETs when a snapshot is available.
Remaining work is to control overload recovery and measure with physically separate
generators through the real proxy; the 100 ms goal for all requests remains unmet.

The [shared recovery follow-up](loadtest/results/shared-recovery.json) retains the
same topology, three-minute phases and reconnects. Allowing normal post-commit
publication one reconciliation cycle avoids premature invalidations and refetches:

| Viewers | HTTP requests before → after | Reduction | Live GET p95 | Summary GET p95 | Published update → receipt p95 | Command p95 |
| ------- | ---------------------------- | --------- | ------------ | --------------- | ------------------------------ | ----------- |
| 100     | 562 → 162                    | 71%       | 8 ms         | 9 ms            | 14 ms                          | 376 ms      |
| 1,000   | 2,562 → 504                  | 80%       | 7 ms         | 26 ms           | 54 ms                          | 375 ms      |
| 10,000  | 81,950 → 3,524               | 96%       | 1,770 ms     | 3,157 ms        | 541 ms                         | 451 ms      |

All phases had zero HTTP timeouts/cancellations, missed writes, SSE errors, stale
final live views or pending/failed jobs. Each committed 60 writes and received all
initial/reconnect ready events. At 10,000 viewers, delta recoveries fell from
113,433 to 443, but live-read and delivery latency still failed the 100 ms gates
(the command exited 1). SSE-ready p95 was 50 / 304 / 4,178 ms; live GET sample
counts were 14 / 128 / 987. Generator lag p95 was 0.8 / 1.1 / 24.2 ms.
At 10,000 viewers, live/summary application-timer p95 was 10 / 11 ms, indicating
most measured request latency lies outside that timer; this does not isolate the
cause or establish production capacity. Real lost broker notifications may take
one additional reconciliation cycle to recover.

The separate [encoding microbenchmark](loadtest/results/shared-encoding-microbenchmark.json)
used 10,000 pending mailboxes and a 64 KiB payload. Sharing replacement encoding
reduced median publish/coalescing time from 2,575 ms to 8.8 ms and distinct retained
wire buffers from 10,000 (657 MB) to one (66 KB). These are internal operation and
buffer measurements, not HTTP/network latency or total process memory. Runtime
hashes were frozen before the full load run and verified afterward.

Request counts include tracker requests. The request mix changes when pushes
replace reads, so latency comparisons do not represent identical HTTP traffic.
The harness checks final notification/live revisions, not every public payload;
public payload correctness is covered by regression/browser tests. Its
`response_bytes` counter excludes SSE traffic and cannot establish total bandwidth
savings. These independent local runs do not establish production capacity.

The load test now enforces a 100 ms live-read p95 budget in addition to the
command budget. `--max-live-p95-ms` changes this explicit acceptance threshold.
Results include client latency, the existing application timing header, their
per-request difference, and separate cache-hit/miss distributions. Time outside
the application timer includes dispatch, response completion and transport;
it must not be interpreted as a pure queue-wait measurement.

To measure an isolated synchronous live-read pool alongside the ASGI API/SSE pool:

```bash
corepack pnpm nx run korfbal-django:loadtest --viewers=100 --matches=1 --seconds=180 \
    --db-pool-size=8 --live-workers=2 --reconnect
```

The harness routes live reads to `--live-workers` when supplied, then to
`--public-workers` when supplied, otherwise to the main ASGI pool.
The isolated mode adds WSGI workers with two blocking threads each
(`--live-threads`), running the same Django application, security
middleware, database and Redis. It changes process allocation, not the public
response contract. This narrow live-only pool remains available for comparisons.
The production web
image includes this live-read pool plus a separate pool for summary, stats, events
and shots; the proxy must route each family to its designated pool.
SSE belongs on its dedicated ASGI pool; writes and other routes stay on the main
API pool. The live-read command, using the same API image and environment, is:

```bash
granian --interface wsgi --no-ws --host 0.0.0.0 --port 1667 \
    --workers 2 --blocking-threads 2 --backpressure 16384 korfbal.wsgi:application
```

Keep port 1665 on the private service network and preserve the existing proxy's
host, HTTPS and CORS configuration. Budget PostgreSQL connections across both
pools and deploy matching application revisions. This command is configuration
for a supervised service, not a background process to launch inside the API container.

The pre-push [live-read follow-up evidence](loadtest/results/live-read-latency.json)
records four fresh three-minute runs with 100 spectators and the final code:

| HTTP process allocation                         | Live p95 | Application-timer p95 |
| ----------------------------------------------- | -------- | --------------------- |
| Existing four ASGI workers                      | 594 ms   | 19 ms                 |
| Four ASGI + two WSGI workers, two threads each  | 108 ms   | 8 ms                  |
| Four ASGI + four WSGI workers, two threads each | 192 ms   | 11 ms                 |
| Four ASGI + four WSGI workers, one thread each  | 242 ms   | 9 ms                  |

The earlier published-snapshot run measured 1,097 ms live p95 with four shared
ASGI workers. All four follow-up runs completed 60 writes with no HTTP/SSE errors,
stale final views or cache misses, but **every run failed the strict 100 ms live
budget**. These measurements do not establish a reliably sub-100 ms deployment;
increasing worker count did not consistently improve the tail on this shared host.

The checked-in [unpooled](loadtest/results/unpooled-single-match.json) and
[pooled](loadtest/results/pooled-single-match.json) samples use identical inputs:
one match, 100 initial shots, four web workers, background statistics jobs and
a halfway reconnect, with 30 seconds per viewer level. They are short local
samples, not production capacity guarantees. Percentiles below include successful
command responses only; the failure counts are essential to interpreting them.
These samples used Python 3.14.7 and Django 6.1.1; the production image currently
pins Python 3.13. Repeat on the production runtime and hardware before sizing it.

| Spectators | Unpooled HTTP errors | Pooled HTTP errors | Command p95, unpooled | Command p95, pooled |
| ---------- | -------------------- | ------------------ | --------------------- | ------------------- |
| 10         | 0                    | 0                  | 1,000 ms              | 562 ms              |
| 50         | 99                   | 0                  | 5,871 ms              | 1,327 ms            |
| 100        | 344                  | 0                  | 3,998 ms              | 2,514 ms            |

The unpooled server logged PostgreSQL's `too many clients already` error. Pooling
kept sampled database usage at 34 connections at the two higher viewer levels,
with no stale final viewers or job errors. The 50/100-viewer pooled phases still
failed the one-second command budget; the 100-viewer phase missed one scheduled
write interval. These historical samples predate the current public snapshot
publication path. The [published snapshot comparison](loadtest/results/published-live-snapshots.json)
retains fresh one- and three-minute before/after runs. At 100 viewers, the longer
run reduced live-read p95 from 2.58 to 1.10 seconds; both versions completed 59/60
scheduled writes and failed the arrival gate. These improvements do not establish
a production capacity limit.

The [four-match sample](loadtest/results/pooled-four-matches.json) distributes 40
viewers across four matches, each starting with 100 shots, with one command every
three seconds per match for 60 seconds and no forced reconnect. With pooling it
completed 78 commands, with zero HTTP errors, stale viewers or pending jobs, and
a sampled peak of 35 database connections. Command p95 was 2,180 ms and two write
intervals were missed, so this scenario also failed the latency/arrival budget.

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
Each active destination receives a separate durable delivery job with bounded
transport timeouts. A failing destination does not prevent attempts to the remaining
devices.

Schedule changes, match notifications, statistics and media work now record durable
jobs inside the domain transaction and dispatch them immediately after commit. A
broker outage leaves the work pending. Beat recovers missed dispatches and promotes
deadlines within the next minute every 60 seconds. Short ETAs preserve debounce and
retry timing; multi-hour deadlines stay in the database. Repeated statistics/media
requests combine into the latest generation. Changes arriving during execution
schedule another generation. Failed work gets up to five attempts with backoff; process
loss is recovered after the bounded execution deadline. PostgreSQL advisory locks
prevent overlapping executions of the same job. Use a direct PostgreSQL connection
(or session pooling), not transaction-mode PgBouncer, for workers.

The worker image supervises separate `celery`, `instant`, `projections`, `media` and
`competition` pools. Their default concurrency is 2/1/2/1/1 and can be overridden with
the `KORFBAL_*_CONCURRENCY` variables in `.env.example`. The dedicated `instant` pool
handles authentication and private KNKV form actions independently of general
background jobs. Queued form actions dispatch after commit and continue draining
due work without scanning team fixtures. Separate discovery/recovery and competition
refresh checks run once a minute in the `competition` pool, retaining provider leases,
deadlines and pacing. Background KNKV imports yield between feeds for due form
actions. Beat must run once per deployment.

Match completion and subsequent timeline/lineup corrections create eligible
substitution jobs with the final live revision inside the same database transaction.
The worker dispatches after commit. If a correction arrives during a provider upload,
completion reconciles that match again and queues a successor; published event IDs
remain available for safe reconciliation. Statistics-only revisions do not cause
substitution uploads. Enabling a team connection and publishing/rescheduling a source
fixture also check only the affected scope for due form work.

The worker trigger audit is:

| Work                             | Trigger                                                         |
| -------------------------------- | --------------------------------------------------------------- |
| Substitutions                    | Finished-match/timeline revision; scoped recovery after upload  |
| Private roster imports           | Due fixture/connection changes; scheduled pre-match windows     |
| Manual form actions              | Committed action request                                        |
| Statistics                       | Committed timeline changes, coalesced per match                 |
| Audio downloads and clips        | Song creation/settings changes and source completion            |
| Match and schedule notifications | Committed match completion or publication event                 |
| MVP reminder/publication         | Deadlines recorded after match completion                       |
| Authentication email             | Direct dispatch through the shared `instant` queue              |
| Competition feeds                | External refresh deadlines, provider pacing and bounded batches |

MVP publication is separate from notification delivery. Each recipient gets an
independent durable intent; completed keys prevent ordinary duplicate delivery.
An external provider accepting a push immediately before worker death can still
cause a repeated push on recovery. Successful intents discard their payloads while
retaining their deduplication keys. `python manage.py background_jobs` reports safe
pending/error counts, and `python manage.py background_jobs --retry-id ID` retries
one exhausted job without replaying completed notifications.

Apply migrations before activating the new worker image. Migration 0002 adopts
non-failed audio sources and unpublished MVP deadlines without broker calls. Drain
the old worker before replacing it, retain a 35-minute container shutdown grace
period, and verify all four worker names respond after startup. Existing Celery task
names remain available for queued messages. Rollback may leave the additive jobs
table in place; old images do not drain it, so preserve it for a forward recovery.

### Security remediation rollout

Apply migrations before starting the updated backend. Release the matching mobile client: old JWTs without a refresh-session identifier require sign-in again, and old unverified admin sessions must complete MFA. Ensure staff have a working email address or passkey before rollout.

Media must use a bucket separate from static files. From the configured Korfbal runtime, `python manage.py check_media_privacy` verifies the MinIO bucket policy without changing it. To remove public allow statements while retaining authenticated grants, an operator can run `python manage.py check_media_privacy --repair --probe`. The probe creates and deletes a uniquely named synthetic object; it does not read user files. Test the signed download endpoint and verify the old media hostname no longer serves raw objects after deploying the proxy configuration. Do not treat a successful local probe as production verification.

Spotify track imports now require both `SPOTIFY_CLIENT_ID` and `SPOTIFY_CLIENT_SECRET`. Metadata comes from Spotify's official API; the Celery worker uses yt-dlp, its packaged JavaScript solver, Node, and ffmpeg to find and convert one audio result. Search matching can differ from spotDL. Direct MP3 uploads remain available without Spotify credentials. The existing `SPOTDL_DOWNLOAD_TIMEOUT_SECONDS` setting continues to bound each download attempt for deployment compatibility. The worker accepts only a successful, nonempty MP3 within the upload size limit and kills decoder descendants on timeout.

## Contextual score forecasts

The optional score model predicts goals with separate season/discipline/phase/age/
colour/format baselines, partially pooled class and poule pace, and team attack and
defence. A joint Laplace approximation retains correlated parameter uncertainty;
64 posterior rate draws drive both the API summary and the web graph. Numerical
fitting uses the workspace's NumPy/SciPy development dependencies, never an API request.

Export from an environment with read access to the competition database. Use the
source season UUID, not its editable display name. Keep exports outside Git:

```sh
uv run python apps/django_projects/korfbal/manage.py export_score_forecasts \
  --season <source-season-uuid> --output /secure/forecast-input.json

OPENBLAS_NUM_THREADS=1 uv run python apps/django_projects/korfbal/manage.py fit_score_forecasts \
  --input /secure/forecast-input.json --output /secure/forecast-v1.json \
  --report /secure/forecast-validation.json \
  --origin <first-validation-origin-ISO8601> \
  --origin <second-validation-origin-ISO8601> \
  --cutoff <training-cutoff-ISO8601> --approve
```

All dates must include a timezone. Origins precede the cutoff, which cannot exceed
export time. Results are replayed from their last observed revision before each
origin. Duration must have been observed by that origin (and by kickoff for test
matches). Cups, awarded results, incomplete scores and unverified 0–0 records are
excluded. Contexts need ten training matches; individual teams and poules can be
unseen and are integrated over their priors. Actual verified 0–0 matches cannot yet
be distinguished from placeholders by the feed and remain quarantined.

The report includes score log loss, W/D/L Brier score, goal MAE, marginal interval
coverage, calibration bins, per-context metrics and poule-cluster bootstrap paired
differences. It compares with a Gamma-Poisson context baseline and the old fixed-total
predictor, including exported pre-match ratings and context outcome calibration.
Exports without the legacy forecasts cannot pass the promotion gate. `--cold-start`
reports a separate held-out-poule experiment and cannot approve an artifact.

Approval requires at least 100 tested matches across ten poules and two nonempty
chronological origins, improvement over both baselines in both proper scores with
95% paired bootstrap intervals below zero, and 70–95% marginal coverage for the
nominal 80% intervals. A failed `--approve` still saves the candidate and report,
then exits unsuccessfully. This is a conservative initial gate, not proof of accuracy
for every competition context. Do not repeatedly tune against the same test windows.

Mount a passed artifact at a **new immutable path**, configure
`KORFBAL_SCORE_FORECAST_ARTIFACT` to that path, and restart API workers. Artifacts
record their actual availability time and never apply to earlier kickoffs. Unsupported
contexts, missing duration, invalid or unapproved artifacts retain the previous
predictor. Clear the setting and restart workers to roll back. No fitted production
artifact is bundled, and these commands do not deploy or change ratings.

Limitations: historical score revisions are available, but identities, class mappings,
and fixture schedules use current metadata snapshots; the export reports that caveat.
Live updates condition the rate draws on goals and elapsed time under a constant-rate
Poisson process. They are explicitly labelled unvalidated for live play. Negative
binomial and bivariate alternatives are follow-up experiments after this baseline is
measured. A score target beats 75% of opponent-score scenarios; it does not imply a
75% win probability or that the target is reachable.
