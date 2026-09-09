# Korfbal (Django) (opencode)

Scope: `apps/django_projects/korfbal/**`

## What this is

Korfbal is a Django app for match tracking (incl. live match tracker), players, teams, and stats.

## Most useful commands

Prefer Nx targets (recommended):

- List projects: `corepack pnpm nx show projects --verbose`
- Run tests: `corepack pnpm nx run korfbal-django:test`
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

- Bulk roster-link deletions bypass M2M signals. Capture and lock affected TeamData
  rows before deleting provider observations, then reconcile their roster history.

- Register new signal modules in `AppConfig.ready()` with `import_module()` so Ruff
  cannot remove a side-effect-only import as unused.

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
- Keep event-editor DRF serializers input-only. Apply typed event corrections through the
  `game_tracker` event-editor command boundary so validation, the aggregate lock, projections,
  revision recording, and realtime publication commit together.
- Keep timeline GET endpoints on the `game_tracker` timeline-read boundary. Build related payloads
  from one consistent read snapshot and never use `select_for_update()` for events, shots, or audit
  history reads; those row locks serialize readers with live tracker writes.
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
- Parse UUID query parameters at the API boundary and return a controlled 400; constrain UUID
  detail routes so malformed identifiers become 404s instead of leaking ORM validation errors.
- Test data migrations with `MigrationExecutor` and the historical app registry. Current model
  classes cannot detect dependency, field-state, or migration-order regressions.
- Add new migration test files to both explicit migration commands in `project.json`; the
  general test lane excludes `migration_regression` tests.
- Mark every `MigrationExecutor` test with `migration_regression`; the Nx test target runs those
  against real migrations in an isolated database while ordinary tests use `--nomigrations`.
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

- Sportlink `EventTimeResolution=NONE` can still accompany minute-based Duration and
  MatchPeriod.PlayTime. Accept the observed NONE contract only when validated period
  minutes sum to Duration; do not confuse event timestamp precision with match length.

- Drain Sportlink metadata from a due-component queue; do not rebuild the match
  polling graph for each component. During catch-up, preserve due result/schedule
  priority and periodically recheck it without scanning the entire metadata backlog.
