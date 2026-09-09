"""Third-party integrations (Spotify, webpush, Prometheus)."""

from __future__ import annotations

from .env import env, env_int
from .security import CSRF_TRUSTED_ORIGINS


spotify_origin = (
    CSRF_TRUSTED_ORIGINS[0] if CSRF_TRUSTED_ORIGINS else "https://localhost"
)

SPOTIFY_CLIENT_ID = env("SPOTIFY_CLIENT_ID", "")
SPOTIFY_CLIENT_SECRET = env("SPOTIFY_CLIENT_SECRET", "")
SPOTIFY_REDIRECT_URI = env(
    "SPOTIFY_REDIRECT_URI",
    f"{spotify_origin.rstrip('/')}/api/player/spotify/callback/",
)

# --- Web push notifications (PWA) ---
# The frontend subscribes using the *public* VAPID key.
# The backend sends notifications using `pywebpush` with the private key.
WEBPUSH_VAPID_PUBLIC_KEY = env("WEBPUSH_VAPID_PUBLIC_KEY", "")
WEBPUSH_VAPID_PRIVATE_KEY = env("WEBPUSH_VAPID_PRIVATE_KEY", "")
# Subject must be a contact URI (commonly a mailto: address).
WEBPUSH_VAPID_SUBJECT = env("WEBPUSH_VAPID_SUBJECT", "mailto:butrosgroot@gmail.com")
WEBPUSH_TTL_SECONDS = env_int("WEBPUSH_TTL_SECONDS", 60 * 60)

PROMETHEUS_LATENCY_BUCKETS = (
    0.1,
    0.2,
    0.5,
    0.6,
    0.8,
    1.0,
    2.0,
    3.0,
    4.0,
    5.0,
    6.0,
    7.5,
    9.0,
    12.0,
    15.0,
    20.0,
    30.0,
    float("inf"),
)
PROMETHEUS_METRIC_NAMESPACE = "kwt"


# Historical and current competition imports share one durable provider budget.
SPORTLINK_HOURLY_LIMIT = max(0, int(env("SPORTLINK_HOURLY_LIMIT", "0")))
SPORTLINK_DAILY_LIMIT = max(0, int(env("SPORTLINK_DAILY_LIMIT", "0")))
SPORTLINK_REQUEST_SPACING = max(1, int(env("SPORTLINK_REQUEST_SPACING", "5")))
# Blank disables the override; zero removes artificial delay during backfill.
_backfill_spacing = env("SPORTLINK_BACKFILL_REQUEST_SPACING", "").strip()
SPORTLINK_BACKFILL_REQUEST_SPACING = (
    max(0, int(_backfill_spacing)) if _backfill_spacing else None
)

SPORTLINK_IMPORT_ROSTERS = env("SPORTLINK_IMPORT_ROSTERS", "false").lower() == "true"

# Enable after repairing existing publication links; new import scopes bind once.
SPORTLINK_SPLIT_SEASONS = env("SPORTLINK_SPLIT_SEASONS", "false").lower() == "true"

SPORTLINK_IMPORT_LINEUPS = env("SPORTLINK_IMPORT_LINEUPS", "false").lower() == "true"

# Local scheduler heartbeat; provider traffic still follows per-feed deadlines.
SPORTLINK_SYNC_ENABLED = env("SPORTLINK_SYNC_ENABLED", "false").lower() == "true"
SPORTLINK_SYNC_SEASON = env("SPORTLINK_SYNC_SEASON", "")
SPORTLINK_SYNC_SESSION_FILE = env("SPORTLINK_SYNC_SESSION_FILE", "")
SPORTLINK_SYNC_MAX_REQUESTS = max(
    0, min(10000, int(env("SPORTLINK_SYNC_MAX_REQUESTS", "0")))
)

# Bound each worker turn; the shared lease skips overlapping heartbeats.
SPORTLINK_SYNC_MAX_SECONDS = max(
    1, min(240, int(env("SPORTLINK_SYNC_MAX_SECONDS", "240")))
)
