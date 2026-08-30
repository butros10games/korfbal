"""Audit coverage for schedule API, query, and model contracts."""

from __future__ import annotations

from datetime import date, timedelta
from http import HTTPStatus
from typing import Any, NamedTuple, cast

from django.test.client import Client
from django.utils import timezone
import pytest

from apps.club.models import Club
from apps.game_tracker.models import MatchData, Shot
from apps.schedule.models import Match, Season, SeasonPool
from apps.schedule.queries.seasons import (
    current_season,
    most_recent_season,
    requested_or_default_season,
    season_options_payload,
)
from apps.team.models import Team

from .match_api_test_support import create_user


pytestmark = pytest.mark.django_db
EXPECTED_AGGREGATE_COUNT = 2


class ScheduleGraph(NamedTuple):
    """Minimal reusable schedule graph for outward-contract tests."""

    season: Season
    home: Team
    away: Team


def _schedule_graph(*, prefix: str) -> ScheduleGraph:
    today = timezone.localdate()
    season = Season.objects.create(
        name=f"{prefix} season",
        start_date=today - timedelta(days=30),
        end_date=today + timedelta(days=30),
    )
    home = Team.objects.create(
        name=f"{prefix} home",
        club=Club.objects.create(name=f"{prefix} home club"),
    )
    away = Team.objects.create(
        name=f"{prefix} away",
        club=Club.objects.create(name=f"{prefix} away club"),
    )
    return ScheduleGraph(season, home, away)


def _match(
    graph: ScheduleGraph,
    *,
    hours_from_now: int = 0,
    status: str = "upcoming",
) -> Match:
    match = Match.objects.create(
        season=graph.season,
        home_team=graph.home,
        away_team=graph.away,
        start_time=timezone.now() + timedelta(hours=hours_from_now),
    )
    match_data = MatchData.objects.get(match_link=match)
    match_data.status = status
    match_data.save(update_fields=["status"])
    return match


def _login_staff(client: Client, *, username: str) -> None:
    staff = create_user(username=username)
    cast(Any, staff).is_staff = True
    staff.save(update_fields=["is_staff"])
    client.force_login(staff)


def test_match_reads_are_public_but_writes_require_staff(client: Client) -> None:
    """Schedule visibility must not accidentally grant editor capabilities."""
    graph = _schedule_graph(prefix="permission")
    match = _match(graph)
    payload = {
        "season_id": str(graph.season.id_uuid),
        "home_team_id": str(graph.home.id_uuid),
        "away_team_id": str(graph.away.id_uuid),
        "start_time": timezone.now().isoformat(),
    }

    assert client.get("/api/matches/").status_code == HTTPStatus.OK
    assert client.get(f"/api/matches/{match.id_uuid}/").status_code == HTTPStatus.OK
    assert (
        client.post(
            "/api/matches/", payload, content_type="application/json"
        ).status_code
        == HTTPStatus.UNAUTHORIZED
    )

    viewer = create_user(username="schedule_audit_viewer")
    client.force_login(viewer)
    assert (
        client.patch(
            f"/api/matches/{match.id_uuid}/",
            {"start_time": timezone.now().isoformat()},
            content_type="application/json",
        ).status_code
        == HTTPStatus.FORBIDDEN
    )


def test_match_list_accepts_repeated_club_filters_for_either_side(
    client: Client,
) -> None:
    """Repeated club filters are an OR across both participating sides."""
    first = _schedule_graph(prefix="first-filter")
    second = _schedule_graph(prefix="second-filter")
    excluded = _schedule_graph(prefix="excluded-filter")
    first_match = _match(first)
    second_match = _match(second)
    _match(excluded)

    response = client.get(
        "/api/matches/",
        {
            "club": [
                str(first.home.club.id_uuid),
                str(second.away.club.id_uuid),
            ]
        },
    )

    assert response.status_code == HTTPStatus.OK
    assert {item["id_uuid"] for item in response.json()} == {
        str(first_match.id_uuid),
        str(second_match.id_uuid),
    }


def test_finished_only_returns_past_finished_matches_and_enforces_minimum_limit(
    client: Client,
) -> None:
    """The public results feed excludes future and non-finished tracker rows."""
    graph = _schedule_graph(prefix="finished-audit")
    older = _match(graph, hours_from_now=-3, status="finished")
    latest = _match(graph, hours_from_now=-1, status="finished")
    _match(graph, hours_from_now=-2, status="active")
    _match(graph, hours_from_now=1, status="finished")

    response = client.get("/api/matches/finished/", {"limit": "0"})

    assert response.status_code == HTTPStatus.OK
    assert [item["id_uuid"] for item in response.json()] == [str(latest.id_uuid)]
    assert str(older.id_uuid) not in {item["id_uuid"] for item in response.json()}


def test_recent_only_returns_matches_that_were_played(client: Client) -> None:
    """The recent feed excludes upcoming fixtures even inside its date window."""
    graph = _schedule_graph(prefix="recent-audit")
    played = _match(graph, hours_from_now=-1, status="finished")
    _match(graph, hours_from_now=1, status="upcoming")
    _match(graph, hours_from_now=2, status="active")

    response = client.get("/api/matches/recent/")

    assert response.status_code == HTTPStatus.OK
    assert [item["id_uuid"] for item in response.json()] == [str(played.id_uuid)]


def test_match_partial_updates_revalidate_existing_pool_constraints(
    client: Client,
) -> None:
    """Changing one side of a pooled match must re-check retained relations."""
    graph = _schedule_graph(prefix="partial-pool")
    outsider = Team.objects.create(name="outsider", club=graph.home.club)
    other_season = Season.objects.create(
        name="partial-pool other season",
        start_date=graph.season.start_date,
        end_date=graph.season.end_date,
    )
    pool = SeasonPool.objects.create(season=graph.season, name="A")
    pool.teams.set([graph.home, graph.away])
    match = Match.objects.create(
        season=graph.season,
        pool=pool,
        home_team=graph.home,
        away_team=graph.away,
        start_time=timezone.now(),
    )
    _login_staff(client, username="partial_pool_staff")

    wrong_team = client.patch(
        f"/api/matches/{match.id_uuid}/",
        {"away_team_id": str(outsider.id_uuid)},
        content_type="application/json",
    )
    wrong_season = client.patch(
        f"/api/matches/{match.id_uuid}/",
        {"season_id": str(other_season.id_uuid)},
        content_type="application/json",
    )

    assert wrong_team.status_code == HTTPStatus.BAD_REQUEST
    assert wrong_team.json() == {
        "away_team_id": ["Team must belong to the selected pool."]
    }
    assert wrong_season.status_code == HTTPStatus.BAD_REQUEST
    assert wrong_season.json() == {
        "pool_id": ["Pool must belong to the selected season."]
    }


def test_pool_editor_rejects_weak_identity_and_membership_changes(
    client: Client,
) -> None:
    """Pool writes enforce minimum size, stable season, and casefolded names."""
    graph = _schedule_graph(prefix="pool-audit")
    other_season = Season.objects.create(
        name="pool-audit other season",
        start_date=graph.season.start_date,
        end_date=graph.season.end_date,
    )
    extra = Team.objects.create(name="pool extra", club=graph.home.club)
    pool = SeasonPool.objects.create(season=graph.season, name="Poule A")
    pool.teams.set([graph.home, graph.away])
    _login_staff(client, username="pool_contract_staff")

    too_small = client.post(
        "/api/seasons/pools/",
        {
            "season_id": str(graph.season.id_uuid),
            "name": "Poule B",
            "team_ids": [str(extra.id_uuid)],
        },
        content_type="application/json",
    )
    duplicate_name = client.post(
        "/api/seasons/pools/",
        {
            "season_id": str(graph.season.id_uuid),
            "name": "pOuLe A",
            "team_ids": [str(graph.home.id_uuid), str(extra.id_uuid)],
        },
        content_type="application/json",
    )
    moved = client.patch(
        f"/api/seasons/pools/{pool.id_uuid}/",
        {"season_id": str(other_season.id_uuid)},
        content_type="application/json",
    )
    deleted = client.delete(f"/api/seasons/pools/{pool.id_uuid}/")

    assert too_small.status_code == HTTPStatus.BAD_REQUEST
    assert "team_ids" in too_small.json()
    assert duplicate_name.status_code == HTTPStatus.BAD_REQUEST
    assert "name" in duplicate_name.json()
    assert moved.status_code == HTTPStatus.BAD_REQUEST
    assert "season_id" in moved.json()
    assert deleted.status_code == HTTPStatus.METHOD_NOT_ALLOWED
    assert SeasonPool.objects.filter(pk=pool.pk).exists()


def test_pool_editor_requires_two_distinct_teams(client: Client) -> None:
    """Repeating one team ID must not satisfy the two-team minimum."""
    graph = _schedule_graph(prefix="distinct-pool")
    _login_staff(client, username="distinct_pool_staff")

    response = client.post(
        "/api/seasons/pools/",
        {
            "season_id": str(graph.season.id_uuid),
            "name": "Poule A",
            "team_ids": [str(graph.home.id_uuid), str(graph.home.id_uuid)],
        },
        content_type="application/json",
    )

    assert response.status_code == HTTPStatus.BAD_REQUEST
    assert "team_ids" in response.json()
    assert not SeasonPool.objects.filter(season=graph.season).exists()


def test_pool_filter_rejects_a_malformed_season_id(client: Client) -> None:
    """Invalid public input returns a controlled client error, never a server error."""
    _schedule_graph(prefix="malformed-pool-filter")
    _login_staff(client, username="malformed_pool_filter_staff")

    response = client.get("/api/seasons/pools/", {"season": "not-a-uuid"})

    assert response.status_code == HTTPStatus.BAD_REQUEST


def test_season_lists_are_ordered_and_report_distinct_counts(client: Client) -> None:
    """Editor options expose deterministic ordering and non-multiplied totals."""
    today = timezone.localdate()
    older = Season.objects.create(
        name="counted older",
        start_date=today - timedelta(days=400),
        end_date=today - timedelta(days=200),
    )
    graph = _schedule_graph(prefix="counted current")
    first_pool = SeasonPool.objects.create(season=graph.season, name="A")
    second_pool = SeasonPool.objects.create(season=graph.season, name="B")
    first_pool.teams.set([graph.home, graph.away])
    second_pool.teams.set([graph.home, graph.away])
    _match(graph, hours_from_now=1)
    _match(graph, hours_from_now=2)
    _login_staff(client, username="season_count_staff")

    response = client.get("/api/seasons/")

    assert response.status_code == HTTPStatus.OK
    payload = response.json()
    assert [item["id_uuid"] for item in payload] == [
        str(graph.season.id_uuid),
        str(older.id_uuid),
    ]
    assert payload[0]["match_count"] == EXPECTED_AGGREGATE_COUNT
    assert payload[0]["pool_count"] == EXPECTED_AGGREGATE_COUNT


def test_season_query_helpers_prefer_scoped_current_then_first_option() -> None:
    """Shared overview selection never leaks an unscoped current season."""
    today = timezone.localdate()
    completed = Season.objects.create(
        name="query completed",
        start_date=today - timedelta(days=100),
        end_date=today - timedelta(days=20),
    )
    current = Season.objects.create(
        name="query current",
        start_date=today - timedelta(days=1),
        end_date=today + timedelta(days=1),
    )
    future = Season.objects.create(
        name="query future",
        start_date=today + timedelta(days=20),
        end_date=today + timedelta(days=100),
    )

    assert current_season() == current
    assert most_recent_season() == completed
    assert (
        requested_or_default_season(str(completed.id_uuid), [future, current])
        == current
    )
    assert requested_or_default_season(None, [future, completed]) == future
    assert requested_or_default_season(None, []) is None
    assert season_options_payload([completed, current]) == [
        {
            "id_uuid": str(completed.id_uuid),
            "name": completed.name,
            "start_date": completed.start_date.isoformat(),
            "end_date": completed.end_date.isoformat(),
            "is_current": False,
        },
        {
            "id_uuid": str(current.id_uuid),
            "name": current.name,
            "start_date": current.start_date.isoformat(),
            "end_date": current.end_date.isoformat(),
            "is_current": True,
        },
    ]


def test_match_final_score_counts_only_scored_shots_for_participating_teams() -> None:
    """The model score contract ignores misses and goals assigned elsewhere."""
    graph = _schedule_graph(prefix="score-audit")
    match = _match(graph, status="finished")
    match_data = MatchData.objects.get(match_link=match)
    scorer = cast(Any, create_user(username="score_audit_scorer")).player
    unrelated = Team.objects.create(name="unrelated", club=graph.home.club)

    for team, scored in (
        (graph.home, True),
        (graph.home, True),
        (graph.home, False),
        (graph.away, True),
        (unrelated, True),
        (None, True),
    ):
        Shot.objects.create(
            player=scorer,
            match_data=match_data,
            team=team,
            scored=scored,
        )

    assert match.get_final_score() == (2, 1)


def test_season_date_range_is_inclusive_at_both_boundaries() -> None:
    """One-day seasons are valid and are reported as current."""
    today = timezone.localdate()
    client = Client()
    _login_staff(client, username="inclusive_season_staff")

    response = client.post(
        "/api/seasons/",
        {
            "name": "one-day season",
            "start_date": date.isoformat(today),
            "end_date": date.isoformat(today),
        },
        content_type="application/json",
    )

    assert response.status_code == HTTPStatus.CREATED
    assert response.json()["is_current"] is True
