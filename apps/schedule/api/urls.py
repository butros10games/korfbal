"""URL configuration for schedule API."""

from __future__ import annotations

from django.urls import include, path
from rest_framework.routers import DefaultRouter

from .match_notes import MatchNoteDetailView, MatchNotesView
from .views import MatchViewSet


router = DefaultRouter()
router.register(r"", MatchViewSet, basename="match")

urlpatterns = [
    path("<uuid:match_id>/notes/", MatchNotesView.as_view(), name="match-notes"),
    path(
        "<uuid:match_id>/notes/<uuid:note_id>/",
        MatchNoteDetailView.as_view(),
        name="match-note-detail",
    ),
    path("", include(router.urls)),
]
