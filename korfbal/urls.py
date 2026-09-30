"""korfbal URL Configuration."""

from django.conf import settings
from django.contrib import admin
from django.urls import include, path


urlpatterns = [
    path("admin/", admin.site.urls),
    # Both API mounts include the same views, permissions and throttles; only the
    # URL prefix differs. `/api/` is canonical: configured clients use it, the
    # production edge forwards only `/api/*` (and the alias below) to Django, and
    # `reverse()` resolves to it because the later mount wins.
    #
    # The unprefixed mount is not reachable through the production edge. It
    # remains for direct app-server callers such as local development and tests
    # that still use root paths; migrate those before removing it. `/healthz` is
    # answered by the edge itself, not by this mount.
    path("", include("korfbal.api_urls")),
    path("api/", include("korfbal.api_urls")),
    # Legacy lineup alias from the removed Django templates (last client use
    # 2025-12). Retire once edge access logs show no `/match/api/` traffic and
    # the edge rule forwarding it is removed; `/api/match/` serves current clients.
    path("match/api/", include("apps.game_tracker.api.urls")),
]

if getattr(settings, "KORFBAL_ENABLE_PROMETHEUS", False):
    urlpatterns.append(path("", include("django_prometheus.urls")))
