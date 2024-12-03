"""Django settings for korfbal project."""

import os
from pathlib import Path

from dotenv import load_dotenv
from storages.backends.s3boto3 import S3Boto3Storage

# Load environment variables from .env file in the project root
BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(os.path.join(BASE_DIR, ".env"))

# Build paths inside the project like this: BASE_DIR / "subdir".
PROJECT_DIR = Path(__file__).resolve().parent

# SECURITY WARNING: keep the secret key used in production secret!
SECRET_KEY = os.environ.get("SECRET_KEY")

# SECURITY WARNING: don't run with debug turned on in production!
DEBUG = os.environ.get("DEBUG", "False") == "True"

ALLOWED_HOSTS = os.environ.get("ALLOWED_HOSTS", "korfbal.butrosgroot.com").split(",")

CSRF_TRUSTED_ORIGINS = os.environ.get(
    "CSRF_TRUSTED_ORIGINS", "https://korfbal.butrosgroot.com"
).split(",")

if DEBUG:
    SECURE_SSL_REDIRECT = False
    ALLOWED_HOSTS.extend(["localhost", "127.0.0.1"])
else:
    SECURE_HSTS_SECONDS = 31536000  # 1 year
    SECURE_HSTS_INCLUDE_SUBDOMAINS = True
    SECURE_HSTS_PRELOAD = True

    SECURE_SSL_REDIRECT = True

    # Secure cookies
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True

# Application definition
INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "mobiledetect",
    "phonenumber_field",
    "apps.club",
    "apps.player",
    "apps.team",
    "apps.schedule",
    "apps.hub",
    "apps.game_tracker",
    "apps.common",
    "authentication.apps.AuthenticationConfig",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "authentication.auth_backend.BlockAdminLoginMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "mobiledetect.middleware.DetectMiddleware",
    "apps.common.middleware.VisitorTrackingMiddleware",
]

ROOT_URLCONF = "korfbal.urls"
LOGIN_URL = "login"

AUTHENTICATION_BACKENDS = [
    "authentication.auth_backend.EmailOrUsernameModelBackend",
    "django.contrib.auth.backends.ModelBackend",
]

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [os.path.join(BASE_DIR, "templates")],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                # custom context processors
                "apps.common.context_processors.standart_imports",
            ],
        },
    },
]

ASGI_APPLICATION = "korfbal.asgi.application"

CHANNEL_LAYERS = {
    "default": {
        "BACKEND": "channels_redis.core.RedisChannelLayer",
        "CONFIG": {
            "hosts": [
                (
                    os.environ.get("REDIS_HOST", "127.0.0.1"),
                    int(os.environ.get("REDIS_PORT", "6379")),
                )
            ],
            "capacity": 100,
            "expiry": 60,
        },
    },
}

# Database
# https://docs.djangoproject.com/en/4.1/ref/settings/#databases

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": os.environ.get("POSTGRES_DB"),
        "USER": os.environ.get("POSTGRES_USER"),
        "PASSWORD": os.environ.get("POSTGRES_PASSWORD"),
        "HOST": os.environ.get("POSTGRES_HOST"),
        "PORT": os.environ.get("POSTGRES_PORT"),
    }
}

CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.redis.RedisCache",
        "LOCATION": f"redis://{os.environ.get('REDIS_HOST', '127.0.0.1')}:{os.environ.get('REDIS_PORT', '6379')}/1",  # noqa E501
    }
}

# Password validation
# https://docs.djangoproject.com/en/4.1/ref/settings/#auth-password-validators

AUTH_PASSWORD_VALIDATORS = [
    {
        "NAME": (
            "django.contrib.auth.password_validation.UserAttributeSimilarityValidator",
        )
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

# Email settings
EMAIL_BACKEND = "django.core.mail.backends.smtp.EmailBackend"
EMAIL_HOST = os.environ.get("EMAIL_HOST", "smtp.gmail.com")
EMAIL_USE_TLS = True
EMAIL_HOST_USER = os.environ.get("EMAIL_USER")
EMAIL_HOST_PASSWORD = os.environ.get("EMAIL_PASSWORD")
EMAIL_PORT = os.environ.get("EMAIL_PORT", "587")

# Internationalization
LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

# Templates
TEMPLATE_DIRS = [
    os.path.join(BASE_DIR, "templates"),
]

# Internal Minio URL for application use
AWS_S3_ENDPOINT_URL = os.environ.get("MINIO_URL", "http://kwt-minio:9000")

# Public URL for serving static files to the browser
AWS_S3_CUSTOM_DOMAIN = os.environ.get("MINIO_PUBLIC_URL", "http://localhost:9000")

AWS_ACCESS_KEY_ID = os.environ.get("MINIO_ACCESS_KEY", "minioadmin")
AWS_SECRET_ACCESS_KEY = os.environ.get("MINIO_SECRET_KEY", "minioadmin")
AWS_STORAGE_BUCKET_NAME = os.environ.get("STATIC_BUCKET", "static")
AWS_MEDIA_BUCKET_NAME = os.environ.get("MEDIA_BUCKET", "media")
AWS_QUERYSTRING_AUTH = False

AWS_S3_CONFIG = {
    "retries": {
        "max_attempts": 5,
        "mode": "standard",
    },
}

STORAGES = {
    "default": {
        "BACKEND": "storages.backends.s3boto3.S3Boto3Storage",
        "OPTIONS": {
            "bucket_name": AWS_MEDIA_BUCKET_NAME,
        },
    },
    "staticfiles": {
        "BACKEND": "storages.backends.s3boto3.S3Boto3Storage",
        "OPTIONS": {
            "bucket_name": AWS_STORAGE_BUCKET_NAME,
        },
    },
}

STATIC_URL = f"{AWS_S3_CUSTOM_DOMAIN}/{AWS_STORAGE_BUCKET_NAME}/"
MEDIA_URL = f"{AWS_S3_CUSTOM_DOMAIN}/{AWS_MEDIA_BUCKET_NAME}/"

STATICFILES_STORAGE = "storages.backends.s3boto3.S3StaticStorage"
STATICFILES_DIRS = ["static_workfile"]

WEBPACK_LOADER = {
    "DEFAULT": {
        "BUNDLE_DIR_NAME": "webpack_bundles/",
        "STATS_FILE": BASE_DIR / "webpack-stats.json",
    }
}
