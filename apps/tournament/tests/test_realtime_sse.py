"""Regression coverage for reliable tournament SSE invalidations."""

from __future__ import annotations

from http import HTTPStatus
import json

from asgiref.sync import sync_to_async
from asgiref.testing import ApplicationCommunicator
from django.contrib.auth import get_user_model
from django.test import override_settings
from django.utils import timezone
import pytest

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
