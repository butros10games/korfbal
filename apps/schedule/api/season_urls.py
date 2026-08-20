"""URL routes for season management."""

from django.urls import include, path
from rest_framework.routers import DefaultRouter

from .season_views import SeasonViewSet


router = DefaultRouter()
router.register(r"", SeasonViewSet, basename="season")

urlpatterns = [path("", include(router.urls))]
