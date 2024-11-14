from django.contrib import admin
from django.urls import include, path

urlpatterns = [
    path("admin/", admin.site.urls),
    path("match/", include("apps.game_tracker.urls")),
    path("club/", include("apps.club.urls")),
    path("profile/", include("apps.player.urls")),
    path("teams/", include("apps.team.urls")),
    path("", include("apps.hub.urls")),
    path("", include("authentication.urls")),
]
