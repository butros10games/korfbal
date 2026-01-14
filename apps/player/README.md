# player (Korfbal)

Player domain for the Korfbal backend.

Includes:

- Player profiles + privacy settings
- Player-song/goal-song features (Spotify + spotDL)
- Push notification subscriptions (PWA / Web Push)

## Notes

- API endpoints live under `apps/django_projects/korfbal/apps/player/api/`.
- Background tasks live in `apps/django_projects/korfbal/apps/player/tasks.py`.

Run tests via: `corepack pnpm nx run korfbal-django:test`.
