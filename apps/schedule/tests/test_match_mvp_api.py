"""Tests for match MVP voting endpoints."""

from __future__ import annotations

from datetime import timedelta
from http import HTTPStatus
from typing import Any, cast
from uuid import uuid4

from django.contrib.auth import get_user_model
from django.contrib.auth.base_user import AbstractBaseUser
from django.test.client import Client
from django.utils import timezone
import pytest

from apps.awards.models import MatchMvp
from apps.game_tracker.models import MatchPlayer
from apps.game_tracker.tests.tracker_test_helpers import (
    TrackerMatchContext,
    create_match_part,
    create_tracker_match,
)
from apps.player.models.player import Player


pytestmark = pytest.mark.django_db
UNKNOWN_CANDIDATE_ID = "00000000-0000-4000-8000-000000000003"


def _match(*, finished: bool = False) -> TrackerMatchContext:
    tracker = create_tracker_match(prefix=str(uuid4()))
    if finished:
        tracker.match_data.status = "finished"
        tracker.match_data.save(update_fields=["status"])
    return tracker


def _open_voting() -> TrackerMatchContext:
    tracker = _match(finished=True)
    create_match_part(
        match_data=tracker.match_data,
        active=False,
        start_offset=-timedelta(minutes=32),
        end_offset=-timedelta(minutes=2),
    )
    return tracker


def _user(username: str) -> AbstractBaseUser:
    user_model = cast(Any, get_user_model())
    return cast(AbstractBaseUser, user_model.objects.create_user(username=username))


def _player(username: str) -> Player:
    return cast(Player, cast(Any, _user(username)).player)


def _candidates(tracker: TrackerMatchContext) -> tuple[Player, Player]:
    candidates = (_player(f"alice-{uuid4()}"), _player(f"bob-{uuid4()}"))
    MatchPlayer.objects.create(
        match_data=tracker.match_data,
        team=tracker.home_team,
        player=candidates[0],
    )
    MatchPlayer.objects.create(
        match_data=tracker.match_data,
        team=tracker.away_team,
        player=candidates[1],
    )
    return candidates


def _url(tracker: TrackerMatchContext, *, vote: bool = False) -> str:
    suffix = "vote/" if vote else ""
    return f"/api/matches/{tracker.match.id_uuid}/mvp/{suffix}"


def _close_voting(tracker: TrackerMatchContext) -> None:
    mvp = MatchMvp.objects.get(match=tracker.match)
    mvp.finished_at = timezone.now() - timedelta(hours=9)
    mvp.closes_at = mvp.finished_at + timedelta(hours=3)
    mvp.save(update_fields=["finished_at", "closes_at", "updated_at"])


def test_mvp_status_unavailable_before_finished(client: Client) -> None:
    """MVP status remains unavailable until the match finishes."""
    response = client.get(_url(_match()))

    assert response.status_code == HTTPStatus.OK
    payload = response.json()
    assert payload["available"] is False
    assert payload["open"] is False
    assert payload["published_at"] is None
    assert payload["vote_breakdown"] == []


def test_mvp_vote_returns_conflict_when_match_not_finished(client: Client) -> None:
    """Voting before the match finishes returns a conflict."""
    tracker = _match()
    response = client.post(
        _url(tracker, vote=True),
        data={"candidate_id_uuid": "does-not-matter"},
        content_type="application/json",
    )

    assert response.status_code == HTTPStatus.CONFLICT
    assert response.json() == {
        "detail": "Voting is only available after the match is finished.",
    }


@pytest.mark.parametrize(
    ("data", "detail"),
    [
        pytest.param("[]", "Invalid JSON body.", id="non-object-json"),
        pytest.param({}, "Missing 'candidate_id_uuid'.", id="missing-candidate"),
        pytest.param(
            {"candidate_id_uuid": UNKNOWN_CANDIDATE_ID},
            "Unknown candidate.",
            id="unknown-candidate",
        ),
    ],
)
def test_mvp_vote_rejects_invalid_candidate_input(
    client: Client,
    data: str | dict[str, str],
    detail: str,
) -> None:
    """Malformed or invalid candidate input returns the specific client error."""
    tracker = _match(finished=True)
    response = client.post(
        _url(tracker, vote=True), data=data, content_type="application/json"
    )

    assert response.status_code == HTTPStatus.BAD_REQUEST
    assert response.json() == {"detail": detail}


def test_mvp_vote_rejects_candidate_not_in_match(client: Client) -> None:
    """An existing player outside the match roster cannot receive a vote."""
    tracker = _match(finished=True)
    outsider = _player(f"outsider-{uuid4()}")

    response = client.post(
        _url(tracker, vote=True),
        data={"candidate_id_uuid": str(outsider.id_uuid)},
        content_type="application/json",
    )

    assert response.status_code == HTTPStatus.CONFLICT
    assert response.json() == {"detail": "Invalid MVP candidate."}


def test_mvp_vote_flow_and_publish_after_close(client: Client) -> None:
    """An authenticated vote persists and publishes when voting closes."""
    tracker = _open_voting()
    candidate, _ = _candidates(tracker)
    client.force_login(_user("voter"))

    response = client.get(_url(tracker))
    assert response.status_code == HTTPStatus.OK
    payload = response.json()
    assert payload["available"] is True
    assert payload["open"] is True
    assert payload["user_vote"] is None
    assert any(
        item["id_uuid"] == str(candidate.id_uuid) for item in payload["candidates"]
    )

    response = client.post(
        _url(tracker, vote=True),
        data={"candidate_id_uuid": str(candidate.id_uuid)},
        content_type="application/json",
    )
    assert response.status_code == HTTPStatus.OK
    payload = response.json()
    assert payload["user_vote"]["candidate_id_uuid"] == str(candidate.id_uuid)
    assert any(
        (line.get("candidate") or {}).get("id_uuid") == str(candidate.id_uuid)
        and line.get("votes") == 1
        for line in payload["vote_breakdown"]
    )

    _close_voting(tracker)
    response = client.get(_url(tracker))
    assert response.status_code == HTTPStatus.OK
    payload = response.json()
    assert payload["open"] is False
    assert payload["published_at"] is not None
    assert payload["mvp"]["id_uuid"] == str(candidate.id_uuid)


def test_mvp_anonymous_vote_persists_via_cookie(client: Client) -> None:
    """An anonymous voter can cast and change the cookie-backed vote."""
    tracker = _open_voting()
    candidate_a, candidate_b = _candidates(tracker)

    response = client.get(_url(tracker))
    assert response.status_code == HTTPStatus.OK
    payload = response.json()
    assert payload["available"] is True
    assert payload["open"] is True
    assert payload["user_vote"] is None

    response = client.post(
        _url(tracker, vote=True),
        data={"candidate_id_uuid": str(candidate_a.id_uuid)},
        content_type="application/json",
    )
    assert response.status_code == HTTPStatus.OK
    assert response.json()["user_vote"]["candidate_id_uuid"] == str(candidate_a.id_uuid)

    response = client.get(_url(tracker))
    assert response.status_code == HTTPStatus.OK
    assert response.json()["user_vote"]["candidate_id_uuid"] == str(candidate_a.id_uuid)

    response = client.post(
        _url(tracker, vote=True),
        data={"candidate_id_uuid": str(candidate_b.id_uuid)},
        content_type="application/json",
    )
    assert response.status_code == HTTPStatus.OK
    assert response.json()["user_vote"]["candidate_id_uuid"] == str(candidate_b.id_uuid)

    _close_voting(tracker)
    response = client.get(_url(tracker))
    assert response.status_code == HTTPStatus.OK
    payload = response.json()
    assert payload["open"] is False
    assert payload["published_at"] is not None
    assert payload["mvp"]["id_uuid"] == str(candidate_b.id_uuid)
