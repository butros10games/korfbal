"""ASGI Server-Sent Events consumer for public match invalidations."""

from __future__ import annotations

import asyncio
import json
from urllib.parse import parse_qs
from uuid import UUID

from channels.consumer import AsyncConsumer
from channels.db import database_sync_to_async
from channels.exceptions import StopConsumer
from django.conf import settings

from apps.game_tracker.models import MatchData
from apps.game_tracker.realtime.metrics import (
    SSE_ACTIVE_CONNECTIONS,
    SSE_EVENTS_SENT,
    SSE_REJECTIONS,
)
from apps.game_tracker.realtime.publisher import match_group_name


class MatchEventsSseConsumer(AsyncConsumer):
    """Multiplex public change notifications for a bounded set of matches."""

    match_ids: tuple[str, ...] = ()
    heartbeat_task: asyncio.Task[None] | None = None
    connection_counted = False

    async def http_request(self, event: dict[str, object]) -> None:
        """Open a validated event stream for the requested match IDs."""
        del event
        if not settings.KORFBAL_SSE_ENABLED:
            SSE_REJECTIONS.labels(reason="disabled").inc()
            await self._reject(404, "SSE is disabled.")
            return

        origin = self._origin()
        if origin and not self._origin_allowed(origin):
            SSE_REJECTIONS.labels(reason="origin").inc()
            await self._reject(403, "Origin is not allowed.")
            return

        try:
            self.match_ids = self._parse_match_ids()
        except ValueError as exc:
            SSE_REJECTIONS.labels(reason="request").inc()
            await self._reject(400, str(exc))
            return

        for match_id in self.match_ids:
            await self.channel_layer.group_add(
                match_group_name(match_id),
                self.channel_name,
            )

        revisions = await self._current_revisions(self.match_ids)
        headers = [
            (b"content-type", b"text/event-stream; charset=utf-8"),
            (b"cache-control", b"no-cache, no-transform"),
            (b"x-accel-buffering", b"no"),
            (b"vary", b"Origin"),
        ]
        if origin:
            headers.append((b"access-control-allow-origin", origin.encode("ascii")))

        await self.send({
            "type": "http.response.start",
            "status": 200,
            "headers": headers,
        })
        SSE_ACTIVE_CONNECTIONS.inc()
        self.connection_counted = True
        await self._send_event("ready", {"revisions": revisions})
        self.heartbeat_task = asyncio.create_task(self._send_heartbeats())

    async def http_disconnect(self, event: dict[str, object]) -> None:
        """Release group subscriptions when the browser disconnects.

        Raises:
            StopConsumer: Always, to terminate the Channels application loop.

        """
        del event
        await self._cleanup()
        raise StopConsumer

    async def match_changed(self, event: dict[str, object]) -> None:
        """Forward a Channels group notification as an SSE event."""
        await self._send_event(
            "match.changed",
            {
                "match_id": event["match_id"],
                "revision": event["revision"],
                "resources": event["resources"],
            },
        )

    async def _send_event(self, name: str, payload: object) -> None:
        body = f"event: {name}\ndata: {json.dumps(payload, separators=(',', ':'))}\n\n"
        await self.send(
            {
                "type": "http.response.body",
                "body": body.encode(),
                "more_body": True,
            },
        )
        SSE_EVENTS_SENT.labels(event=name).inc()

    async def _send_heartbeats(self) -> None:
        try:
            while True:
                await asyncio.sleep(settings.KORFBAL_SSE_HEARTBEAT_SECONDS)
                await self.send(
                    {
                        "type": "http.response.body",
                        "body": b": heartbeat\n\n",
                        "more_body": True,
                    },
                )
                SSE_EVENTS_SENT.labels(event="heartbeat").inc()
        except asyncio.CancelledError:
            return

    async def _cleanup(self) -> None:
        if self.connection_counted:
            SSE_ACTIVE_CONNECTIONS.dec()
            self.connection_counted = False
        if self.heartbeat_task:
            self.heartbeat_task.cancel()
            self.heartbeat_task = None
        for match_id in self.match_ids:
            await self.channel_layer.group_discard(
                match_group_name(match_id),
                self.channel_name,
            )
        self.match_ids = ()

    async def _reject(self, status: int, message: str) -> None:
        await self.send(
            {
                "type": "http.response.start",
                "status": status,
                "headers": [(b"content-type", b"application/json")],
            },
        )
        await self.send(
            {
                "type": "http.response.body",
                "body": json.dumps({"detail": message}).encode(),
                "more_body": False,
            },
        )
        raise StopConsumer

    def _parse_match_ids(self) -> tuple[str, ...]:
        params = parse_qs(self.scope.get("query_string", b"").decode())
        raw_ids = params.get("match_ids", [""])[0]
        values = tuple(
            dict.fromkeys(
                value.strip() for value in raw_ids.split(",") if value.strip()
            )
        )
        if not values:
            raise ValueError("At least one match_id is required.")
        if len(values) > settings.KORFBAL_SSE_MAX_MATCHES:
            raise ValueError("Too many match_ids were requested.")
        try:
            return tuple(str(UUID(value)) for value in values)
        except ValueError as exc:
            raise ValueError("Every match_id must be a valid UUID.") from exc

    def _origin(self) -> str | None:
        headers = dict(self.scope.get("headers", []))
        value = headers.get(b"origin")
        return value.decode("ascii") if value else None

    @staticmethod
    def _origin_allowed(origin: str) -> bool:
        return (
            settings.CORS_ALLOW_ALL_ORIGINS or origin in settings.CORS_ALLOWED_ORIGINS
        )

    @staticmethod
    @database_sync_to_async
    def _current_revisions(match_ids: tuple[str, ...]) -> dict[str, int]:
        rows = MatchData.objects.filter(match_link_id__in=match_ids).values_list(
            "match_link_id",
            "live_revision",
        )
        return {str(match_id): revision for match_id, revision in rows}
