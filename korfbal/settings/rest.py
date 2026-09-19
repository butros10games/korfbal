"""Django REST Framework and OpenAPI config."""

from __future__ import annotations


REST_FRAMEWORK = {
    "DEFAULT_SCHEMA_CLASS": "drf_spectacular.openapi.AutoSchema",
    "DEFAULT_AUTHENTICATION_CLASSES": (
        "korfbal.authentication.JwtBearerAuthentication",
        "rest_framework.authentication.SessionAuthentication",
    ),
    "DEFAULT_PERMISSION_CLASSES": ("rest_framework.permissions.IsAuthenticated",),
}

SPECTACULAR_SETTINGS = {
    "TITLE": "KorfConnect API",
    "DESCRIPTION": "API for KorfConnect application",
    "VERSION": "1.0.0",
    "SERVE_INCLUDE_SCHEMA": False,
    "ENUM_NAME_OVERRIDES": {
        "TournamentSideEnum": ["home", "away"],
        "PlayerVisibilityEnum": "apps.player.models.player.Player.Visibility",
    },
}
