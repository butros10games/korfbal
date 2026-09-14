# player (Korfbal)

Player domain for the Korfbal backend.

Includes:

- Player profiles + privacy settings
- Player-song/goal-song features (YouTube or Spotify links via yt-dlp, plus audio uploads)
- Push notification subscriptions (PWA / Web Push)

## Notes

- API endpoints live under `apps/django_projects/korfbal/apps/player/api/`.
- Background tasks live in `apps/django_projects/korfbal/apps/player/tasks.py`.
- Import with `POST /api/player/me/songs/` and `source_url`; `spotify_url` remains a
  legacy input/output alias backed by the existing database column. Submit exactly
  one link or `audio_file`. No Spotify connection is needed for YouTube imports.
- YouTube watch, share, Shorts, Music and embed links resolve to one cached video.
  Link timestamps initialize each player's start time; repeat imports preserve saved
  settings. Imports accept public, non-live videos up to 15 minutes and 25 MB.
- The media worker downloads MP3 audio and metadata, then queues existing goal clips.
  Restricted/unavailable videos fail with a retryable message; MP3 upload remains available.

Run tests via: `corepack pnpm nx run korfbal-django:test`.
