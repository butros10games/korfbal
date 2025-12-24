"""Routing for the club app websockets."""

from typing import Any, cast

from django.urls import path

from .consumers import ClubDataConsumer


websocket_urlpatterns = [
    path("ws/club/<uuid:id>/", cast(Any, ClubDataConsumer.as_asgi())),
]
