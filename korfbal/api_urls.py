"""API URL configuration for the Korfbal project."""

from django.urls import include, path
from drf_spectacular.views import (
    SpectacularAPIView,
    SpectacularRedocView,
    SpectacularSwaggerView,
)


urlpatterns = [
    path("schema/", SpectacularAPIView.as_view(), name="schema"),
    path(
        "schema/swagger-ui/",
        SpectacularSwaggerView.as_view(url_name="schema"),
        name="swagger-ui",
    ),
    path(
        "schema/redoc/",
        SpectacularRedocView.as_view(url_name="schema"),
        name="redoc",
    ),
    path("club/", include("apps.club.api.urls")),
    path("player/", include("apps.player.api.urls")),
    path("team/", include("apps.team.api.urls")),
    path("matches/", include("apps.schedule.api.urls")),
    path("match/", include("apps.game_tracker.api.urls")),
    path("hub/", include("apps.hub.api.urls")),
    path("debug/", include("apps.kwt_common.api.urls")),
    # Authentication endpoints for the SPA (API-only; no HTML pages)
    path("", include("korfbal.auth_api_urls")),
]
