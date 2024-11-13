"""
Django settings for Korfbal project.

Generated by 'django-admin startproject' using Django 4.1.7.

For more information on this file, see
https://docs.djangoproject.com/en/4.1/topics/settings/

For the full list of settings and their values, see
https://docs.djangoproject.com/en/4.1/ref/settings/
"""

from pathlib import Path
import os
import json

# Build paths inside the project like this: BASE_DIR / "subdir".
BASE_DIR = Path(__file__).resolve().parent.parent
PROJECT_DIR = Path(__file__).resolve().parent

# Load secrets from private_settings.json
with open(os.path.join(PROJECT_DIR, "private_settings.json")) as f:
    secrets = json.load(f)

# SECURITY WARNING: keep the secret key used in production secret!
SECRET_KEY = secrets["SECRET_KEY"]

# SECURITY WARNING: don"t run with debug turned on in production!
DEBUG = secrets["DEBUG"]

ALLOWED_HOSTS = ["korfbal.butrosgroot.com"]

CSRF_TRUSTED_ORIGINS = ["https://korfbal.butrosgroot.com"]

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

ROOT_URLCONF = "Korfbal.urls"
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

ASGI_APPLICATION = "Korfbal.asgi.application"
CHANNEL_LAYERS = {
    "default": {
        "BACKEND": "channels_redis.core.RedisChannelLayer",
        "CONFIG": {
            "hosts": [("127.0.0.1", 6379)],
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
        "NAME": secrets["DB_NAME"],
        "USER": secrets["DB_USER"],
        "PASSWORD": secrets["DB_PASSWORD"],
        "HOST": secrets["DB_HOST"],  # Replace with the actual hostname
        "PORT": secrets["DB_PORT"],  # Replace with the actual port number
    }
}

CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.redis.RedisCache",
        "LOCATION": "redis://127.0.0.1:6379/1",
    }
}

# Password validation
# https://docs.djangoproject.com/en/4.1/ref/settings/#auth-password-validators

AUTH_PASSWORD_VALIDATORS = [
    {
        "NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator",
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

EMAIL_BACKEND = "django.core.mail.backends.smtp.EmailBackend"
EMAIL_HOST = "smtp.gmail.com"
EMAIL_USE_TLS = True
EMAIL_HOST_USER = secrets["EMAIL_USER"]
EMAIL_HOST_PASSWORD = secrets["EMAIL_PASSWORD"]
EMAIL_PORT = secrets["EMAIL_PORT"]

# Internationalization
# https://docs.djangoproject.com/en/4.1/topics/i18n/

LANGUAGE_CODE = "en-us"

TIME_ZONE = "UTC"

USE_I18N = True

USE_TZ = True


# Static files (CSS, JavaScript, Images)
# https://docs.djangoproject.com/en/4.1/howto/static-files/

STATIC_URL = "static/"
STATIC_ROOT = "static/"
STATICFILES_DIRS = ["static_workfile/"]

TEMPLATE_DIRS = [
    os.path.join(BASE_DIR, "templates"),
]

# Default primary key field type
# https://docs.djangoproject.com/en/4.1/ref/settings/#default-auto-field

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

STORAGES = {
    "default": {
        "BACKEND": "storages.backends.sftpstorage.SFTPStorage",
        "OPTIONS": {
            "host": secrets["SFTP_HOST"],
            "params": {
                "username": secrets["SFTP_USER"],
                "password": secrets["SFTP_PASSWORD"],
                "port": secrets["SFTP_PORT"],
            },
            "root_path": os.path.join(secrets["SFTP_REMOTE_PATH"], "media"),
        },
    },
    "staticfiles": {
        "BACKEND": "storages.backends.sftpstorage.SFTPStorage",
        "OPTIONS": {
            "host": secrets["SFTP_HOST"],
            "params": {
                "username": secrets["SFTP_USER"],
                "password": secrets["SFTP_PASSWORD"],
                "port": secrets["SFTP_PORT"],
            },
            "root_path": os.path.join(secrets["SFTP_REMOTE_PATH"], "static"),
        },
    },
}
