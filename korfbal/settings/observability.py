"""Logging, access logs and Sentry error/performance reporting."""

from __future__ import annotations

from korfbal.observability import SentryOptions, init_sentry, logging_config

from .env import env, env_bool
from .runtime import DEBUG, DJANGO_ENV, RUNNING_TESTS


def _rate(name: str, default: str) -> float:
    return min(1.0, max(0.0, float(env(name, default) or default)))


# JSON lines for the log collector; readable text while developing locally.
KORFBAL_LOG_FORMAT = env("KORFBAL_LOG_FORMAT", "text" if DEBUG else "json").lower()
KORFBAL_LOG_LEVEL = env("KORFBAL_LOG_LEVEL", "INFO").upper()
# One structured line per API request (route pattern, never the raw path).
KORFBAL_ACCESS_LOG = env_bool("KORFBAL_ACCESS_LOG", not RUNNING_TESTS)

LOGGING = logging_config(log_format=KORFBAL_LOG_FORMAT, level=KORFBAL_LOG_LEVEL)

# Blank DSN keeps reporting off (local development, tests, self-hosters).
SENTRY_DSN = "" if RUNNING_TESTS else env("SENTRY_DSN", "")
SENTRY_ENVIRONMENT = env("SENTRY_ENVIRONMENT", DJANGO_ENV)
# Baked into release images as the 12-character commit; matches frontend releases.
SENTRY_RELEASE = env("SENTRY_RELEASE", env("KORFBAL_RELEASE", ""))
SENTRY_TRACES_SAMPLE_RATE = _rate("SENTRY_TRACES_SAMPLE_RATE", "0.1")
SENTRY_PROFILES_SAMPLE_RATE = _rate("SENTRY_PROFILES_SAMPLE_RATE", "0")
# Each beat entry becomes a cron monitor; check the plan's monitor quota first.
SENTRY_MONITOR_BEAT_TASKS = env_bool("SENTRY_MONITOR_BEAT_TASKS", False)

SENTRY_ENABLED = init_sentry(
    SentryOptions(
        dsn=SENTRY_DSN,
        environment=SENTRY_ENVIRONMENT,
        release=SENTRY_RELEASE,
        traces_sample_rate=SENTRY_TRACES_SAMPLE_RATE,
        profiles_sample_rate=SENTRY_PROFILES_SAMPLE_RATE,
        monitor_beat_tasks=SENTRY_MONITOR_BEAT_TASKS,
    )
)
