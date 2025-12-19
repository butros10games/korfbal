"""URL routes for game_tracker API."""

from __future__ import annotations

from django.urls import path

from . import views


urlpatterns = [
    path(
        "player_overview_data/<uuid:match_id>/<uuid:team_id>/",
        views.player_overview_data,
        name="match-player-overview-data",
    ),
    path(
        "players_team/<uuid:match_id>/<uuid:team_id>/",
        views.players_team,
        name="match-team-available-players",
    ),
    path(
        "player_search/<uuid:match_id>/<uuid:team_id>/",
        views.player_search,
        name="match-team-player-search",
    ),
    path(
        "player_designation/",
        views.player_designation,
        name="match-player-designation",
    ),
]
