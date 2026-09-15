"""Production application settings with explicitly isolated service destinations."""

from importlib import import_module
import os
from typing import Any


if os.environ.get("KORFBAL_LOADTEST") != "isolated":
    raise RuntimeError("Start this settings module through python -m loadtest.")

application_settings = import_module("korfbal.settings")
globals().update({
    name: getattr(application_settings, name)
    for name in dir(application_settings)
    if name.isupper()
})


DEBUG = False
SECRET_KEY = os.environ["KORFBAL_LOADTEST_SECRET"]
DATABASES: dict[str, dict[str, Any]] = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": "korfbal_loadtest",
        "USER": "postgres",
        "PASSWORD": "",
        "HOST": "127.0.0.1",
        "PORT": os.environ["KORFBAL_LOADTEST_DB_PORT"],
    }
}
DATABASES["default"]["OPTIONS"] = dict(
    application_settings.DATABASES["default"].get("OPTIONS", {})
)
_valkey_port = int(os.environ["KORFBAL_LOADTEST_VALKEY_PORT"])
CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.redis.RedisCache",
        "LOCATION": f"redis://127.0.0.1:{_valkey_port}/1",
    }
}
CACHES["public_live"] = {
    **application_settings.CACHES["public_live"],
    "LOCATION": CACHES["default"]["LOCATION"],
}
CHANNEL_LAYERS = {
    "default": {
        "BACKEND": "channels_redis.core.RedisChannelLayer",
        "CONFIG": {
            "hosts": [
                {
                    "host": "127.0.0.1",
                    "port": _valkey_port,
                    "socket_timeout": None,
                    "socket_connect_timeout": 5,
                }
            ],
            "capacity": 1500,
            "expiry": 10,
        },
    }
}
CELERY_BROKER_URL = f"redis://127.0.0.1:{_valkey_port}/0"
CELERY_RESULT_BACKEND = CELERY_BROKER_URL
CELERY_TASK_ALWAYS_EAGER = False
KORFBAL_SSE_ENABLED = True
KORFBAL_ENABLE_IMPACT_AUTO_RECOMPUTE = False
KORFBAL_IMPACT_AUTO_RECOMPUTE_LIMIT = 0
KORFBAL_LOG_SLOW_REQUESTS = False
KORFBAL_LOG_SLOW_DB_QUERIES = False
SPORTLINK_SYNC_ENABLED = False
SPORTLINK_SYNC_SESSION_FILE = ""
ALLOWED_HOSTS = ["127.0.0.1", "localhost", "testserver"]
SECURE_SSL_REDIRECT = False
SESSION_COOKIE_SECURE = False
CSRF_COOKIE_SECURE = False
SESSION_COOKIE_DOMAIN = None
CSRF_COOKIE_DOMAIN = None
SESSION_COOKIE_NAME = "sessionid"
CSRF_COOKIE_NAME = "csrftoken"
CSRF_TRUSTED_ORIGINS = [os.environ["KORFBAL_LOADTEST_ORIGIN"]]
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.InMemoryStorage"},
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
}
MAILERS = {"default": {"BACKEND": "django.core.mail.backends.locmem.EmailBackend"}}
