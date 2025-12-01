"""URL configuration for schedule API."""

from __future__ import annotations

from django.urls import include, path
from rest_framework.routers import DefaultRouter

from .views import MatchViewSet


router = DefaultRouter()
router.register(r"", MatchViewSet, basename="match")

urlpatterns = [
    path("", include(router.urls)),
]
