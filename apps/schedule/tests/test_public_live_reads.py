"""Public live reads retain tracker semantics without preparing coach data."""

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from http import HTTPStatus
from typing import Any, cast
from unittest.mock import patch

from django.core.cache import caches
from django.db import connection, connections, transaction
from django.test.client import Client
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
import pytest

from apps.game_tracker.models import MatchData, Pause, Shot
from apps.game_tracker.services import public_live
from apps.game_tracker.services.live_update_signal_control import (
    suppress_tracker_delete_side_effects,
)
from apps.game_tracker.services.tracker_state import get_tracker_state
from apps.schedule.api.views import MatchViewSet
from apps.schedule.models import Match

from .match_api_test_support import (
    MatchGraph,
    create_match_graph,
    create_match_part,
    create_user,
)


pytestmark = pytest.mark.django_db
MAX_PUBLIC_LIVE_SELECTS = 10
UNCHANGED_PUBLIC_LIVE_SELECTS = 1
PUBLIC_KEYS = {
    "match_id",
    "match_data_id",
    "status",
    "current_part",
    "parts",
    "paused",
    "timer",
    "score",
    "last_changed_at",
    "live_revision",
}


def _live_graph(*, status: str = "active", paused: bool = False) -> MatchGraph:
    graph = create_match_graph(prefix="Public live read")
    if status != "upcoming":
        part = create_match_part(graph)
        start = timezone.now() - timedelta(seconds=45)
        Pause.objects.create(
            match_data=graph.match_data,
            match_part=part,
            start_time=start,
            end_time=start + timedelta(seconds=15),
            active=False,
        )
        if paused:
            Pause.objects.create(
                match_data=graph.match_data,
                match_part=part,
                start_time=timezone.now() - timedelta(seconds=5),
                active=True,
            )
    player = cast(Any, create_user(username="public-live-player")).player
    Shot.objects.bulk_create([
        Shot(match_data=graph.match_data, team=team, player=player, scored=True)
        for team in (graph.home_team, graph.home_team, graph.away_team)
    ])
    MatchData.objects.filter(pk=graph.match_data.pk).update(status=status)
    graph.match_data.refresh_from_db()
    return graph


@pytest.mark.parametrize("endpoint", ["live", "live/poll"])
def test_public_live_reads_do_not_query_private_tracker_data(
    client: Client,
    endpoint: str,
) -> None:
    """A small public snapshot must not prepare roster, audio or coach controls."""
    graph = _live_graph()
    with CaptureQueriesContext(connection) as captured:
        response = client.get(
            f"/api/matches/{graph.match.id_uuid}/{endpoint}/", {"since_revision": 0}
        )
    assert response.status_code == HTTPStatus.OK
    assert response.json()["score"] == {"home": 2, "away": 1}
    selects = [row["sql"] for row in captured if row["sql"].startswith("SELECT")]
    assert len(selects) <= MAX_PUBLIC_LIVE_SELECTS
    for sql in selects:
        assert not any(
            table in sql
            for table in (
                "game_tracker_playergroup",
                "game_tracker_playerchange",
                "game_tracker_timeout",
                "player_playersong",
                "game_tracker_goaltype",
            )
        )


@pytest.mark.parametrize("endpoint", ["live", "live/poll"])
@pytest.mark.parametrize(
    ("status", "paused", "source"),
    [
        ("upcoming", False, "tracker"),
        ("active", False, "tracker"),
        ("active", True, "tracker"),
        ("finished", False, "tracker"),
        ("finished", False, "knkv"),
        ("finished", False, "archive"),
    ],
)
def test_public_live_fields_match_the_tracker_clock_and_score(
    client: Client,
    endpoint: str,
    status: str,
    paused: bool,
    source: str,
) -> None:
    """Keep pause accounting and imported final-score precedence identical."""
    graph = _live_graph(status=status, paused=paused)
    MatchData.objects.filter(pk=graph.match_data.pk).update(
        score_source=source,
        home_score=8,
        away_score=6,
    )
    reference = get_tracker_state(graph.match, team=graph.home_team)
    response = client.get(
        f"/api/matches/{graph.match.id_uuid}/{endpoint}/", {"since_revision": -1}
    )
    assert response.status_code == HTTPStatus.OK
    payload = response.json()
    expected_keys = PUBLIC_KEYS | (
        {"resources"} if endpoint.endswith("poll") else set()
    )
    assert set(payload) == expected_keys
    for field in PUBLIC_KEYS - {"score", "timer"}:
        assert payload[field] == reference[field]
    assert payload["score"] == {
        "home": reference["score"]["for"],
        "away": reference["score"]["against"],
    }
    if "server_time" in reference["timer"]:
        assert payload["timer"].pop("server_time")
        reference["timer"].pop("server_time")
    assert payload["timer"] == reference["timer"]


@pytest.mark.parametrize("endpoint", ["live", "live/poll"])
def test_public_live_does_not_mix_old_metadata_with_a_new_tracker_snapshot(
    client: Client,
    endpoint: str,
) -> None:
    """A write after detail lookup must not leave an older status/revision label."""
    graph = _live_graph()
    next_revision = graph.match_data.live_revision + 1
    original = MatchViewSet.get_object

    def get_then_update(view: MatchViewSet) -> Match:
        match = original(view)
        MatchData.objects.filter(pk=graph.match_data.pk).update(
            status="finished",
            score_source="knkv",
            home_score=8,
            away_score=6,
            live_revision=next_revision,
        )
        return match

    with patch.object(MatchViewSet, "get_object", get_then_update):
        response = client.get(
            f"/api/matches/{graph.match.id_uuid}/{endpoint}/",
            {"since_revision": -1, "format": "json"},
        )
    assert response.status_code == HTTPStatus.OK
    payload = response.json()
    assert payload["status"] == "finished"
    assert payload["live_revision"] == next_revision
    assert payload["score"] == {"home": 8, "away": 6}


@pytest.mark.django_db(transaction=True)
@pytest.mark.skipif(connection.vendor != "postgresql", reason="PostgreSQL MVCC")
@pytest.mark.parametrize("endpoint", ["live", "live/poll"])
def test_public_live_snapshot_does_not_lock_or_mix_concurrent_writes(
    client: Client,
    endpoint: str,
) -> None:
    """A writer can commit during a read without changing that read's revision."""
    graph = _live_graph()
    revision = graph.match_data.live_revision
    player_id = (
        Shot.objects
        .filter(match_data=graph.match_data)
        .values_list("player_id", flat=True)
        .first()
    )
    assert player_id is not None
    original_snapshot = public_live._build_public_snapshot
    caches["public_live"].clear()

    def write_goal() -> None:
        try:
            with transaction.atomic():
                with connections["default"].cursor() as cursor:
                    cursor.execute("SET LOCAL lock_timeout = '2s'")
                Shot.objects.bulk_create([
                    Shot(
                        match_data_id=graph.match_data.pk,
                        player_id=player_id,
                        team_id=graph.home_team.pk,
                        scored=True,
                    )
                ])
                MatchData.objects.filter(pk=graph.match_data.pk).update(
                    live_revision=revision + 1,
                )
        finally:
            connections["default"].close()

    interleaved = False

    def snapshot_after_write(match_data: MatchData) -> dict[str, Any]:
        nonlocal interleaved
        # The writer also publishes a snapshot. Interleave once so publication
        # cannot recursively trigger another write through this patched helper.
        if not interleaved:
            interleaved = True
            with ThreadPoolExecutor(max_workers=1) as executor:
                executor.submit(write_goal).result(timeout=10)
        return original_snapshot(match_data)

    with patch.object(public_live, "_build_public_snapshot", snapshot_after_write):
        response = client.get(f"/api/matches/{graph.match.id_uuid}/{endpoint}/")
    assert response.status_code == HTTPStatus.OK
    assert response.json()["live_revision"] == revision
    assert response.json()["score"] == {"home": 2, "away": 1}
    assert interleaved
    next_response = client.get(f"/api/matches/{graph.match.id_uuid}/live/")
    assert next_response.json()["live_revision"] == revision + 1
    assert next_response.json()["score"] == {"home": 3, "away": 1}


def test_unchanged_public_poll_skips_clock_and_score_queries(client: Client) -> None:
    """Idle polling only reads the current revision and timestamp."""
    graph = _live_graph()
    with (
        patch.object(public_live, "_build_public_snapshot") as snapshot,
        CaptureQueriesContext(connection) as captured,
    ):
        response = client.get(
            f"/api/matches/{graph.match.id_uuid}/live/poll/",
            {"since_revision": graph.match_data.live_revision, "timeout": 3},
        )
    assert response.status_code == HTTPStatus.OK
    payload = response.json()
    assert set(payload) == {
        "changed",
        "server_time",
        "last_changed_at",
        "live_revision",
    }
    assert payload["changed"] is False
    assert payload["live_revision"] == graph.match_data.live_revision
    assert payload["last_changed_at"] == graph.match_data.live_changed_at.isoformat()
    snapshot.assert_not_called()
    assert len(captured) == UNCHANGED_PUBLIC_LIVE_SELECTS
    assert all(row["sql"].startswith("SELECT") for row in captured)


def test_public_live_handles_missing_tracker_data_and_invalid_cursors(
    client: Client,
) -> None:
    """Missing tracker state stays empty; malformed cursors still return JSON 400."""
    graph = create_match_graph(prefix="Missing public live")
    with suppress_tracker_delete_side_effects():
        graph.match_data.delete()
    url = f"/api/matches/{graph.match.id_uuid}/live/"
    snapshot = client.get(url)
    assert snapshot.status_code == HTTPStatus.OK
    assert snapshot.content == b""
    response = client.get(f"{url}poll/", {"since_revision": -1})
    assert response.status_code == HTTPStatus.OK
    assert response.json()["changed"] is False
    assert response.json()["live_revision"] == 0
    for cursor in ("invalid", "-2"):
        invalid = client.get(f"{url}poll/", {"since_revision": cursor})
        assert invalid.status_code == HTTPStatus.BAD_REQUEST
        assert invalid.json() == {
            "detail": "Invalid 'since_revision'.",
            "message": "Invalid 'since_revision'.",
            "code": "bad_request",
        }
