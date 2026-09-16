"""ASGI Server-Sent Events consumer for public match invalidations."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from itertools import starmap
import json
from time import time
from typing import Any
from urllib.parse import parse_qs
from uuid import UUID

from channels.consumer import get_handler_name
from channels.db import database_sync_to_async
from channels.exceptions import StopConsumer
from django.conf import settings
from django.utils import timezone

from apps.game_tracker.adapters.outbound.compact_match import PUBLIC_RESOURCES
from apps.game_tracker.adapters.outbound.match_bootstrap import Bootstrap
from apps.game_tracker.adapters.outbound.match_fanout import (
    Fanout,
    Mailbox,
    shared_fanout,
)
from apps.game_tracker.models import MatchData
from apps.game_tracker.realtime.contracts import ALL_LIVE_RESOURCES
from apps.game_tracker.realtime.metrics import (
    SSE_ACTIVE_CONNECTIONS,
    SSE_EVENTS_SENT,
    SSE_REJECTIONS,
)


# Resolve fixed labels once; every successful socket write still increments once.
_MATCH_CHANGE_SENT = SSE_EVENTS_SENT.labels(event="match.changed")
_HEARTBEAT_SENT = SSE_EVENTS_SENT.labels(event="heartbeat")


class MatchEventsSseConsumer:
    """Multiplex public change notifications for a bounded set of matches."""

    match_ids: tuple[str, ...] = ()
    heartbeat_task: asyncio.Task[None] | None = None
    connection_counted = False

    fanout: Fanout | None = None
    mailbox: Mailbox | None = None

    async def __call__(
        self,
        scope: dict[str, Any],
        receive: Callable[[], Awaitable[dict[str, Any]]],
        send: Callable[[dict[str, Any]], Awaitable[None]],
    ) -> None:
        """Keep viewer dispatch free of per-event database and broker operations."""
        self.scope, self.base_send = scope, send
        self.subscription_ready = asyncio.Event()
        tasks = [
            asyncio.create_task(self._receive_requests(receive)),
            asyncio.create_task(self._send_changes()),
        ]
        try:
            done, _pending = await asyncio.wait(
                tasks, return_when=asyncio.FIRST_COMPLETED
            )
            for task in done:
                task.result()
        except StopConsumer:
            pass
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await self._cleanup()

    @classmethod
    def as_asgi(cls) -> Callable[..., Awaitable[None]]:
        """Create an independent connection handler for each ASGI scope."""

        async def application(
            scope: dict[str, Any],
            receive: Callable[[], Awaitable[dict[str, Any]]],
            send: Callable[[dict[str, Any]], Awaitable[None]],
        ) -> None:
            await cls()(scope, receive, send)

        return application

    async def send(self, message: dict[str, Any]) -> None:
        """Write through the ASGI server's flow-controlled send callable."""
        await self.base_send(message)

    async def dispatch(self, message: dict[str, Any]) -> None:
        """Only explicit revision reads need Django database connection cleanup."""
        await getattr(self, get_handler_name(message))(message)

    async def _receive_requests(
        self, receive: Callable[[], Awaitable[dict[str, Any]]]
    ) -> None:
        """Observe disconnects even when the socket's send is backpressured."""
        while True:
            await self.dispatch(await receive())

    async def _send_changes(self) -> None:
        """Keep one sender alive instead of scheduling a new task for each frame."""
        await self.subscription_ready.wait()
        assert self.mailbox is not None
        mailbox = self.mailbox
        while True:
            await self.match_changed(await mailbox.receive())

    async def http_request(self, event: dict[str, object]) -> None:
        """Open a validated event stream for the requested match IDs."""
        del event
        if self.fanout is not None:
            return
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
            resources = self._parse_resources()
        except ValueError as exc:
            SSE_REJECTIONS.labels(reason="request").inc()
            await self._reject(400, str(exc))
            return

        self.mailbox = Mailbox(self.match_ids, resources=resources)
        self.fanout = shared_fanout(self._current_revisions)
        await self.fanout.subscribe(self.mailbox)

        revisions = await self.fanout.current_revisions(self.match_ids)
        params = parse_qs(self.scope.get("query_string", b"").decode())
        snapshots = {}
        if params.get("snapshot") == ["1"] or resources is not None:
            cache = (
                self.fanout.bootstraps
                if resources is None
                else self.fanout.compact_bootstraps
            )
            values = await asyncio.gather(*starmap(cache.get, revisions.items()))
            snapshots = {
                match_id: value
                for match_id, value in zip(revisions, values, strict=True)
                if value is not None
            }
        await self._prepare_compact(revisions, snapshots, resources)
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
        ready: dict[str, Any] = {"revisions": revisions}
        if params.get("snapshot") == ["1"]:
            ready["snapshot_matches"] = list(snapshots)
            ready["live_states"] = {
                match_id: live
                for match_id, snapshot in snapshots.items()
                if (live := snapshot.live_state()) is not None
            }
        if resources is not None:
            live_states, frames = self._compact_start(revisions, snapshots, resources)
            # Replay bytes retain their original emission time. Anchor client
            # clocks to this connection instead of the age of a retained frame.
            ready.update(
                snapshot_matches=list(revisions),
                live_states=live_states,
                compact=1,
                server_time=time(),
            )
        else:
            frames = [snapshot.wire for snapshot in snapshots.values()]
        await self._send_event("ready", ready)
        for wire in frames:
            await self.send({
                "type": "http.response.body",
                "body": wire,
                "more_body": True,
            })
        self.subscription_ready.set()
        self.heartbeat_task = asyncio.create_task(self._send_heartbeats())

    async def _prepare_compact(
        self,
        revisions: dict[str, int],
        snapshots: dict[str, Bootstrap],
        resources: frozenset[str] | None,
    ) -> None:
        if resources is None:
            return
        assert self.fanout is not None
        for match_id, revision in revisions.items():
            snapshot = snapshots.get(match_id)
            initial = {
                "match_id": match_id,
                "revision": revision,
                "resources": sorted(ALL_LIVE_RESOURCES),
                "snapshot": True,
            }
            if snapshot is not None:
                initial = {**snapshot.event}
                live = snapshot.live_state()
                if live is not None:
                    initial["live"] = live
            await self.fanout.prepare_compact(match_id, resources, initial)

    def _compact_start(
        self,
        revisions: dict[str, int],
        snapshots: dict[str, Bootstrap],
        resources: frozenset[str],
    ) -> tuple[dict[str, Any], list[bytes]]:
        assert self.fanout is not None

        cursor = dict(self.scope.get("headers", [])).get(b"last-event-id", b"")
        frames = []
        live_states = {}
        for match_id, revision in revisions.items():
            snapshot = snapshots.get(match_id)
            codec = self.fanout.compact[match_id][resources]
            replay = codec.replay(cursor) if codec.revision >= revision else None
            if replay is not None:
                frames.extend(replay)
                # No await between reading the codec and discarding covered delivery.
                # Updates arriving during sends must remain queued.
                if self.mailbox is not None:
                    self.mailbox.discard(match_id)
                continue
            initial = {
                "match_id": match_id,
                "revision": revision,
                "resources": sorted(ALL_LIVE_RESOURCES),
                "snapshot": True,
            }
            if snapshot is not None:
                initial = {**snapshot.event}
                live = snapshot.live_state()
                if live is not None:
                    initial["live"] = live
            frames.append(
                self.fanout.seed_compact(match_id, resources, initial)["_wire"]
            )
            # This snapshot covers everything currently queued for this match.
            # Clear before the first send await so later publications stay queued.
            if self.mailbox is not None:
                self.mailbox.discard(match_id)
            live = codec.document.get("live")
            if live is not None:
                timer = live["timer"]
                if timer["type"] != "deactivated":
                    timer = {**timer, "server_time": timezone.now().isoformat()}
                live_states[match_id] = {**live, "timer": timer}
        return live_states, frames

    async def http_disconnect(self, event: dict[str, object]) -> None:
        """Release group subscriptions when the browser disconnects.

        Raises:
            StopConsumer: Always, to terminate the Channels application loop.

        """
        del event
        raise StopConsumer

    async def match_changed(self, event: dict[str, object]) -> None:
        """Forward a Channels group notification as an SSE event."""
        await self.send({
            "type": "http.response.body",
            "body": event["_wire"],
            "more_body": True,
        })
        _MATCH_CHANGE_SENT.inc()

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
                        "body": b":\n\n",
                        "more_body": True,
                    },
                )
                _HEARTBEAT_SENT.inc()
        except asyncio.CancelledError:
            return

    async def _cleanup(self) -> None:
        if self.connection_counted:
            SSE_ACTIVE_CONNECTIONS.dec()
            self.connection_counted = False
        if self.heartbeat_task:
            self.heartbeat_task.cancel()
            await asyncio.gather(self.heartbeat_task, return_exceptions=True)
            self.heartbeat_task = None
        if self.fanout is not None and self.mailbox is not None:
            fanout, self.fanout = self.fanout, None
            await fanout.unsubscribe(self.mailbox)
            self.mailbox = None
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

    def _parse_resources(self) -> frozenset[str] | None:
        params = parse_qs(self.scope.get("query_string", b"").decode())
        if "compact" not in params:
            return None
        if params["compact"] != ["1"]:
            raise ValueError("Unsupported compact SSE version.")
        values = params.get("resources", [",".join(sorted(PUBLIC_RESOURCES))])
        if len(values) != 1:
            raise ValueError("One public resource list is required.")
        resources = frozenset(values[0].split(","))
        if not resources <= PUBLIC_RESOURCES:
            raise ValueError("Unknown public resource subscription.")
        return resources

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
