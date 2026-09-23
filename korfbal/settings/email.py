"""Email configuration."""

from __future__ import annotations

from .env import env, env_bool, env_int


MAILERS = {
    "default": {
        "BACKEND": env(
            "EMAIL_BACKEND",
            "django.core.mail.backends.smtp.EmailBackend",
        ),
        "OPTIONS": {
            "host": env("EMAIL_HOST", "smtp.gmail.com"),
            "port": env_int("EMAIL_PORT", 587),
            "use_tls": env_bool("EMAIL_USE_TLS", True),
            "username": env("EMAIL_USER", ""),
            "password": env("EMAIL_PASSWORD", ""),
        },
    },
}

# Django's implicit sender is webmaster@localhost, which is unsuitable for
# production mail sent through the configured SMTP account.
DEFAULT_FROM_EMAIL = env("DEFAULT_FROM_EMAIL", env("EMAIL_USER", ""))
