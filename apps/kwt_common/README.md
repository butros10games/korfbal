# kwt_common (Korfbal)

Shared utilities for the Korfbal Django backend.

This app holds cross-cutting concerns that are used by multiple apps in
`apps/django_projects/korfbal/apps/`, such as:

- Middleware (request timing, slow query logging)
- Context processors and shared helpers

## Notes

- This repository uses `uv` + `pytest` + `ruff`.
- Run tests via the project target: `npm run nx -- run korfbal-django:test`.
