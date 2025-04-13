"""Django settings for korfbal project."""

import os
from pathlib import Path

from dotenv import load_dotenv


# ------------------------------------------------------------------------------
# Helper Functions
# ------------------------------------------------------------------------------
def get_bool_env(env_key: str, default: bool = False) -> bool:
    """Return True if environment variable is set to 'true' or '1' (case-insensitive).
    Otherwise, return the default value.
    """
    return os.getenv(env_key, str(default)).lower() in ["true", "1"]


def get_list_env(env_key: str, default=None, delimiter: str = ",") -> list[str]:
    """Split the environment variable by the given delimiter and return as a list.
    If the env variable is not set, return the default list.
    """
    if default is None:
        default = []
    val = os.getenv(env_key, None)
    return val.split(delimiter) if val else default


# ------------------------------------------------------------------------------
# Base Directories and Environment
# ------------------------------------------------------------------------------
BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")  # Load environment variables from the .env file

PROJECT_DIR = BASE_DIR / "korfbal"

# ------------------------------------------------------------------------------
# Security & Debug
# ------------------------------------------------------------------------------
SECRET_KEY = os.getenv("SECRET_KEY")  # Provide a default for dev
DEBUG = get_bool_env("DEBUG", default=False)

ALLOWED_HOSTS = get_list_env("ALLOWED_HOSTS", default=["korfbal.butrosgroot.com"])
CSRF_TRUSTED_ORIGINS = get_list_env(
    "CSRF_TRUSTED_ORIGINS", default=["https://korfbal.butrosgroot.com"]
)

if DEBUG:
    # Debug-specific settings
    SECURE_SSL_REDIRECT = False
    ALLOWED_HOSTS.extend(["localhost", "127.0.0.1"])
else:
    # Production security settings
    SECURE_HSTS_SECONDS = 31536000  # 1 year
    SECURE_HSTS_INCLUDE_SUBDOMAINS = True
    SECURE_HSTS_PRELOAD = True
    SECURE_SSL_REDIRECT = True

    # Secure cookies
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True

# ------------------------------------------------------------------------------
# Application Definition
# ------------------------------------------------------------------------------
INSTALLED_APPS = [
    # Django apps
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    # Third-party
    "mobiledetect",
    "phonenumber_field",
    # Local apps
    "apps.club",
    "apps.player",
    "apps.team",
    "apps.schedule",
    "apps.hub",
    "apps.game_tracker",
    "apps.common",
    "bg_auth.apps.AuthenticationConfig",
]

RUNNER = os.getenv("RUNNER", "")

if RUNNER == "uwsgi":
    INSTALLED_APPS.append("django_prometheus")

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "bg_auth.auth_backend.BlockAdminLoginMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "mobiledetect.middleware.DetectMiddleware",
    "apps.common.middleware.VisitorTrackingMiddleware",
]

if RUNNER == "uwsgi":
    MIDDLEWARE = (
        ["django_prometheus.middleware.PrometheusBeforeMiddleware"]
        + MIDDLEWARE
        + ["django_prometheus.middleware.PrometheusAfterMiddleware"]
    )

ROOT_URLCONF = "korfbal.urls"
ASGI_APPLICATION = "korfbal.asgi.application"
LOGIN_URL = "login"

AUTHENTICATION_BACKENDS = [
    "bg_auth.auth_backend.EmailOrUsernameModelBackend",
    "django.contrib.auth.backends.ModelBackend",
]

SESSION_ENGINE = "django.contrib.sessions.backends.cache"

# ------------------------------------------------------------------------------
# Templates
# ------------------------------------------------------------------------------
TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                # custom context processors
                "apps.common.context_processors.standard_imports",
            ],
        },
    },
]

# ------------------------------------------------------------------------------
# Channels & Redis
# ------------------------------------------------------------------------------
CHANNEL_LAYERS = {
    "default": {
        "BACKEND": "channels_redis.core.RedisChannelLayer",
        "CONFIG": {
            "hosts": [
                (
                    os.getenv("REDIS_HOST", "127.0.0.1"),
                    int(os.getenv("REDIS_PORT", "6379")),
                )
            ],
            "capacity": 100,
            "expiry": 60,
        },
    },
}

# ------------------------------------------------------------------------------
# Database
# https://docs.djangoproject.com/en/4.1/ref/settings/#databases
# ------------------------------------------------------------------------------
DATABASES = {
    "default": {
        "ENGINE": (
            "django_prometheus.db.backends.postgresql"
            if RUNNER == "uwsgi"
            else "django.db.backends.postgresql"
        ),
        "NAME": os.getenv("POSTGRES_DB"),
        "USER": os.getenv("POSTGRES_USER"),
        "PASSWORD": os.getenv("POSTGRES_PASSWORD"),
        "HOST": os.getenv("POSTGRES_HOST"),
        "PORT": os.getenv("POSTGRES_PORT"),
    }
}

# ------------------------------------------------------------------------------
# Caches
# ------------------------------------------------------------------------------
CACHES = {
    "default": {
        "BACKEND": (
            "django_prometheus.cache.backends.redis.RedisCache"
            if RUNNER == "uwsgi"
            else "django.core.cache.backends.redis.RedisCache"
        ),
        "LOCATION": (
            f"redis://{os.getenv('REDIS_HOST', '127.0.0.1')}:"
            f"{os.getenv('REDIS_PORT', '6379')}/1"
        ),
    }
}

# ------------------------------------------------------------------------------
# Password Validation
# https://docs.djangoproject.com/en/4.1/ref/settings/#auth-password-validators
# ------------------------------------------------------------------------------
AUTH_PASSWORD_VALIDATORS = [
    {
        "NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator",  # noqa E501
    },
    {
        "NAME": "django.contrib.auth.password_validation.MinimumLengthValidator",
        "OPTIONS": {
            "min_length": 6,
        },
    },
    {
        "NAME": "django.contrib.auth.password_validation.CommonPasswordValidator",
    },
    {
        "NAME": "django.contrib.auth.password_validation.NumericPasswordValidator",
    },
]

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# ------------------------------------------------------------------------------
# Email Settings
# ------------------------------------------------------------------------------
EMAIL_BACKEND = "django.core.mail.backends.smtp.EmailBackend"
EMAIL_HOST = os.getenv("EMAIL_HOST", "smtp.gmail.com")
EMAIL_USE_TLS = True
EMAIL_HOST_USER = os.getenv("EMAIL_USER")
EMAIL_HOST_PASSWORD = os.getenv("EMAIL_PASSWORD")
EMAIL_PORT = os.getenv("EMAIL_PORT", "587")

# ------------------------------------------------------------------------------
# Internationalization
# ------------------------------------------------------------------------------
LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

# ------------------------------------------------------------------------------
# Template Directories
# ------------------------------------------------------------------------------
TEMPLATE_DIRS = [BASE_DIR / "templates"]

# ------------------------------------------------------------------------------
# MinIO / S3 Settings
# ------------------------------------------------------------------------------
AWS_S3_ENDPOINT_URL = os.getenv("MINIO_URL", "http://kwt-minio:9000")
AWS_S3_CUSTOM_DOMAIN = os.getenv("MINIO_PUBLIC_DOMAIN", "localhost")

AWS_STATIC_CUSTOM_DOMAIN = f"static.{AWS_S3_CUSTOM_DOMAIN}"
AWS_MEDIA_CUSTOM_DOMAIN = f"media.{AWS_S3_CUSTOM_DOMAIN}"

AWS_ACCESS_KEY_ID = os.getenv("MINIO_ACCESS_KEY", "minioadmin")
AWS_SECRET_ACCESS_KEY = os.getenv("MINIO_SECRET_KEY", "minioadmin")
AWS_STORAGE_BUCKET_NAME = os.getenv("STATIC_BUCKET", "static")
AWS_MEDIA_BUCKET_NAME = os.getenv("MEDIA_BUCKET", "media")
AWS_QUERYSTRING_AUTH = True

AWS_S3_CONFIG = {
    "retries": {
        "max_attempts": 5,
        "mode": "standard",
    },
}

# ------------------------------------------------------------------------------
# Static & Media Files
# ------------------------------------------------------------------------------
STATIC_URL = f"{AWS_STATIC_CUSTOM_DOMAIN}/"
MEDIA_URL = f"{AWS_MEDIA_CUSTOM_DOMAIN}/"

STATICFILES_STORAGE = "storages.backends.s3boto3.S3Boto3Storage"
STATICFILES_DIRS = [BASE_DIR / "static_workfile"]

STORAGES = {
    "default": {
        "BACKEND": STATICFILES_STORAGE,
        "OPTIONS": {
            "bucket_name": AWS_MEDIA_BUCKET_NAME,
            "default_acl": "public-read",
            "querystring_auth": False,
            "custom_domain": AWS_MEDIA_CUSTOM_DOMAIN,
        },
    },
    "staticfiles": {
        "BACKEND": STATICFILES_STORAGE,
        "OPTIONS": {
            "bucket_name": AWS_STORAGE_BUCKET_NAME,
            "default_acl": "public-read",
            "querystring_auth": False,
            "custom_domain": AWS_STATIC_CUSTOM_DOMAIN,
        },
    },
}

# ------------------------------------------------------------------------------
# Spotify API
# ------------------------------------------------------------------------------
SPOTIFY_CLIENT_ID = os.getenv("SPOTIFY_CLIENT_ID", "")
SPOTIFY_CLIENT_SECRET = os.getenv("SPOTIFY_CLIENT_SECRET", "")
SPOTIFY_REDIRECT_URI = (
    f"{os.getenv('CSRF_TRUSTED_ORIGINS', 'https://localhost')}/spotify"
)

# ------------------------------------------------------------------------------
# Prometheus
# ------------------------------------------------------------------------------
PROMETHEUS_LATENCY_BUCKETS = (
    0.01,
    0.025,
    0.05,
    0.075,
    0.1,
    0.25,
    0.5,
    0.75,
    1.0,
    2.5,
    5.0,
    7.5,
    10.0,
    25.0,
    50.0,
    75.0,
    float("inf"),
)
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
