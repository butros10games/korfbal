"""Routing for the team app websockets."""

from typing import Any, cast

from django.urls import path

from .consumers import TeamDataConsumer


websocket_urlpatterns = [
    path("ws/teams/<uuid:id>/", cast(Any, TeamDataConsumer.as_asgi())),
]
