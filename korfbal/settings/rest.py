"""Django REST Framework and OpenAPI config."""

from __future__ import annotations


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
