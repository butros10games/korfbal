"""URL routes for player API."""

from django.urls import include, path
from rest_framework.routers import DefaultRouter

from .views import CurrentPlayerAPIView, PlayerViewSet


router = DefaultRouter()
router.register(r"players", PlayerViewSet)

urlpatterns = [
    path("", include(router.urls)),
    path("me/", CurrentPlayerAPIView.as_view(), name="player-current"),
]
