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

# Optional Netscape-format session file mounted read-only in the media worker.
# Downloader jobs use private temporary copies because yt-dlp rewrites cookie jars.
YOUTUBE_COOKIES_FILE = env("YOUTUBE_COOKIES_FILE", "")

# --- Web push notifications (PWA) ---
# The frontend subscribes using the *public* VAPID key.
# The backend sends notifications using `pywebpush` with the private key.
WEBPUSH_VAPID_PUBLIC_KEY = env("WEBPUSH_VAPID_PUBLIC_KEY", "")
WEBPUSH_VAPID_PRIVATE_KEY = env("WEBPUSH_VAPID_PRIVATE_KEY", "")
# Subject must be a contact URI (commonly a mailto: address).
WEBPUSH_VAPID_SUBJECT = env("WEBPUSH_VAPID_SUBJECT", "mailto:butrosgroot@gmail.com")
WEBPUSH_TTL_SECONDS = env_int("WEBPUSH_TTL_SECONDS", 60 * 60)

# --- iOS Live Activities (Dynamic Island) ---
# ActivityKit pushes bypass Expo's push service and go straight to APNs with an
# ES256 provider token. Create the key under Apple Developer > Keys (APNs) and
# paste the .p8 contents (newlines may be escaped as \n).
APNS_TEAM_ID = env("APNS_TEAM_ID", "")
APNS_KEY_ID = env("APNS_KEY_ID", "")
APNS_PRIVATE_KEY = env("APNS_PRIVATE_KEY", "")
APNS_BUNDLE_ID = env("APNS_BUNDLE_ID", "korfbal.butrosgroot.com")
# Development builds signed with a development profile use the sandbox host.
APNS_USE_SANDBOX = env("APNS_USE_SANDBOX", "false").lower() == "true"

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
# Also route independent outdoor autumn/spring poules to their own playing
# seasons (continuous outdoor competitions stay in the annual scope). Requires
# SPORTLINK_SPLIT_SEASONS; existing scopes are converted with the reviewed
# repair_competition_context command.
SPORTLINK_SPLIT_OUTDOOR_PHASES = (
    env("SPORTLINK_SPLIT_OUTDOOR_PHASES", "false").lower() == "true"
)

SPORTLINK_IMPORT_LINEUPS = env("SPORTLINK_IMPORT_LINEUPS", "false").lower() == "true"

# Local scheduler heartbeat; provider traffic still follows per-feed deadlines.
SPORTLINK_SYNC_ENABLED = env("SPORTLINK_SYNC_ENABLED", "false").lower() == "true"
SPORTLINK_SYNC_SEASON = env("SPORTLINK_SYNC_SEASON", "")
SPORTLINK_SYNC_SESSION_FILE = env("SPORTLINK_SYNC_SESSION_FILE", "")
SPORTLINK_SYNC_MAX_REQUESTS = max(
    0, min(10000, int(env("SPORTLINK_SYNC_MAX_REQUESTS", "0")))
)

# Season-scoped history imports run in the current sync's idle time and reuse
# its app session. Each heartbeat makes at most this many provider requests.
SPORTLINK_HISTORY_MAX_REQUESTS = max(
    1, min(1000, int(env("SPORTLINK_HISTORY_MAX_REQUESTS", "300")))
)
# "manager": a long-running supervised loop sends every provider request and a
# separate pool publishes in parallel. "unified": the same turns as Celery beat
# tasks with publication inside each turn. "legacy": the separate live sync and
# history tasks. The worker supervisor reads the same variable.
SPORTLINK_SCHEDULER = env("SPORTLINK_SCHEDULER", "manager").strip().lower()
# History's share of requests that are not time-critical live work (0 to 1).
SPORTLINK_HISTORY_SHARE = min(
    1.0, max(0.0, float(env("SPORTLINK_HISTORY_SHARE", "0.5")))
)

# Seconds between history requests; history shares the app account with the live
# sync, so it stays paced rather than bursting thousands of requests.
SPORTLINK_HISTORY_REQUEST_SPACING = max(
    0, int(env("SPORTLINK_HISTORY_REQUEST_SPACING", "1"))
)

# Opt-in enrichment lane: player photos and club metadata queued outside the
# active live season, inside the provider turn. Zero requests per turn keeps it
# closed; enable only with a reviewed preview_historical_photos and a pilot cap.
SPORTLINK_ENRICHMENT_MAX_REQUESTS = max(
    0, min(1000, int(env("SPORTLINK_ENRICHMENT_MAX_REQUESTS", "0")))
)
# Durable rolling-day cap on the lane's own requests; zero leaves only the
# provider-wide hourly/daily limits.
SPORTLINK_ENRICHMENT_DAILY_LIMIT = max(
    0, int(env("SPORTLINK_ENRICHMENT_DAILY_LIMIT", "0"))
)
# The lane's weight among routine requests, relative to live (1 - history share)
# and history (history share).
SPORTLINK_ENRICHMENT_SHARE = min(
    1.0, max(0.0, float(env("SPORTLINK_ENRICHMENT_SHARE", "0.25")))
)
# Comma-separated resource kinds the lane may request.
SPORTLINK_ENRICHMENT_KINDS = [
    kind.strip()
    for kind in env(
        "SPORTLINK_ENRICHMENT_KINDS",
        "player_photo,club_logo,club_contact,club_sports,club_details",
    ).split(",")
    if kind.strip()
]

# Finished editions the app serves no (or partial) results for. A weekly task
# reads a few of their empty checkpoints again and imports the edition once the
# provider serves it; blank disables the recheck.
SPORTLINK_HISTORY_RECHECK_EDITIONS = [
    int(edition)
    for edition in env("SPORTLINK_HISTORY_RECHECK_EDITIONS", "").split(",")
    if edition.strip()
]

# Existing published pools receive context only after the bounded repair preview
# has been reviewed. Zero keeps the publication backlog disabled.
SPORTLINK_CONTEXT_BACKLOG_LIMIT = max(
    0, min(5000, int(env("SPORTLINK_CONTEXT_BACKLOG_LIMIT", "0")))
)

# Bound each worker turn; the shared lease skips overlapping heartbeats.
SPORTLINK_SYNC_MAX_SECONDS = max(
    1, min(240, int(env("SPORTLINK_SYNC_MAX_SECONDS", "240")))
)

# Immutable, approved offline artifact; empty retains the existing predictor.
KORFBAL_SCORE_FORECAST_ARTIFACT = env("KORFBAL_SCORE_FORECAST_ARTIFACT", "")
