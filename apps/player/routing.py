"""Routing for the player app websockets."""

from typing import Any, cast

from django.urls import path

from .consumers import ProfileDataConsumer


websocket_urlpatterns = [
    path("ws/profile/<uuid:id>/", cast(Any, ProfileDataConsumer.as_asgi())),
]
