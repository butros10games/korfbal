"""URL routes for club API."""

from django.urls import include, path
from rest_framework.routers import DefaultRouter

from .logo import ClubLogoAPIView
from .views import ClubViewSet


router = DefaultRouter()
router.register(r"clubs", ClubViewSet)

urlpatterns = [
    path(
        "clubs/<uuid:club_id>/logo/<str:version>/",
        ClubLogoAPIView.as_view(),
        name="club-logo",
    ),
    path("", include(router.urls)),
]
