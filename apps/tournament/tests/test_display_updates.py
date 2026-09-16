"""Public-only versioned tournament display publications."""

from __future__ import annotations

from copy import deepcopy
import json
from unittest.mock import AsyncMock, patch

from django.contrib.auth import get_user_model
from django.core.cache import caches
from django.utils import timezone
import pytest

from apps.tournament.adapters.outbound.display_updates import build_display_update
from apps.tournament.adapters.outbound.realtime import ChannelsTournamentChangePublisher
from apps.tournament.models import (
    Tournament,
    TournamentMatch,
    TournamentPool,
    TournamentPoolEntry,
    TournamentStage,
    TournamentTeam,
)
from apps.tournament.services.snapshot import build_tournament_snapshot


pytestmark = pytest.mark.django_db


@pytest.fixture
def tournament() -> Tournament:
    """Public multi-match fixture with standings affected by a goal."""
    owner = get_user_model().objects.create(username="display-publication")
    tournament = Tournament.objects.create(
        owner=owner,
        name="Public cup",
        slug="public-cup",
        starts_at=timezone.now(),
        status=Tournament.Status.LIVE,
        visibility=Tournament.Visibility.PUBLIC,
    )
    stage = TournamentStage.objects.create(
        tournament=tournament, name="Pools", kind=TournamentStage.Kind.POOL
    )
    pool = TournamentPool.objects.create(
        tournament=tournament, stage=stage, name="Pool A"
    )
    home, away = [
        TournamentTeam.objects.create(tournament=tournament, name=side)
        for side in ("Home", "Away")
    ]
    for team in (home, away):
        TournamentPoolEntry.objects.create(pool=pool, team=team)
    for number in range(1, 11):
        TournamentMatch.objects.create(
            tournament=tournament,
            stage=stage,
            pool=pool,
            home_team=home,
            away_team=away,
            match_number=number,
            status=TournamentMatch.Status.LIVE,
            home_score=0,
            away_score=0,
        )
    return tournament


def unpack(frame: bytes | None) -> dict:
    """Read one encoded server event."""
    assert frame is not None
    return json.loads(frame.split(b"data: ", 1)[1])


def test_goal_patch_reconstructs_complete_snapshot_and_is_smaller(
    tournament: Tournament,
) -> None:
    """One goal includes derived standings without resending all ten matches."""
    key = str(tournament.pk)
    _, first = build_display_update(key)
    baseline = unpack(first)["snapshot"]
    match = tournament.matches.order_by("match_number").first()
    assert match is not None
    match.home_score = 1
    match.revision += 1
    match.save(update_fields=["home_score", "revision"])
    tournament.live_revision = 1
    tournament.save(update_fields=["live_revision"])
    revision, frame = build_display_update(key)
    update = unpack(frame)
    assert revision == 1
    assert update["base_revision"] == 0
    reconstructed = deepcopy(baseline)
    for path, value in update["changes"]:
        target = reconstructed
        for part in path[:-1]:
            target = target[part]
        target[path[-1]] = value
    assert reconstructed == build_tournament_snapshot(tournament)
    assert len(frame or "") < len(first or "") / 4
    assert any(path[0] == "pools" for path, _value in update["changes"])
    assert str(tournament.display_token).encode() not in (frame or b"")


@pytest.mark.parametrize(
    ("status", "visibility"),
    [("draft", "public"), ("archived", "public"), ("live", "unlisted")],
)
def test_non_public_content_never_enters_the_stream(
    tournament: Tournament, status: str, visibility: str
) -> None:
    """Even an existing public checkpoint is discarded after access changes."""
    key = str(tournament.pk)
    build_display_update(key)
    tournament.status = status
    tournament.visibility = visibility
    tournament.save(update_fields=["status", "visibility"])
    with patch(
        "apps.tournament.adapters.outbound.display_updates.build_tournament_snapshot"
    ) as snapshot:
        assert build_display_update(key) == (0, None)
    snapshot.assert_not_called()
    assert caches["public_live"].get(f"tournament-display:v1:{key}") is None


def test_expired_checkpoint_and_large_snapshots_fall_back_safely(
    tournament: Tournament,
) -> None:
    """Missing state sends a replacement; oversized content sends invalidation only."""
    key = str(tournament.pk)
    build_display_update(key)
    caches["public_live"].delete(f"tournament-display:v1:{key}")
    assert "snapshot" in unpack(build_display_update(key)[1])
    with patch("apps.tournament.adapters.outbound.display_updates._MAX_BYTES", 1):
        assert build_display_update(key) == (0, None)


def test_cache_failure_still_publishes_revision(tournament: Tournament) -> None:
    """An unavailable optional cache must never swallow a committed invalidation."""
    layer = AsyncMock()
    revision = 4
    with (
        patch(
            "apps.tournament.adapters.outbound.realtime.get_channel_layer",
            return_value=layer,
        ),
        patch(
            "apps.tournament.adapters.outbound.realtime.build_display_update",
            side_effect=RuntimeError("cache offline"),
        ),
    ):
        ChannelsTournamentChangePublisher().publish(
            tournament_id=str(tournament.pk), revision=revision
        )
    event = layer.group_send.call_args.args[1]
    assert event["revision"] == revision
    assert event["display_frame"] is None
