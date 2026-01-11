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
