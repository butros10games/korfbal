"""External service configuration (DB, cache, Channels, Celery)."""

from __future__ import annotations

from kombu import Queue

from .env import env, env_bool, env_int
from .runtime import KORFBAL_ENABLE_PROMETHEUS, RUNNING_TESTS


VALKEY_HOST = env("VALKEY_HOST", "127.0.0.1")
VALKEY_PORT = env_int("VALKEY_PORT", 6379)
KORFBAL_SSE_ENABLED = env_bool("KORFBAL_SSE_ENABLED", False)
KORFBAL_SSE_HEARTBEAT_SECONDS = env_int("KORFBAL_SSE_HEARTBEAT_SECONDS", 15)
KORFBAL_SSE_RECONCILE_SECONDS = env_int("KORFBAL_SSE_RECONCILE_SECONDS", 1)
KORFBAL_SSE_MAX_MATCHES = env_int("KORFBAL_SSE_MAX_MATCHES", 25)

CHANNEL_LAYERS = {
    "default": {
        "BACKEND": "channels_redis.core.RedisChannelLayer",
        "CONFIG": {
            "hosts": [
                {
                    "host": VALKEY_HOST,
                    "port": VALKEY_PORT,
                    # Channels blocks for 5s; Redis 8's default read timeout races it.
                    "socket_timeout": None,
                    "socket_connect_timeout": 5,
                }
            ],
            "capacity": 1500,
            "expiry": 10,
        },
    },
}

if RUNNING_TESTS:
    CHANNEL_LAYERS = {"default": {"BACKEND": "channels.layers.InMemoryChannelLayer"}}

db_engine = (
    "django_prometheus.db.backends.postgresql"
    if KORFBAL_ENABLE_PROMETHEUS
    else "django.db.backends.postgresql"
)

DATABASES = {
    "default": {
        "ENGINE": db_engine,
        "NAME": env("POSTGRES_DB", "korfbal"),
        "USER": env("POSTGRES_USER", "postgres"),
        "PASSWORD": env("POSTGRES_PASSWORD", "postgres"),
        "HOST": env("POSTGRES_HOST", "127.0.0.1"),
        "PORT": env("POSTGRES_PORT", "5432"),
    },
}

cache_backend = (
    "django_prometheus.cache.backends.redis.RedisCache"
    if KORFBAL_ENABLE_PROMETHEUS
    else "django.core.cache.backends.redis.RedisCache"
)

CACHES = {
    "default": {
        "BACKEND": cache_backend,
        "LOCATION": f"redis://{VALKEY_HOST}:{VALKEY_PORT}/1",
    },
}

CELERY_BROKER_URL = (
    f"redis://{env('CELERY_BROKER_HOST', VALKEY_HOST)}:"
    f"{env('CELERY_BROKER_PORT', str(VALKEY_PORT))}/0"
)
CELERY_RESULT_BACKEND = (
    f"redis://{env('CELERY_RESULT_HOST', VALKEY_HOST)}:"
    f"{env('CELERY_RESULT_PORT', str(VALKEY_PORT))}/0"
)
CELERY_ACCEPT_CONTENT = ["json"]
# Shared authentication tasks explicitly publish to instant. Workers must consume
# it as well as the default queue used by competition and media jobs.
CELERY_TASK_QUEUES = tuple(
    Queue(name) for name in ("celery", "instant", "projections", "media", "competition")
)
CELERY_WORKER_PREFETCH_MULTIPLIER = 1
CELERY_WORKER_MAX_TASKS_PER_CHILD = 100
CELERY_TASK_ROUTES = {
    "apps.competition.tasks.*": {"queue": "competition"},
    "apps.player.tasks.download_*": {"queue": "media"},
    "apps.game_tracker.tasks.*": {"queue": "projections"},
}
CELERY_TASK_SERIALIZER = "json"
CELERY_TIMEZONE = env("CELERY_TIMEZONE", "UTC")
CELERY_TASK_TRACK_STARTED = env_bool("CELERY_TASK_TRACK_STARTED", True)
CELERY_TASK_ALWAYS_EAGER = env_bool("CELERY_TASK_ALWAYS_EAGER", False)
