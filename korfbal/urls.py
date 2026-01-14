"""korfbal URL Configuration."""

from django.conf import settings
from django.contrib import admin
from django.urls import include, path


urlpatterns = [
    path("admin/", admin.site.urls),
    # Primary API routing: we host the API on a dedicated domain
    # (e.g. https://api.korfbal.localhost), so we don't need an extra /api prefix.
    path("", include("korfbal.api_urls")),
    # Backwards-compatible prefix for older clients that still call /api/...
    path("api/", include("korfbal.api_urls")),
    # Backwards-compatible alias for older clients that still call /match/api/...
    path("match/api/", include("apps.game_tracker.api.urls")),
]

if getattr(settings, "RUNNER", "") == "uwsgi":
    urlpatterns.append(path("", include("django_prometheus.urls")))
