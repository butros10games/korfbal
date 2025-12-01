"""URL routes for hub API."""

from __future__ import annotations

from django.urls import path

from .views import UpdateFeedView


urlpatterns = [
    path("updates/", UpdateFeedView.as_view(), name="hub-updates"),
]
