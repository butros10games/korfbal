"""External service configuration (DB, cache, Channels, Celery)."""

from __future__ import annotations

from .env import env, env_bool, env_int
from .runtime import KORFBAL_ENABLE_PROMETHEUS, RUNNING_TESTS


VALKEY_HOST = env("VALKEY_HOST", "127.0.0.1")
VALKEY_PORT = env_int("VALKEY_PORT", 6379)

CHANNEL_LAYERS = {
    "default": {
        "BACKEND": "channels_redis.core.RedisChannelLayer",
        "CONFIG": {
            "hosts": [(VALKEY_HOST, VALKEY_PORT)],
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
CELERY_TASK_SERIALIZER = "json"
CELERY_TIMEZONE = env("CELERY_TIMEZONE", "UTC")
CELERY_TASK_TRACK_STARTED = env_bool("CELERY_TASK_TRACK_STARTED", True)
CELERY_TASK_ALWAYS_EAGER = env_bool("CELERY_TASK_ALWAYS_EAGER", False)
