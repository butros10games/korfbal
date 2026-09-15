"""Audit coverage for schedule timeline and live-state HTTP boundaries."""

from __future__ import annotations

from datetime import timedelta
from http import HTTPStatus
from typing import Any, cast
from unittest.mock import patch

from django.contrib.auth.base_user import AbstractBaseUser
from django.test.client import Client
from django.utils import timezone
import pytest

from apps.kwt_common.tests.api_test_support import assert_api_error
from apps.player.models.player_club_membership import PlayerClubMembership

from .match_api_test_support import MatchGraph, create_match_graph, create_user


pytestmark = pytest.mark.django_db
PRIVATE_TRACKER_KEYS = {
    "command_sequence",
    "goal_audio",
    "goal_types",
    "last_event",
    "opponent",
    "player_groups",
    "reserve_players",
    "substitutions",
    "team",
    "timeouts",
}


def _login_home_club_member(
    client: Client,
    graph: MatchGraph,
) -> AbstractBaseUser:
    user = create_user(username="schedule-audit-poll-member")
    PlayerClubMembership.objects.create(
        player=cast(Any, user).player,
        club=graph.home_team.club,
        start_date=timezone.localdate(graph.match.start_time) - timedelta(days=1),
    )
    client.force_login(user)
    return user


def test_public_live_endpoints_strip_team_tracker_details(client: Client) -> None:
    """Public reads never build private tracker data, including on a poll change."""
    graph = create_match_graph(prefix="Public live allowlist")
    with (
        patch("apps.schedule.api.match_viewset_live.get_tracker_state") as private_read,
        patch(
            "apps.schedule.api.match_viewset_live.poll_tracker_state"
        ) as private_poll,
    ):
        full_response = client.get(f"/api/matches/{graph.match.id_uuid}/live/")
        changed_response = client.get(
            f"/api/matches/{graph.match.id_uuid}/live/poll/",
            {"since_revision": "-1", "timeout": "1"},
        )
    private_read.assert_not_called()
    private_poll.assert_not_called()
    assert full_response.status_code == HTTPStatus.OK
    assert changed_response.status_code == HTTPStatus.OK
    for payload in (full_response.json(), changed_response.json()):
        assert payload["score"] == {"home": 0, "away": 0}
        assert PRIVATE_TRACKER_KEYS.isdisjoint(payload)
    assert isinstance(changed_response.json()["resources"], list)


def test_authorized_tracker_poll_forwards_parsed_transport_options(
    client: Client,
) -> None:
    """The private long-poll adapter preserves cursor, timeout, and compact mode."""
    graph = create_match_graph(prefix="Tracker poll transport")
    _login_home_club_member(client, graph)
    expected = {"changed": False, "live_revision": 7}

    with patch(
        "apps.schedule.api.match_viewset_live.poll_tracker_state",
        return_value=expected,
    ) as poll_state:
        response = client.get(
            (
                f"/api/matches/{graph.match.id_uuid}/tracker/"
                f"{graph.home_team.id_uuid}/poll/"
            ),
            {"since_revision": "7", "timeout": "12", "compact": "1"},
        )

    assert response.status_code == HTTPStatus.OK
    assert response.json() == expected
    poll_state.assert_called_once_with(
        graph.match,
        team=graph.home_team,
        since_revision=7,
        timeout_seconds=12,
        compact=True,
    )


@pytest.mark.parametrize("endpoint", ["events", "shots"])
@pytest.mark.parametrize("cursor", ["invalid", "-1"])
def test_timeline_reads_reject_invalid_revision_before_querying(
    client: Client,
    endpoint: str,
    cursor: str,
) -> None:
    """Timeline cursors are non-negative integers at the public boundary."""
    graph = create_match_graph(prefix=f"Invalid {endpoint} cursor {cursor}")
    service_name = "read_match_events" if endpoint == "events" else "read_match_shots"

    with patch(
        f"apps.schedule.api.match_viewset_event_reads.{service_name}"
    ) as read_timeline:
        response = client.get(
            f"/api/matches/{graph.match.id_uuid}/{endpoint}/",
            {"since_revision": cursor},
        )

    assert response.status_code == HTTPStatus.BAD_REQUEST
    assert_api_error(response.json(), {"detail": "Invalid 'since_revision'."})
    read_timeline.assert_not_called()
