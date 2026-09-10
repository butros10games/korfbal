"""Regression coverage for reliable tournament SSE invalidations."""

from __future__ import annotations

import asyncio
from http import HTTPStatus
from inspect import unwrap
import json

from asgiref.sync import sync_to_async
from asgiref.testing import ApplicationCommunicator
from channels.db import database_sync_to_async
from django.contrib.auth import get_user_model
from django.db import connection
from django.test import override_settings
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
import pytest

from apps.tournament import realtime_reconciliation
from apps.tournament.models import Tournament
from apps.tournament.realtime import TournamentEventsSseConsumer


def _sse_scope(tournament_id: str) -> dict[str, object]:
    return {
        "type": "http",
        "http_version": "1.1",
        "method": "GET",
        "scheme": "https",
        "path": "/api/tournament-live/events/",
        "raw_path": b"/api/tournament-live/events/",
        "query_string": f"tournament_ids={tournament_id}".encode(),
        "headers": [],
        "client": ("127.0.0.1", 1234),
        "server": ("testserver", 443),
    }


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
@override_settings(
    KORFBAL_SSE_ENABLED=True,
    KORFBAL_SSE_HEARTBEAT_SECONDS=60,
    KORFBAL_SSE_RECONCILE_SECONDS=0.01,
)
async def test_sse_consumer_recovers_a_missed_channel_notification() -> None:
    """A durable revision reaches an open stream even when group_send is lost."""
    owner = await sync_to_async(get_user_model().objects.create)(
        username="realtime-watchdog",
    )
    tournament = await sync_to_async(Tournament.objects.create)(
        name="Reliable live tournament",
        slug="reliable-live-tournament",
        owner=owner,
        starts_at=timezone.now(),
    )
    tournament_id = str(tournament.id_uuid)
    communicator = ApplicationCommunicator(
        TournamentEventsSseConsumer.as_asgi(),
        _sse_scope(tournament_id),
    )

    await communicator.send_input(
        {"type": "http.request", "body": b"", "more_body": False},
    )
    response_start = await communicator.receive_output(timeout=1)
    ready = await communicator.receive_output(timeout=1)

    assert response_start["status"] == HTTPStatus.OK
    assert json.loads(ready["body"].split(b"data: ", maxsplit=1)[1]) == {
        "revisions": {tournament_id: 0},
    }

    await sync_to_async(Tournament.objects.filter(pk=tournament.pk).update)(
        live_revision=1,
    )

    changed = await communicator.receive_output(timeout=1)
    assert b"event: tournament.changed" in changed["body"]
    assert json.loads(changed["body"].split(b"data: ", maxsplit=1)[1]) == {
        "tournament_id": tournament_id,
        "revision": 1,
    }

    await communicator.send_input({"type": "http.disconnect"})
    await communicator.wait(timeout=1)


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
@override_settings(
    KORFBAL_SSE_ENABLED=True,
    KORFBAL_SSE_HEARTBEAT_SECONDS=60,
    KORFBAL_SSE_RECONCILE_SECONDS=0.01,
)
async def test_one_recovery_query_serves_a_hundred_connected_viewers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """All viewers recover a lost notification from one shared SQL read."""
    owner = await sync_to_async(get_user_model().objects.create)(
        username="many-viewers"
    )
    tournament = await sync_to_async(Tournament.objects.create)(
        name="Shared recovery",
        slug="shared-recovery",
        owner=owner,
        starts_at=timezone.now(),
    )
    tournament_id = str(tournament.id_uuid)
    sync_reader = unwrap(realtime_reconciliation.read_tournament_revisions)
    query_counts: list[int] = []
    permit = asyncio.Semaphore(0)

    @database_sync_to_async
    def measured_read(ids: tuple[str, ...]) -> dict[str, int]:
        with CaptureQueriesContext(connection) as queries:
            revisions = sync_reader(ids)
        query_counts.append(len(queries))
        return revisions

    async def read(ids: tuple[str, ...]) -> dict[str, int]:
        await permit.acquire()
        return await measured_read(ids)

    monkeypatch.setattr(realtime_reconciliation, "read_tournament_revisions", read)
    viewers = [
        ApplicationCommunicator(
            TournamentEventsSseConsumer.as_asgi(),
            _sse_scope(tournament_id),
        )
        for _ in range(100)
    ]

    async def connect(viewer: ApplicationCommunicator) -> None:
        await viewer.send_input({
            "type": "http.request",
            "body": b"",
            "more_body": False,
        })
        assert (await viewer.receive_output(timeout=5))["status"] == HTTPStatus.OK
        assert b"event: ready" in (await viewer.receive_output(timeout=5))["body"]

    try:
        await asyncio.gather(*(connect(viewer) for viewer in viewers))
        await sync_to_async(Tournament.objects.filter(pk=tournament.pk).update)(
            live_revision=1
        )
        permit.release()
        changes = await asyncio.gather(
            *(viewer.receive_output(timeout=5) for viewer in viewers)
        )
        assert all(
            json.loads(event["body"].split(b"data: ", maxsplit=1)[1])
            == {
                "tournament_id": tournament_id,
                "revision": 1,
            }
            for event in changes
        )
        assert query_counts == [1]
    finally:
        await asyncio.gather(
            *(viewer.send_input({"type": "http.disconnect"}) for viewer in viewers)
        )
        await asyncio.gather(*(viewer.wait(timeout=5) for viewer in viewers))
    assert not realtime_reconciliation._workers
