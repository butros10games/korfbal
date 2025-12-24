"""Routing for the game_tracker app websocket."""

from typing import Any, cast

from django.urls import path

from .consumers import MatchDataConsumer, MatchTrackerConsumer


websocket_urlpatterns = [
    path("ws/match/<uuid:id>/", cast(Any, MatchDataConsumer.as_asgi())),
    path(
        "ws/match/tracker/<uuid:id>/<uuid:team_id>/",
        cast(Any, MatchTrackerConsumer.as_asgi()),
    ),
]
