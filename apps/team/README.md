# team (Korfbal)

Team domain for the Korfbal backend.

Includes:

- Team metadata + roster
- Team stats and computed “impact”/performance metrics

## Notes

- API endpoints live under `apps/django_projects/korfbal/apps/team/api/`.
- This app contains some of the heaviest stats queries; use the slow-query/slow-request
  toggles in `apps/django_projects/korfbal/korfbal/settings.py` when profiling.

Run tests via: `corepack pnpm nx run korfbal-django:test`.
