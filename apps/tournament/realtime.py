"""ASGI Server-Sent Events consumer for tournament display invalidations."""

from __future__ import annotations

import asyncio
import json
from urllib.parse import parse_qs
from uuid import UUID

from channels.consumer import AsyncConsumer
from channels.db import database_sync_to_async
from channels.exceptions import StopConsumer
from django.conf import settings

from apps.tournament.models import Tournament
from apps.tournament.realtime_contracts import tournament_group_name


MAX_TOURNAMENT_STREAMS = 20


class TournamentEventsSseConsumer(AsyncConsumer):
    """Stream public revision notifications for a bounded tournament set."""

    tournament_ids: tuple[str, ...] = ()
    heartbeat_task: asyncio.Task[None] | None = None

    async def http_request(self, event: dict[str, object]) -> None:
        """Validate and open a tournament revision stream."""
        del event
        if not settings.KORFBAL_SSE_ENABLED:
            await self._reject(404, "SSE is disabled.")
            return
        origin = self._origin()
        if origin and not self._origin_allowed(origin):
            await self._reject(403, "Origin is not allowed.")
            return
        try:
            self.tournament_ids = self._parse_ids()
        except ValueError as exc:
            await self._reject(400, str(exc))
            return
        for tournament_id in self.tournament_ids:
            await self.channel_layer.group_add(
                tournament_group_name(tournament_id), self.channel_name
            )
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
        await self._send_event("ready", {"revisions": await self._revisions()})
        self.heartbeat_task = asyncio.create_task(self._heartbeats())

    async def http_disconnect(self, event: dict[str, object]) -> None:
        """Release subscriptions after a receiver or browser disconnects.

        Raises:
            StopConsumer: Always, after subscription cleanup.

        """
        del event
        await self._cleanup()
        raise StopConsumer

    async def tournament_changed(self, event: dict[str, object]) -> None:
        """Forward a committed tournament invalidation."""
        await self._send_event(
            "tournament.changed",
            {
                "tournament_id": event["tournament_id"],
                "revision": event["revision"],
            },
        )

    async def _send_event(self, name: str, payload: object) -> None:
        body = f"event: {name}\ndata: {json.dumps(payload, separators=(',', ':'))}\n\n"
        await self.send({
            "type": "http.response.body",
            "body": body.encode(),
            "more_body": True,
        })

    async def _heartbeats(self) -> None:
        try:
            while True:
                await asyncio.sleep(settings.KORFBAL_SSE_HEARTBEAT_SECONDS)
                await self.send({
                    "type": "http.response.body",
                    "body": b": heartbeat\n\n",
                    "more_body": True,
                })
        except asyncio.CancelledError:
            return

    async def _cleanup(self) -> None:
        if self.heartbeat_task:
            self.heartbeat_task.cancel()
            self.heartbeat_task = None
        for tournament_id in self.tournament_ids:
            await self.channel_layer.group_discard(
                tournament_group_name(tournament_id), self.channel_name
            )
        self.tournament_ids = ()

    async def _reject(self, status_code: int, detail: str) -> None:
        await self.send({
            "type": "http.response.start",
            "status": status_code,
            "headers": [(b"content-type", b"application/json")],
        })
        await self.send({
            "type": "http.response.body",
            "body": json.dumps({"detail": detail}).encode(),
            "more_body": False,
        })
        raise StopConsumer

    def _parse_ids(self) -> tuple[str, ...]:
        params = parse_qs(self.scope.get("query_string", b"").decode())
        values = tuple(
            dict.fromkeys(
                item.strip()
                for item in params.get("tournament_ids", [""])[0].split(",")
                if item.strip()
            )
        )
        if not values:
            raise ValueError("At least one tournament_id is required.")
        if len(values) > MAX_TOURNAMENT_STREAMS:
            raise ValueError("Too many tournament_ids were requested.")
        try:
            return tuple(str(UUID(value)) for value in values)
        except ValueError as exc:
            raise ValueError("Every tournament_id must be a valid UUID.") from exc

    def _origin(self) -> str | None:
        headers = dict(self.scope.get("headers", []))
        value = headers.get(b"origin")
        return value.decode("ascii") if value else None

    @staticmethod
    def _origin_allowed(origin: str) -> bool:
        return (
            settings.CORS_ALLOW_ALL_ORIGINS or origin in settings.CORS_ALLOWED_ORIGINS
        )

    @database_sync_to_async
    def _revisions(self) -> dict[str, int]:
        return {
            str(tournament_id): revision
            for tournament_id, revision in Tournament.objects.filter(
                id_uuid__in=self.tournament_ids
            ).values_list("id_uuid", "live_revision")
        }
