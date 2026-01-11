"""Email configuration."""

from __future__ import annotations

from .env import env, env_bool


EMAIL_BACKEND = env("EMAIL_BACKEND", "django.core.mail.backends.smtp.EmailBackend")
EMAIL_HOST = env("EMAIL_HOST", "smtp.gmail.com")
EMAIL_USE_TLS = env_bool("EMAIL_USE_TLS", True)
EMAIL_PORT = env("EMAIL_PORT", "587")
EMAIL_HOST_USER = env("EMAIL_USER", "")
EMAIL_HOST_PASSWORD = env("EMAIL_PASSWORD", "")
