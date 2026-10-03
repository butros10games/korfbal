"""URL routes for club API."""

from django.urls import include, path
from rest_framework.routers import DefaultRouter

from apps.player.media_paths import VARIANT

from .logo import ClubLogoAPIView, ClubLogoVariantAPIView
from .views import ClubViewSet


router = DefaultRouter()
router.register(r"clubs", ClubViewSet)

urlpatterns = [
    path(
        "clubs/<uuid:club_id>/logo/<str:version>/",
        ClubLogoAPIView.as_view(),
        name="club-logo",
    ),
    path(
        f"clubs/<uuid:club_id>/logo/<str:version>/{VARIANT}/",
        ClubLogoVariantAPIView.as_view(),
        name="club-logo-variant",
    ),
    path("", include(router.urls)),
]
