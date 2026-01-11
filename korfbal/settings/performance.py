"""App-specific performance and operational tuning flags."""

from __future__ import annotations

from .env import env_bool, env_int
from .runtime import DEBUG, RUNNING_TESTS


# --- Performance tuning (Korfbal) ---
# Keep API responses predictable by default. Expensive self-healing recomputes
# can be enabled temporarily when needed.
KORFBAL_ENABLE_IMPACT_AUTO_RECOMPUTE = env_bool(
    "KORFBAL_ENABLE_IMPACT_AUTO_RECOMPUTE",
    DEBUG or RUNNING_TESTS,
)

_default_impact_recompute_limit = 0
if RUNNING_TESTS:
    _default_impact_recompute_limit = 3
elif DEBUG:
    _default_impact_recompute_limit = 1

KORFBAL_IMPACT_AUTO_RECOMPUTE_LIMIT = env_int(
    "KORFBAL_IMPACT_AUTO_RECOMPUTE_LIMIT",
    _default_impact_recompute_limit,
)

# Slow SQL logging (opt-in). Useful to spot missing indexes / N+1 patterns.
KORFBAL_LOG_SLOW_DB_QUERIES = env_bool("KORFBAL_LOG_SLOW_DB_QUERIES", False)
KORFBAL_SLOW_DB_QUERY_MS = env_int("KORFBAL_SLOW_DB_QUERY_MS", 200)
KORFBAL_SLOW_DB_INCLUDE_SQL = env_bool("KORFBAL_SLOW_DB_INCLUDE_SQL", False)

# Slow request surfacing (opt-in). Adds timing headers and keeps a rolling
# buffer (in cache) of the slowest requests so you don't have to tail logs.
KORFBAL_LOG_SLOW_REQUESTS = env_bool("KORFBAL_LOG_SLOW_REQUESTS", False)
KORFBAL_SLOW_REQUEST_MS = env_int("KORFBAL_SLOW_REQUEST_MS", 500)
KORFBAL_SLOW_REQUEST_BUFFER_SIZE = env_int("KORFBAL_SLOW_REQUEST_BUFFER_SIZE", 200)
KORFBAL_SLOW_REQUEST_BUFFER_TTL_S = env_int(
    "KORFBAL_SLOW_REQUEST_BUFFER_TTL_S",
    60 * 60 * 24,
)

# --- spotDL (goal song downloads) ---
# Some downloads can take longer due to upstream rate limiting / search issues.
# Keep this configurable per environment.
SPOTDL_DOWNLOAD_TIMEOUT_SECONDS = env_int(
    "SPOTDL_DOWNLOAD_TIMEOUT_SECONDS",
    60 * 15,
)

# If a download gets stuck (worker restart/crash), the CachedSong can remain in
# DOWNLOADING/UPLOADING forever. The Celery task will reclaim the work once the
# record hasn't been updated for this long.
#
# Default: timeout + 60 seconds.
SPOTDL_STALE_IN_PROGRESS_SECONDS = env_int(
    "SPOTDL_STALE_IN_PROGRESS_SECONDS",
    SPOTDL_DOWNLOAD_TIMEOUT_SECONDS + 60,
)
