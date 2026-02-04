"""Core Django configuration (apps, middleware, templates, auth)."""

from __future__ import annotations

from .runtime import KORFBAL_ENABLE_PROMETHEUS


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

if KORFBAL_ENABLE_PROMETHEUS:
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
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "bg_django_mobile_detector.middleware.DetectMiddleware",
]

if KORFBAL_ENABLE_PROMETHEUS:
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

AUTH_PASSWORD_VALIDATORS = [
    {
        "NAME": "django.contrib.auth.password_validation."
        "UserAttributeSimilarityValidator",
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
