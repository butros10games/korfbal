"""Routing for the game_tracker app websocket."""

from django.urls import path

from .consumers import MatchDataConsumer, MatchTrackerConsumer


websocket_urlpatterns = [
    path("ws/match/<uuid:id>/", MatchDataConsumer.as_asgi()),  # type: ignore[arg-type]
    path("ws/match/tracker/<uuid:id>/<uuid:team_id>/", MatchTrackerConsumer.as_asgi()),  # type: ignore[arg-type]
]
