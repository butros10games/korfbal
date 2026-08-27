# Korfbal Django: domains & app structure

This document captures the intended Django app boundaries for the Korfbal project, and how we expect code to be organized as features grow.

## Goals

- Keep business concepts easy to find (domain-driven-ish structure).
- Avoid “god apps” and circular imports.
- Make it safe to evolve features (MVP, notifications, badges) without rewriting the whole project.
- Keep API routes stable for the frontend.

## Current domain map (intended)

### Core domains

- `apps.club` — clubs, branding, club admins.
- `apps.team` — teams that belong to clubs; season/team roster via `TeamData`.
- `apps.player` — player profile, privacy, follows, membership history, goal-song + Spotify integration.
- `apps.schedule` — seasons + matches (fixtures). API owns `/api/matches/…` endpoints.
- `apps.game_tracker` — live match operations/events, match data/status, shots, minutes, impact computation.

### Orchestration

- `apps.hub` — cross-domain “dashboard/feed” endpoints. Should remain thin and call domain services/selectors.

### Platform / operational

- `apps.kwt_common` — middleware and operational/debug endpoints (slow requests, slow queries, timing headers).

## Recommended final structure (near-term)

Keep the existing app split (it’s a good size), with one change:

- **Move MVP voting into a dedicated app** so it can grow into notifications and badges cleanly.

### New app: `apps.awards`

Owns:

- MVP voting (models + services)
- Future: badges/achievements
- Future: notification generation related to awards (MVP/badges)

Depends on:

- `apps.schedule` (for `Match`)
- `apps.player` (for `Player`)
- `apps.game_tracker` (for match events needed to determine candidates and finish times)

Does **not** need to be depended on by core domain apps.

## API routing guidance

We intentionally keep MVP as a **sub-resource of a match**.

- Keep routes under `/api/matches/<match_id>/mvp` and `/api/matches/<match_id>/mvp/vote` (stable for the SPA).
- The `apps.schedule.api.views.MatchViewSet` can continue to host these endpoints but should delegate to `apps.awards.services.*`.

If/when we add global award endpoints, add them under `/api/awards/…` and include them from `korfbal/api_urls.py`.

## Code organization rules (DX)

### 1) Prefer services/selectors over cross-app model imports

- Domain apps should expose “read models” and “write operations” via `services/`.
- Orchestration (hub) should import services/selectors, not raw models from many apps.

### 2) Dependency direction

- Core apps (`club`, `team`, `player`, `schedule`) should stay as pure as practical.
- Derived domains (`game_tracker`, `awards`) may depend on core apps.
- Orchestration (`hub`) may depend on everything but should be thin.
- Platform (`kwt_common`) should be dependency-light and reusable.

### 3) Keep REST-ish ownership by route

- Match-centric endpoints belong under schedule’s match routes.
- Telemetry/tracker endpoints belong under match routes but implemented in `game_tracker.services`.
- Award endpoints belong under match routes or `/api/awards` depending on resource shape.

## MVP ownership

MVP models and services live in `apps.awards`. The existing database tables keep
their historical names:

- `schedule_matchmvp`
- `schedule_matchmvpvote`

The awards models declare these names explicitly, so no runtime compatibility
modules are needed under `apps.schedule`.

## When to split further

Only consider creating more apps if:

- a subdomain grows large enough to have its own lifecycle (e.g. “notifications” used across multiple domains), or
- multiple projects in the monorepo need the same feature (then prefer `libs/django_packages/…`).
