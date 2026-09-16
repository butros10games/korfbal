# Korfbal (Django) (opencode)

Scope: `apps/django_projects/korfbal/**`

## What this is

Korfbal is a Django app for match tracking (incl. live match tracker), players, teams, and stats.

## Most useful commands

Prefer Nx targets (recommended):

- List projects: `corepack pnpm nx show projects --verbose`
- Run tests: `corepack pnpm nx run korfbal-django:test`
- Run ordinary tests while iterating: `corepack pnpm nx run korfbal-django:test-general`
- Run historical migration checks: `corepack pnpm nx run korfbal-django:test-migrations`
- Run lint: `corepack pnpm nx run korfbal-django:lint`

Fallback (from this directory):

- Install deps: `uv sync`
- Dev server: `uv run python manage.py runserver 0.0.0.0:8000`
- Tests: `uv run pytest -q`
- Lint: `uv run ruff check .` (optionally `uv run ruff check --fix .`)
- Format: `uv run ruff format .`

## Frontend companion

The Korfbal web frontend lives at `apps/node_projects/frontend/korfbal-web/`.
Match tracker issues often require coordinated backend + frontend changes.

## Gotchas

- Public club-logo URLs must be stable and versioned by the immutable storage key; expiring private-media tokens in cached catalogue responses cause repeated downloads and broken logos. Keep the public route restricted to the current club logo, never arbitrary media keys.

- For eligibility, use published competition classifications for imported teams;
  `TeamData` can retain the manual B-category/rank-1 defaults. Keep missing player
  ages and official B-youth cutoffs as explicit checks, never inferred permission.

- Account deletion removes the Django user only. Preserve the linked Player, KNKV
  metadata, and sporting relationships through `Player.user`'s `SET_NULL`; do not
  invoke the separate player-profile deletion/archival service from user signals.

- Guest lineup writes must validate player IDs against actual source-group membership
  or the club/date/season picker candidates; frontend filtering is not authorization.

- In Caddy, use an explicit matcher for relative redirects (`redir * /admin/ 301`); otherwise `/admin/` is parsed as a matcher. Verify both `/admin` and `/admin/`, since an unmatched handler can return an empty HTTP 200.

- The worker image supervises isolated `celery,instant`, `projections`, `media`, and
  `competition` pools. Preserve `instant` for shared `bg_auth` MFA/activation tasks;
  verify every pool after changing the image entrypoint or queue routing.
- Persist background intent inside the domain transaction with `kwt_common.services.jobs`.
  Broker publication, cache claims, and task ETA reservations are not durable workflow state.
  Keep recipient delivery separate from MVP publication and retain completed intent keys.

- Bulk roster-link deletions bypass M2M signals. Capture and lock affected TeamData
  rows before deleting provider observations, then reconcile their roster history.

- Register new signal modules in `AppConfig.ready()` with `import_module()` so Ruff
  cannot remove a side-effect-only import as unused.

- Keep app-owned `apps/*/static/` sources tracked; only collected `/static/` output is generated.
  The root ignore also matches nested static directories, so preserve the local exceptions.
  Keep app assets in collectstatic release detection and check custom asset URLs after deployment.

- Don’t commit `.env` files. Use the project’s template and document required vars in PR notes.
- Keep packages imported during Django startup in the base runtime dependencies; Celery and
  collectstatic images must be able to initialize every installed app too.
- Keep Korfbal Python dependencies synchronized in the root workspace `uv.lock` and the production
  `deps/uv.lock`; do not recreate a project-local lock beside `manage.py`.
- WebSocket/live features: prefer minimal changes; add/extend tests when behavior changes.
- API and outbound-provider modules are adapters; application/domain/services/tasks/signals must not import them or HTTP framework modules.
- Keep tracker command metadata in `services/tracker_commands/registry.py`, mutation behavior
  in its family handlers, read snapshots in `services/tracker_state.py`, and the shared
  lock/idempotency/publication envelope in `services/tracker_http.py`.
- Scoring commands report complete logical event IDs directly. Preserve empty deltas
  for reconciled reports and verify optimized IDs against full timeline diffs; clock,
  undo and editor changes can affect existing rows and still need that comparison.
- Keep event-editor DRF serializers input-only. Apply typed event corrections through the
  `game_tracker` event-editor command boundary so validation, the aggregate lock, projections,
  revision recording, and realtime publication commit together.
- When adding tracker event types, update the typed replay registry and deletion/restoration
  lists together, and test replay twice; rebuilding periods cascades into their event rows.
- Keep timeline GET endpoints on the `game_tracker` timeline-read boundary. Build related payloads
  from one consistent read snapshot and never use `select_for_update()` for events, shots, or audit
  history reads; those row locks serialize readers with live tracker writes.
- Keep isolated load-test settings aligned with production cache aliases and socket/pool
  options while redirecting every service endpoint to owned disposable containers.
- Public live caches must contain only revision-stable public fields. Generate timer
  `server_time` per response and bypass shared caching inside caller-owned transactions
  so rolled-back writes cannot populate or consume committed snapshots. Use the
  dedicated public-live cache with short socket timeouts and no retries; optional
  Redis reads inside a snapshot must not hold the database pool for seconds.
- Use the native Django Redis backend for atomic public snapshots, including the
  Prometheus variant (`NativeRedisCache`); its `RedisCache` class wraps django-redis
  and has a different client/serialization API. Reuse clients across ASGI threads.
- Share match SSE broker reception and revision recovery per event loop. Keep viewer
  dispatch database-free, bound pending updates per match, and preserve invalidations
  when coalescing snapshots; revision gaps must refresh all affected client resources.
- Coalesced SSE updates must share encoded replacement frames across slow viewers; never serialize a large frame per mailbox. Preserve affected-resource unions and bound the number of shared variants without losing invalidations. Give post-commit broker publication one reconciliation cycle before broad recovery, without restarting that grace period as revisions advance.
- Keep SSE request reception and bounded mailbox delivery in persistent tasks;
  per-message task creation adds fanout scheduling overhead. Disconnect or parent
  cancellation must cancel a backpressured sender before releasing shared fanout.
- Keep `timer.server_time` out of compact patch dictionaries: clients reconstruct
  it from the connection's ready clock anchor, falling back to frame timestamps
  for older servers; replay timestamps cannot measure current clock offset.
  Bootstrap snapshots must clear covered pending frames before awaiting sends,
  preserving only publications arriving afterward.
- When shared SSE encoding falls back to local state, rotate its dictionary epoch
  and send a reset before further patches; otherwise workers can fork the same
  sequence space. Test Redis compare-and-swap with distinct concurrent updates,
  as identical duplicate writes alone cannot prove lost-update protection.
- Keep shared compact bootstrap mutations behind Redis compare-and-swap too;
  a throttled preparation must never seed the shared epoch locally. Test a cold
  bootstrap becoming available during the preparation throttle, then the next update.
- Before claiming SSE load capacity, verify Granian backlog/backpressure admission
  limits, ready-connection counts, delivery failures and generator event-loop lag;
  successful-request percentiles alone cannot validate an overloaded run.
- Report SSE body bytes separately from ordinary HTTP requests in capacity tests. Record proxy mode and CPU partitions; CPU affinity on one host is not a physically separate generator, and averages hide snapshot/reconnect bursts.
- Pushed timeline deltas must name the previous revision affecting that resource;
  unrelated revisions may be skipped, but missing bases or ordered IDs require HTTP
  recovery. Coalescing must never relabel an older payload with a newer revision.
- Shared public match reads must preserve timeline identity, delta bases, deletions
  and ordering. Keep credentials and filters on the normal authentication/object
  resolution path, and never put private tracker or audit payloads in these caches.
- Published public snapshots must fence committed revisions atomically, persist
  refresh intent with the domain transaction, and cap freshness from read start.
  Keep zero-SQL HTTP coverage alongside rollback, deletion and real Redis
  out-of-order publication tests; cache-only reads must preserve filtered-route checks.
- With Django 6.1 fetch modes, use `FETCH_RAISE` for read querysets whose relations are explicitly
  loaded. For mutable many-to-many endpoints, use `FETCH_PEERS` or re-prefetch after writes because
  Django invalidates the relation cache before DRF renders the response.
- For DRF detail actions that search related entities, disable the parent viewset’s search filter on that action; otherwise `get_object()` applies the related search term to the parent and returns a misleading 404.
- DRF `partial_update()` delegates to `update()`. Keep qualifier refreshes and revision
  publication in one update override so a tournament PATCH does not publish twice.
- Don’t add standalone indexes for `ForeignKey` or `OneToOneField` columns; Django already indexes
  them. Add only composite or specialized indexes that serve a measured query shape.
- Don’t make exception dataclasses frozen. Python context managers attach traceback state while
  unwinding, and frozen exceptions can mask the original domain error with a `TypeError`.
- Document new APIView methods with explicit drf-spectacular request, response and parameter schemas; the full OpenAPI regression test rejects serializer-inference warnings and errors.
- Keep dedicated API routes on JSON adapters, including account activation, resend and
  logout; shared HTML auth views may reverse a login route that Korfbal does not expose.
  Preserve field errors, conflict metadata and protocol headers when normalizing errors.
- Parse UUID query parameters at the API boundary and return a controlled 400; constrain UUID
  detail routes so malformed identifiers become 404s instead of leaking ORM validation errors.
- In tournament planners, check derived match-end, changeover and rest timestamps before
  persisting a schedule. A valid input datetime can still overflow during arithmetic;
  translate that failure into a domain validation error that the API maps to 400.
- Test data migrations with `MigrationExecutor` and the historical app registry. Current model
  classes cannot detect dependency, field-state, or migration-order regressions.
- Add new migration test files to both `test-migrations` commands in `project.json`; the
  general test lane excludes `migration_regression` tests.
- Keep `test` dependent on both test lanes, including their `ci` configurations, so the
  standard validation command always runs migration checks and CI retains coverage enforcement.
- Mark every `MigrationExecutor` test with `migration_regression`; the Nx test target runs those
  against real migrations in an isolated database while ordinary tests use `--nomigrations`.
- Keep the database-free migration graph check in the general test lane and override
  `MIGRATION_MODULES` there so `--nomigrations` cannot hide conflicting shipped migration leaves.
- Size both CI test lanes explicitly and budget their workers together; leaving the migration
  lane sequential can dominate the full target even when the general suite finishes quickly.
- Local migration tests use four workers; the `ci` configuration keeps two alongside the
  general lane's two to fit the four-CPU quality runner. Benchmark both lanes together
  before increasing that CI budget.
- When a test only needs to execute `transaction.on_commit()` callbacks, keep normal
  `django_db` rollback isolation and use `django_capture_on_commit_callbacks(execute=True)`;
  reserve `transaction=True` for real transaction visibility, async/SSE, and migration tests.
- Keep test file storage rooted through `MEDIA_ROOT` rather than a fixed `STORAGES` location so the
  autouse isolation fixture can give every test and xdist worker its own temporary directory.
- Keep values evaluated inside `pytest.mark.parametrize` deterministic; collection-time randomness
  gives xdist workers different node IDs and aborts the parallel suite before tests run.

For Sportlink competition imports, preserve the originating User-Agent in the private
OAuth session file and send `X-Navajo-Instance: KNKV` plus the endpoint-specific
`X-Navajo-Version`; a bearer token and the `v` query parameter alone can return
misleading provider 500/603 errors.

- Historical Sportlink deduplication must preserve the union of discovered date scopes;
  test narrow-then-wide discovery and bulk-before-detail request counts. Use each
  Dataservice endpoint's documented limits, and prove complete coverage separately
  from HTTP success or a deduplicated source ID.

## PR-first workflow (required)

- Branch: `opencode/korfbal-<short-slug>`
- Before PR: run `uv run pytest -q` (and any targeted tests you touched)
- PR body must include:
    - Summary
    - How to test locally (exact commands)
    - Risks/rollout notes

- Publish KNKV people into native `Player` and `TeamData.players` records and reuse
  the existing profile/roster UI. Keep provider identifiers and observation history
  as metadata; do not create parallel player entities or login accounts for imports.

- KNKV allocation CSVs have two independent side-by-side poule columns and lose
  worksheet names. Require explicit file gender/context, preserve section-specific
  four/eight-player formats and the `Midweek zaal` exception, and keep missing
  average ages/points distinct from zero. Never infer age from a J-number.

- Keep KNKV fetch scopes separate from native indoor/outdoor seasons. Resolve all
  publication through the same season bindings, and keep team membership roles
  separate from match starting/substitute assignments.

- Preserve private KNKV roster sizes as anonymous per-feed counts. Do not create
  placeholder Player records or sum anonymous counts across feeds as distinct people.

- KNKV uses the literal `PersonId: "PRIVATE"` for multiple anonymous people in one
  response. Count those rows individually; deduplicate only genuine person IDs.

- Version fitted forecast artifacts with their availability time and season/context
  scope. Never apply learned coefficients to historical matches before that cutoff;
  keep fitting offline and coefficient serving free of database queries. Serve the
  same features used in fitting and evaluation; preseason KNKV points and
  result-updated Elo ratings are not interchangeable inputs.

- Sportlink result feeds also contain unscored scheduled/postponed fixtures. Do not
  treat `result_observed_at` alone as a final result when accepting newer program
  rescheduling; preserve actual scores and finished statuses.

- Sportlink hourly/daily quotas are operator choices, not verified provider limits.
  Keep zero-as-disabled semantics explicit and honor provider Retry-After without
  silently replacing configured quotas after a rate-limit observation.

- Persist exact match membership for Sportlink conditional feed checks; HTTP 200/304
  success must not mark absent matches fresh. Clear legacy collection validators
  when introducing membership tracking, so old ETags cannot certify unknown scope.

- Club result coverage does not confirm official poule standings. Schedule stale
  poule feeds from actual result revisions, including on resumed worker runs;
  preserve provider pacing and skip freshness-only score observations.

- Sportlink feeds can contain fixtures with identical home/away team IDs. Skip those
  rows before identity writes without rejecting valid neighbours or counting skipped
  fixtures as observed coverage; retain validation for other identity conflicts.

- Sportlink `EventTimeResolution=NONE` can still accompany minute-based Duration and
  MatchPeriod.PlayTime. Accept the observed NONE contract only when validated period
  regular-period minutes sum to Duration; cup extra time is optional and the observed
  zero-minute `Strafworpserie` is untimed. Neither proves those phases were played.

- Drain Sportlink metadata from a due-component queue; do not rebuild the match
  polling graph for each component. During catch-up, preserve due result/schedule
  priority and periodically recheck it without scanning the entire metadata backlog.

- When repairing Sportlink imports after a worker release, check separately launched
  backfill containers too. An older importer can keep exhausting shared checkpoints
  even when the scheduled worker already contains the parser fix.

- Catch tournament command errors outside a nested transaction savepoint; winner propagation
  can fail after saving a result, so refresh the match after rollback before returning a conflict.

- Share tournament SSE recovery reads per ASGI event loop, retain a bounded latest-revision
  queue per receiver, and release the shared worker after the last disconnect; viewer
  count must not multiply periodic database scans or let slow receivers block others.

- Tournament SSE subscriptions do not authenticate display tokens. Push snapshot content
  only for publicly visible, published/live/finished tournaments; private displays must
  recover through authorized HTTP reads. Apply patches only against their exact revision.

- Distinguish missing Sportlink feed membership from HTTP failure. Pace absent-result
  retries with an attempt timestamp without advancing confirmed match coverage; retain
  all owner-feed fallbacks and persist safe diagnostics before publication can fail.

- Admin rendering and permission tests must establish `bg_auth_mfa_verified` from the
  authenticated user’s session auth hash; `force_login()` alone now redirects to MFA.

- Dispatch committed durable work immediately; keep periodic scans for recovery and
  future deadlines. Release execution ownership before publishing a successor, and
  keep match-form discovery out of the action-processing path.

- Queue private match-form work from the finalized `MatchLiveChange` revision inside
  the mutation transaction; discovery is recovery. Reconcile after an upload so edits
  during provider I/O get a successor, and ignore statistics-only revision changes.

- Keep goal-song clip lengths consistent in worker preparation, selected-song URLs, and tracker manifests. Song administration must reject an unknown explicit season instead of falling back to the current season.
