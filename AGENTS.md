# Korfbal (Django) (opencode)

Scope: `apps/django_projects/korfbal/**`

## What this is

Korfbal is a Django app for match tracking (incl. live match tracker), players, teams, and stats.

## Most useful commands

Prefer Nx targets (recommended):

- List projects: `npm run nx -- show projects --verbose`
- Run tests: `npm run nx -- run korfbal-django:test`
- Run lint: `npm run nx -- run korfbal-django:lint`

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
- WebSocket/live features: prefer minimal changes; add/extend tests when behavior changes.

## PR-first workflow (required)

- Branch: `opencode/korfbal-<short-slug>`
- Before PR: run `uv run pytest -q` (and any targeted tests you touched)
- PR body must include:
    - Summary
    - How to test locally (exact commands)
    - Risks/rollout notes
