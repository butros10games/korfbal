"""ASGI config for korfbal project."""

from collections.abc import Awaitable, Callable
import os
from typing import cast

from django.core.asgi import get_asgi_application


os.environ.setdefault("DJANGO_SETTINGS_MODULE", "korfbal.settings")

AsgiMessage = dict[str, object]
AsgiReceive = Callable[[], Awaitable[AsgiMessage]]
AsgiSend = Callable[[AsgiMessage], Awaitable[None]]
AsgiApplication = Callable[[AsgiMessage, AsgiReceive, AsgiSend], Awaitable[None]]


django_application = cast(AsgiApplication, get_asgi_application())

# The Django app registry must be initialized before importing the consumer's models.
from apps.game_tracker.realtime.consumer import MatchEventsSseConsumer  # noqa: E402


sse_application = cast(AsgiApplication, MatchEventsSseConsumer.as_asgi())


async def application(
    scope: AsgiMessage,
    receive: AsgiReceive,
    send: AsgiSend,
) -> None:
    """Route the streaming endpoint outside synchronous Django middleware."""
    if scope["type"] == "http" and scope["path"] == "/api/live/events/":
        await sse_application(scope, receive, send)
        return
    await django_application(scope, receive, send)
