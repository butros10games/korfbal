"""URL routes for game_tracker API."""

from __future__ import annotations

from collections.abc import Callable
from typing import cast

from django.http import HttpResponseBase
from django.urls import path

from . import views


def _route(view: object) -> Callable[..., HttpResponseBase]:
    """Adapt DRF's decorated callable type to Django's URL view contract."""
    return cast(Callable[..., HttpResponseBase], view)


urlpatterns = [
    path(
        "player_overview_data/<uuid:match_id>/<uuid:team_id>/",
        _route(views.player_overview_data),
        name="match-player-overview-data",
    ),
    path(
        "players_team/<uuid:match_id>/<uuid:team_id>/",
        _route(views.players_team),
        name="match-team-available-players",
    ),
    path(
        "player_search/<uuid:match_id>/<uuid:team_id>/",
        _route(views.player_search),
        name="match-team-player-search",
    ),
    path(
        "player_designation/",
        _route(views.player_designation),
        name="match-player-designation",
    ),
    # Backwards-compatible URL name (legacy server-rendered view tests).
    path(
        "player_designation/",
        _route(views.player_designation),
        name="player_designation",
    ),
]
