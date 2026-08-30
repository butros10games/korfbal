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
- Don’t add standalone indexes for `ForeignKey` or `OneToOneField` columns; Django already indexes
  them. Add only composite or specialized indexes that serve a measured query shape.
- Don’t make exception dataclasses frozen. Python context managers attach traceback state while
  unwinding, and frozen exceptions can mask the original domain error with a `TypeError`.
- Parse UUID query parameters at the API boundary and return a controlled 400; constrain UUID
  detail routes so malformed identifiers become 404s instead of leaking ORM validation errors.
- Test data migrations with `MigrationExecutor` and the historical app registry. Current model
  classes cannot detect dependency, field-state, or migration-order regressions.
- Keep test file storage rooted through `MEDIA_ROOT` rather than a fixed `STORAGES` location so the
  autouse isolation fixture can give every test and xdist worker its own temporary directory.
- Keep values evaluated inside `pytest.mark.parametrize` deterministic; collection-time randomness
  gives xdist workers different node IDs and aborts the parallel suite before tests run.

## PR-first workflow (required)

- Branch: `opencode/korfbal-<short-slug>`
- Before PR: run `uv run pytest -q` (and any targeted tests you touched)
- PR body must include:
    - Summary
    - How to test locally (exact commands)
    - Risks/rollout notes
