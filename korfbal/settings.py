"""Django settings for korfbal project."""

from __future__ import annotations

import os
from pathlib import Path
import sys
from urllib.parse import urlparse

from dotenv import load_dotenv


BASE_DIR = Path(__file__).resolve().parent.parent
PROJECT_DIR = BASE_DIR / "korfbal"
env_file = BASE_DIR / ".env"
if env_file.exists():
    load_dotenv(env_file)


def _env(name: str, default: str | None = None, *, required: bool = False) -> str:
    value = os.getenv(name, default)
    if required and value in {None, ""}:
        raise RuntimeError(f"Environment variable '{name}' is required")
    return "" if value is None else value


def _env_bool(name: str, default: bool = False) -> bool:
    return os.getenv(name, str(default)).lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    return int(raw) if raw else default


def _env_list(name: str, default: str = "", sep: str = ",") -> list[str]:
    raw = os.getenv(name, default) or ""
    return [part.strip() for part in raw.split(sep) if part.strip()]


def _sorted_hosts(values: list[str]) -> list[str]:
    return sorted({host for host in values if host})


DJANGO_ENV = _env("DJANGO_ENV", "development").lower()
DEBUG = _env_bool("DEBUG", DJANGO_ENV != "production")
SECRET_KEY = _env("SECRET_KEY", "change-me" if DEBUG else None, required=not DEBUG)

KORFBAL_ORIGIN = "https://api.korfbal.butrosgroot.com"
WEB_KORFBAL_ORIGIN = "https://korfbal.butrosgroot.com"
KWT_ORIGIN = "https://kwt.localhost"
WEB_KWT_ORIGIN = "https://web.kwt.localhost"


def _origin_variants(origin: str) -> list[str]:
    """Return common host variants for a given origin.

    This project has historically been served from multiple hostnames, e.g.
    `korfbal.<domain>` and `web.<domain>`. If the SPA is reached via an
    alternate hostname (www/web), the browser will enforce CORS and block API
    calls unless that origin is explicitly allowed.

    We keep this conservative and only add predictable, same-domain variants.
    """
    origin = (origin or "").strip().rstrip("/")
    if not origin:
        return []

    parsed = urlparse(origin)
    scheme = parsed.scheme or "https"
    netloc = parsed.netloc or parsed.path
    if not netloc:
        return [origin]

    base = netloc
    for prefix in ("www.", "web."):
        if base.startswith(prefix):
            base = base[len(prefix) :]
            break

    variants = {
        f"{scheme}://{base}",
        f"{scheme}://www.{base}",
        f"{scheme}://web.{base}",
        origin,
    }

    # Keep deterministic ordering for config readability/debuggability.
    return sorted(v for v in variants if v)


# Base URL for the SPA frontend (used for redirects back into the UI).
# - Production: https://korfbal.butrosgroot.com
# - Dev: https://web.kwt.localhost (matches this repo's local HTTPS setup)
WEB_APP_ORIGIN = _env(
    "WEB_APP_ORIGIN",
    WEB_KWT_ORIGIN if DEBUG else WEB_KORFBAL_ORIGIN,
).rstrip("/")

default_hosts = "korfbal.butrosgroot.com,api.korfbal.butrosgroot.com"
ALLOWED_HOSTS = _sorted_hosts(_env_list("ALLOWED_HOSTS", default_hosts))

_default_csrf_trusted = ",".join([
    KORFBAL_ORIGIN,
    *_origin_variants(WEB_KORFBAL_ORIGIN),
])
CSRF_TRUSTED_ORIGINS = _env_list("CSRF_TRUSTED_ORIGINS", _default_csrf_trusted)

# CORS
# The frontend is served from https://korfbal.butrosgroot.com while the API
# is served from https://api.korfbal.butrosgroot.com.
# We use cookies (credentials) for session auth, so we must:
# - allow the specific origin (not '*')
# - allow credentials

_default_cors_allowed = ",".join(_origin_variants(WEB_KORFBAL_ORIGIN))
CORS_ALLOWED_ORIGINS = _sorted_hosts(
    _env_list("CORS_ALLOWED_ORIGINS", _default_cors_allowed),
)
CORS_ALLOW_CREDENTIALS = _env_bool("CORS_ALLOW_CREDENTIALS", True)
if DEBUG:
    ALLOWED_HOSTS = _sorted_hosts([
        *ALLOWED_HOSTS,
        "localhost",
        "127.0.0.1",
        "kwt.localhost",
        "web.kwt.localhost",
        "bg.localhost",
    ])
    CSRF_TRUSTED_ORIGINS = sorted({
        *CSRF_TRUSTED_ORIGINS,
        KWT_ORIGIN,
        WEB_KWT_ORIGIN,
    })
    CORS_ALLOWED_ORIGINS = _sorted_hosts([
        *CORS_ALLOWED_ORIGINS,
        WEB_KWT_ORIGIN,
        KWT_ORIGIN,
        "http://localhost:4173",
        "http://localhost:5173",
    ])
SECURE_SSL_REDIRECT = _env_bool("SECURE_SSL_REDIRECT", not DEBUG)

# --- Reverse proxy / TLS termination ---
# This project is typically deployed behind nginx which terminates TLS and
# forwards requests to Django over plain HTTP.
#
# Without this, Django will think requests are not secure and
# SecurityMiddleware can trigger redirect loops when SECURE_SSL_REDIRECT=True
# (most noticeable on /admin/ and /api/ routes that hit Django).
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")

# If you ever switch nginx to set X-Forwarded-Host, you can enable this too.
# (Currently nginx passes the Host header through, so this is not required.)
# USE_X_FORWARDED_HOST = True

SECURE_HSTS_SECONDS = _env_int("SECURE_HSTS_SECONDS", 31536000 if not DEBUG else 0)
SECURE_HSTS_INCLUDE_SUBDOMAINS = _env_bool(
    "SECURE_HSTS_INCLUDE_SUBDOMAINS",
    not DEBUG,
)
SECURE_HSTS_PRELOAD = _env_bool("SECURE_HSTS_PRELOAD", not DEBUG)
SESSION_COOKIE_SECURE = _env_bool("SESSION_COOKIE_SECURE", not DEBUG)
CSRF_COOKIE_SECURE = _env_bool("CSRF_COOKIE_SECURE", not DEBUG)
X_FRAME_OPTIONS = _env("X_FRAME_OPTIONS", "SAMEORIGIN")

RUNNER = _env("RUNNER", "")


RUNNING_TESTS = bool(os.getenv("PYTEST_CURRENT_TEST")) or any(
    "pytest" in arg for arg in sys.argv
)


# --- Performance tuning (Korfbal) ---
# Keep API responses predictable by default. Expensive self-healing recomputes
# can be enabled temporarily when needed.
KORFBAL_ENABLE_IMPACT_AUTO_RECOMPUTE = _env_bool(
    "KORFBAL_ENABLE_IMPACT_AUTO_RECOMPUTE",
    DEBUG or RUNNING_TESTS,
)

_default_impact_recompute_limit = 0
if RUNNING_TESTS:
    _default_impact_recompute_limit = 3
elif DEBUG:
    _default_impact_recompute_limit = 1

KORFBAL_IMPACT_AUTO_RECOMPUTE_LIMIT = _env_int(
    "KORFBAL_IMPACT_AUTO_RECOMPUTE_LIMIT",
    _default_impact_recompute_limit,
)

# Slow SQL logging (opt-in). Useful to spot missing indexes / N+1 patterns.
KORFBAL_LOG_SLOW_DB_QUERIES = _env_bool("KORFBAL_LOG_SLOW_DB_QUERIES", False)
KORFBAL_SLOW_DB_QUERY_MS = _env_int("KORFBAL_SLOW_DB_QUERY_MS", 200)
KORFBAL_SLOW_DB_INCLUDE_SQL = _env_bool("KORFBAL_SLOW_DB_INCLUDE_SQL", False)

# Slow request surfacing (opt-in). Adds timing headers and keeps a rolling
# buffer (in cache) of the slowest requests so you don't have to tail logs.
KORFBAL_LOG_SLOW_REQUESTS = _env_bool("KORFBAL_LOG_SLOW_REQUESTS", False)
KORFBAL_SLOW_REQUEST_MS = _env_int("KORFBAL_SLOW_REQUEST_MS", 500)
KORFBAL_SLOW_REQUEST_BUFFER_SIZE = _env_int("KORFBAL_SLOW_REQUEST_BUFFER_SIZE", 200)
KORFBAL_SLOW_REQUEST_BUFFER_TTL_S = _env_int(
    "KORFBAL_SLOW_REQUEST_BUFFER_TTL_S",
    60 * 60 * 24,
)


INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "bg_django_mobile_detector",
    "phonenumber_field",
    "django_crontab",
    "apps.club",
    "apps.player",
    "apps.team",
    "apps.schedule",
    "apps.awards",
    "apps.hub",
    "apps.game_tracker",
    "apps.kwt_common",
    "bg_auth.apps.AuthenticationConfig",
    "rest_framework",
    "corsheaders",
    "drf_spectacular",
]

if RUNNER == "uwsgi":
    INSTALLED_APPS.append("django_prometheus")


MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "apps.kwt_common.middleware.request_timing.RequestTimingMiddleware",
    "apps.kwt_common.middleware.slow_queries.SlowQueryLoggingMiddleware",
    "corsheaders.middleware.CorsMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    # NOTE:
    # BlockAdminLoginMiddleware redirects /admin/login/ -> /login/ for
    # unauthenticated users. In production, /login/ is served by the SPA via
    # nginx and does not create a Django session for the admin site.
    #
    # Keeping this enabled prevents the Django admin login page from working.
    # If you later proxy /login/ to Django (or implement shared cookie domain
    # auth for *.korfbal.butrosgroot.com), you can re-enable it.
    # "bg_auth.auth_backend.BlockAdminLoginMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "bg_django_mobile_detector.middleware.DetectMiddleware",
]

if RUNNER == "uwsgi":
    MIDDLEWARE = [
        "django_prometheus.middleware.PrometheusBeforeMiddleware",
        *MIDDLEWARE,
        "django_prometheus.middleware.PrometheusAfterMiddleware",
    ]


ROOT_URLCONF = "korfbal.urls"
WSGI_APPLICATION = "korfbal.wsgi.application"
ASGI_APPLICATION = "korfbal.asgi.application"
# Keep Django's auth redirects working even though the SPA owns /login.
# (Admin uses /admin/login/; APIs should not rely on LOGIN_URL redirects.)
LOGIN_URL = "/admin/login/"


AUTHENTICATION_BACKENDS = [
    "bg_auth.auth_backend.EmailOrUsernameModelBackend",
    "django.contrib.auth.backends.ModelBackend",
]


SESSION_ENGINE = "django.contrib.sessions.backends.cache"


TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        # No project-level templates: the React SPA owns the UI.
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "apps.kwt_common.context_processors.standard_imports",
                "bg_auth.context_processors.auth_settings",
            ],
        },
    },
]


VALKEY_HOST = _env("VALKEY_HOST", "127.0.0.1")
VALKEY_PORT = _env_int("VALKEY_PORT", 6379)


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

if os.getenv("PYTEST_CURRENT_TEST") or any("pytest" in arg for arg in sys.argv):
    CHANNEL_LAYERS = {"default": {"BACKEND": "channels.layers.InMemoryChannelLayer"}}


db_engine = (
    "django_prometheus.db.backends.postgresql"
    if RUNNER == "uwsgi"
    else "django.db.backends.postgresql"
)


DATABASES = {
    "default": {
        "ENGINE": db_engine,
        "NAME": _env("POSTGRES_DB", "korfbal"),
        "USER": _env("POSTGRES_USER", "postgres"),
        "PASSWORD": _env("POSTGRES_PASSWORD", "postgres"),
        "HOST": _env("POSTGRES_HOST", "127.0.0.1"),
        "PORT": _env("POSTGRES_PORT", "5432"),
    },
}


cache_backend = (
    "django_prometheus.cache.backends.redis.RedisCache"
    if RUNNER == "uwsgi"
    else "django.core.cache.backends.redis.RedisCache"
)


CACHES = {
    "default": {
        "BACKEND": cache_backend,
        "LOCATION": f"redis://{VALKEY_HOST}:{VALKEY_PORT}/1",
    },
}


AUTH_PASSWORD_VALIDATORS = [
    {
        "NAME": (
            "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"
        ),
    },
    {
        "NAME": "django.contrib.auth.password_validation.MinimumLengthValidator",
        "OPTIONS": {"min_length": 6},
    },
    {
        "NAME": "django.contrib.auth.password_validation.CommonPasswordValidator",
    },
    {
        "NAME": "django.contrib.auth.password_validation.NumericPasswordValidator",
    },
]


DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"


EMAIL_BACKEND = _env("EMAIL_BACKEND", "django.core.mail.backends.smtp.EmailBackend")
EMAIL_HOST = _env("EMAIL_HOST", "smtp.gmail.com")
EMAIL_USE_TLS = _env_bool("EMAIL_USE_TLS", True)
EMAIL_PORT = _env("EMAIL_PORT", "587")
EMAIL_HOST_USER = _env("EMAIL_USER", "")
EMAIL_HOST_PASSWORD = _env("EMAIL_PASSWORD", "")


LANGUAGE_CODE = _env("LANGUAGE_CODE", "en-us")
TIME_ZONE = _env("TIME_ZONE", "UTC")
USE_I18N = True
USE_TZ = True


AWS_S3_ENDPOINT_URL = _env("MINIO_URL", "http://kwt-minio:9000")
AWS_S3_CUSTOM_DOMAIN = _env("MINIO_PUBLIC_DOMAIN", "localhost")
AWS_STATIC_CUSTOM_DOMAIN = f"static.{AWS_S3_CUSTOM_DOMAIN}"
AWS_MEDIA_CUSTOM_DOMAIN = f"media.{AWS_S3_CUSTOM_DOMAIN}"
AWS_ACCESS_KEY_ID = _env("MINIO_ACCESS_KEY", "minioadmin")
AWS_SECRET_ACCESS_KEY = _env("MINIO_SECRET_KEY", "minioadmin")
AWS_STORAGE_BUCKET_NAME = _env("STATIC_BUCKET", "static")
AWS_MEDIA_BUCKET_NAME = _env("MEDIA_BUCKET", "media")
AWS_QUERYSTRING_AUTH = _env_bool("AWS_QUERYSTRING_AUTH", True)
AWS_QUERYSTRING_EXPIRE = _env_int("AWS_QUERYSTRING_EXPIRE", 3600)
AWS_S3_CONFIG = {"retries": {"max_attempts": 5, "mode": "standard"}}


STATIC_URL = _env("STATIC_URL", f"{AWS_STATIC_CUSTOM_DOMAIN}/")
MEDIA_URL = _env("MEDIA_URL", f"{AWS_MEDIA_CUSTOM_DOMAIN}/")
STATIC_ROOT = Path(_env("STATIC_ROOT", str(BASE_DIR / "static")))
MEDIA_ROOT = Path(_env("MEDIA_ROOT", str(BASE_DIR / "media")))
# No extra static dirs: old Django frontend assets lived in static_workfile/.
STATICFILES_DIRS: list[Path] = []

S3_STORAGE_BACKEND = "storages.backends.s3boto3.S3Boto3Storage"
DEFAULT_STORAGE_BACKEND = _env("DEFAULT_STORAGE_BACKEND", S3_STORAGE_BACKEND)
STATICFILES_STORAGE_BACKEND = _env("STATICFILES_STORAGE", S3_STORAGE_BACKEND)


STORAGES = {
    "default": {
        "BACKEND": DEFAULT_STORAGE_BACKEND,
        "OPTIONS": {
            "bucket_name": AWS_MEDIA_BUCKET_NAME,
            # Personal data (profile pictures) should not be world-readable.
            "default_acl": "private",
            "querystring_auth": AWS_QUERYSTRING_AUTH,
            "querystring_expire": AWS_QUERYSTRING_EXPIRE,
            "custom_domain": AWS_MEDIA_CUSTOM_DOMAIN,
        },
    },
    "staticfiles": {
        "BACKEND": STATICFILES_STORAGE_BACKEND,
        "OPTIONS": {
            "bucket_name": AWS_STORAGE_BUCKET_NAME,
            "default_acl": "public-read",
            "querystring_auth": False,
            "custom_domain": AWS_STATIC_CUSTOM_DOMAIN,
        },
    },
}

CELERY_BROKER_URL = (
    f"redis://{_env('CELERY_BROKER_HOST', VALKEY_HOST)}:"
    f"{_env('CELERY_BROKER_PORT', str(VALKEY_PORT))}/0"
)
CELERY_RESULT_BACKEND = (
    f"redis://{_env('CELERY_RESULT_HOST', VALKEY_HOST)}:"
    f"{_env('CELERY_RESULT_PORT', str(VALKEY_PORT))}/0"
)
CELERY_ACCEPT_CONTENT = ["json"]
CELERY_TASK_SERIALIZER = "json"
CELERY_TIMEZONE = _env("CELERY_TIMEZONE", "UTC")
CELERY_TASK_TRACK_STARTED = _env_bool("CELERY_TASK_TRACK_STARTED", True)
CELERY_TASK_ALWAYS_EAGER = _env_bool("CELERY_TASK_ALWAYS_EAGER", False)


spotify_origin = (
    CSRF_TRUSTED_ORIGINS[0] if CSRF_TRUSTED_ORIGINS else "https://localhost"
)
SPOTIFY_CLIENT_ID = _env("SPOTIFY_CLIENT_ID", "")
SPOTIFY_CLIENT_SECRET = _env("SPOTIFY_CLIENT_SECRET", "")
SPOTIFY_REDIRECT_URI = _env(
    "SPOTIFY_REDIRECT_URI",
    f"{spotify_origin.rstrip('/')}/api/player/spotify/callback/",
)


# --- Web push notifications (PWA) ---
# The frontend subscribes using the *public* VAPID key.
# The backend sends notifications using `pywebpush` with the private key.
WEBPUSH_VAPID_PUBLIC_KEY = _env("WEBPUSH_VAPID_PUBLIC_KEY", "")
WEBPUSH_VAPID_PRIVATE_KEY = _env("WEBPUSH_VAPID_PRIVATE_KEY", "")
# Subject must be a contact URI (commonly a mailto: address).
WEBPUSH_VAPID_SUBJECT = _env("WEBPUSH_VAPID_SUBJECT", "mailto:butrosgroot@gmail.com")
WEBPUSH_TTL_SECONDS = _env_int("WEBPUSH_TTL_SECONDS", 60 * 60)


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

SITE = "Korfbal Web Tool"
LOGIN_FOTO: str = "images/logo/KWT_logo.png"
LOGIN_TITLE: str = "Welkom terug!"
LOGIN_DESCRIPTION: str = "login voor KWT"
REGISTER_TITLE: str = "Registratie"
REGISTER_HEADING_MOBILE: str = "Welkom!"
REGISTER_HEADING_DESKTOP: str = "Welkom!"
REGISTER_DESCRIPTION: str = "Maak je account aan"
BG_AUTH_SUPPORT_EMAIL: str = "butrosgroot@gmail.com"
BG_AUTH_EMAIL_CODE_VALIDITY_SECONDS: int = 15 * 60
BG_AUTH_RESEND_CONFIRMATION_MAX_AGE: int = 24 * 60 * 60
LOGIN_REDIRECT_URL = "index"


REST_FRAMEWORK = {
    "DEFAULT_SCHEMA_CLASS": "drf_spectacular.openapi.AutoSchema",
    "DEFAULT_AUTHENTICATION_CLASSES": (
        "rest_framework.authentication.SessionAuthentication",
        "rest_framework.authentication.BasicAuthentication",
    ),
    "DEFAULT_PERMISSION_CLASSES": ("rest_framework.permissions.IsAuthenticated",),
}

SPECTACULAR_SETTINGS = {
    "TITLE": "Korfbal API",
    "DESCRIPTION": "API for Korfbal application",
    "VERSION": "1.0.0",
    "SERVE_INCLUDE_SCHEMA": False,
}
