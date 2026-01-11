"""Django settings entrypoint for the korfbal project.

Historically this repository used a single, large `settings.py`. It now imports
smaller modules so individual concerns (services, security, storage, etc.) are
 easier to maintain.

The public entrypoint remains `DJANGO_SETTINGS_MODULE=korfbal.settings`.
"""

from __future__ import annotations

# bg_auth settings
from .bg_auth import *  # noqa: F403

# Core Django configuration
from .django_core import (  # noqa: F401
    ASGI_APPLICATION,
    AUTH_PASSWORD_VALIDATORS,
    AUTHENTICATION_BACKENDS,
    DEFAULT_AUTO_FIELD,
    INSTALLED_APPS,
    LOGIN_URL,
    MIDDLEWARE,
    ROOT_URLCONF,
    SESSION_ENGINE,
    TEMPLATES,
    WSGI_APPLICATION,
)

# Email
from .email import *  # noqa: F403

# i18n
from .i18n import *  # noqa: F403

# Integrations
from .integrations import *  # noqa: F403

# App performance switches
from .performance import (  # noqa: F401
    KORFBAL_ENABLE_IMPACT_AUTO_RECOMPUTE,
    KORFBAL_IMPACT_AUTO_RECOMPUTE_LIMIT,
    KORFBAL_LOG_SLOW_DB_QUERIES,
    KORFBAL_LOG_SLOW_REQUESTS,
    KORFBAL_SLOW_DB_INCLUDE_SQL,
    KORFBAL_SLOW_DB_QUERY_MS,
    KORFBAL_SLOW_REQUEST_BUFFER_SIZE,
    KORFBAL_SLOW_REQUEST_BUFFER_TTL_S,
    KORFBAL_SLOW_REQUEST_MS,
    SPOTDL_DOWNLOAD_TIMEOUT_SECONDS,
    SPOTDL_STALE_IN_PROGRESS_SECONDS,
)

# REST / schema
from .rest import *  # noqa: F403

# Runtime flags (DEBUG, SECRET_KEY, etc.)
from .runtime import *  # noqa: F403

# Security (hosts/CORS/CSRF + web app origin)
from .security import (  # noqa: F401
    ALLOWED_HOSTS,
    CORS_ALLOW_CREDENTIALS,
    CORS_ALLOWED_ORIGINS,
    CSRF_COOKIE_SECURE,
    CSRF_TRUSTED_ORIGINS,
    KORFBAL_ORIGIN,
    KWT_ORIGIN,
    SECURE_HSTS_INCLUDE_SUBDOMAINS,
    SECURE_HSTS_PRELOAD,
    SECURE_HSTS_SECONDS,
    SECURE_PROXY_SSL_HEADER,
    SECURE_SSL_REDIRECT,
    SESSION_COOKIE_SECURE,
    WEB_APP_ORIGIN,
    WEB_KORFBAL_ORIGIN,
    WEB_KWT_ORIGIN,
    X_FRAME_OPTIONS,
)

# Services
from .services import (  # noqa: F401
    CACHES,
    CELERY_ACCEPT_CONTENT,
    CELERY_BROKER_URL,
    CELERY_RESULT_BACKEND,
    CELERY_TASK_ALWAYS_EAGER,
    CELERY_TASK_SERIALIZER,
    CELERY_TASK_TRACK_STARTED,
    CELERY_TIMEZONE,
    CHANNEL_LAYERS,
    DATABASES,
    VALKEY_HOST,
    VALKEY_PORT,
)

# Storage
from .storage import *  # noqa: F403
