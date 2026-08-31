"""Tests for the club API endpoints."""

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from http import HTTPStatus
from typing import Literal
import uuid

from django.contrib.auth.models import User
from django.test.client import Client
from django.utils import timezone
import pytest

from apps.club.models import Club
from apps.game_tracker.models import MatchData
from apps.game_tracker.models.player_match_minutes import (
    LATEST_MATCH_MINUTES_VERSION,
    PlayerMatchMinutes,
)
from apps.player.models import Player, PlayerClubMembership
from apps.schedule.models import Match, Season
from apps.team.models import Team, TeamData


EXPECTED_PLAYED_MATCHES = 3
EXPECTED_DISTANCE_TO_LOCK = 3
pytestmark = pytest.mark.django_db


@dataclass(frozen=True)
class _ClubGraph:
    season: Season
    club: Club
    opponent_team: Team


def _make_season(name: str, start_date: date, end_date: date) -> Season:
    return Season.objects.create(name=name, start_date=start_date, end_date=end_date)


def _make_club_graph(
    *,
    club_name: str,
    season_name: str = "2025/2026",
    start_date: date | None = None,
    end_date: date | None = None,
) -> _ClubGraph:
    today = timezone.localdate()
    season = _make_season(
        season_name,
        start_date or today - timedelta(days=30),
        end_date or today + timedelta(days=300),
    )
    club = Club.objects.create(name=club_name)
    opponent_club = Club.objects.create(name=f"{club_name} Opponents")
    opponent_team = Team.objects.create(name="Opponent", club=opponent_club)
    return _ClubGraph(season, club, opponent_team)


def _make_player(username: str) -> Player:
    user = User.objects.create(username=username)
    return Player.objects.select_related("user").get(user=user)


def _login_as_admin(client: Client, club: Club, username: str) -> Player:
    player = _make_player(username)
    club.admin.add(player)
    client.force_login(player.user)
    return player


def _mark_upcoming(match: Match) -> None:
    MatchData.objects.filter(match_link=match).update(status="upcoming")


def _finish_match(
    match: Match,
    *,
    players: tuple[Player, ...] = (),
    home_score: int | None = None,
    away_score: int | None = None,
) -> MatchData:
    match_data = MatchData.objects.get(match_link=match)
    match_data.status = "finished"
    update_fields = ["status"]
    if home_score is not None:
        match_data.home_score = home_score
        update_fields.append("home_score")
    if away_score is not None:
        match_data.away_score = away_score
        update_fields.append("away_score")
    match_data.save(update_fields=update_fields)
    for player in players:
        PlayerMatchMinutes.objects.update_or_create(
            match_data=match_data,
            player=player,
            algorithm_version=LATEST_MATCH_MINUTES_VERSION,
            defaults={"minutes_played": Decimal("120.0")},
        )
    return match_data


def _make_ranked_team(
    graph: _ClubGraph,
    *,
    name: str,
    rank: int,
    competition: str = "",
    players: tuple[Player, ...] = (),
) -> Team:
    team = Team.objects.create(name=name, club=graph.club)
    team_data = TeamData.objects.create(
        team=team,
        season=graph.season,
        competition=competition,
        wedstrijd_sport=True,
        team_rank=rank,
    )
    team_data.players.add(*players)
    return team


def _make_finished_match(
    graph: _ClubGraph,
    team: Team,
    start_time: datetime,
    *players: Player,
) -> Match:
    match = Match.objects.create(
        home_team=team,
        away_team=graph.opponent_team,
        season=graph.season,
        start_time=start_time,
    )
    _finish_match(match, players=players)
    return match


def test_club_overview_returns_team_and_match_payload(client: Client) -> None:
    """Return teams and match-discovered season details in the overview."""
    today = timezone.localdate()
    graph = _make_club_graph(
        club_name="Test Club",
        start_date=today,
        end_date=today + timedelta(days=365),
    )
    team = Team.objects.create(name="Test Team", club=graph.club)
    future_match = Match.objects.create(
        home_team=team,
        away_team=graph.opponent_team,
        season=graph.season,
        start_time=timezone.now() + timedelta(days=2),
    )
    past_match = Match.objects.create(
        home_team=graph.opponent_team,
        away_team=team,
        season=graph.season,
        start_time=timezone.now() - timedelta(days=4),
    )
    _mark_upcoming(future_match)
    _finish_match(past_match, home_score=15, away_score=14)

    response = client.get(f"/api/club/clubs/{graph.club.id_uuid}/overview/")

    assert response.status_code == HTTPStatus.OK
    payload = response.json()
    assert payload["club"]["id_uuid"] == str(graph.club.id_uuid)
    assert len(payload["teams"]) == 1
    assert payload["teams"][0]["name"] == team.name
    assert payload["matches"]["upcoming"][0]["status"] == "upcoming"
    assert payload["matches"]["recent"][0]["status"] == "finished"
    assert payload["meta"]["season_id"] == str(graph.season.id_uuid)
    assert payload["meta"]["season_name"] == graph.season.name
    assert payload["seasons"]
    assert payload["seasons"][0]["id_uuid"] == str(graph.season.id_uuid)


def test_club_overview_can_filter_by_season(client: Client) -> None:
    """Scope teams and matches to the explicitly selected season."""
    today = timezone.localdate()
    graph = _make_club_graph(
        club_name="Filter Club",
        start_date=today - timedelta(days=30),
        end_date=today + timedelta(days=335),
    )
    previous = _make_season(
        "2024/2025",
        today - timedelta(days=400),
        today - timedelta(days=35),
    )
    recent_team = Team.objects.create(name="Recent Team", club=graph.club)
    legacy_team = Team.objects.create(name="Legacy Team", club=graph.club)
    TeamData.objects.create(team=recent_team, season=graph.season)
    TeamData.objects.create(team=legacy_team, season=previous)
    recent_match = Match.objects.create(
        home_team=recent_team,
        away_team=graph.opponent_team,
        season=graph.season,
        start_time=timezone.now() + timedelta(days=3),
    )
    _mark_upcoming(recent_match)
    legacy_match = Match.objects.create(
        home_team=graph.opponent_team,
        away_team=legacy_team,
        season=previous,
        start_time=timezone.now() - timedelta(days=10),
    )
    _finish_match(legacy_match, home_score=18, away_score=14)

    response = client.get(
        f"/api/club/clubs/{graph.club.id_uuid}/overview/",
        {"season": str(previous.id_uuid)},
    )

    assert response.status_code == HTTPStatus.OK
    payload = response.json()
    assert payload["meta"]["season_id"] == str(previous.id_uuid)
    assert payload["meta"]["season_name"] == previous.name
    assert [team["name"] for team in payload["teams"]] == ["Legacy Team"]
    assert payload["matches"]["upcoming"] == []
    assert payload["matches"]["recent"][0]["status"] == "finished"
    assert payload["matches"]["recent"][0]["competition"] == previous.name


def test_club_overview_invalid_season_does_not_broaden(client: Client) -> None:
    """Fall back to a club season without broadening an invalid query."""
    today = timezone.localdate()
    graph = _make_club_graph(club_name="Scope Club")
    previous = _make_season(
        "2024/2025",
        today - timedelta(days=400),
        today - timedelta(days=35),
    )
    current_team = Team.objects.create(name="Current Team", club=graph.club)
    previous_team = Team.objects.create(name="Previous Team", club=graph.club)
    TeamData.objects.create(team=current_team, season=graph.season)
    TeamData.objects.create(team=previous_team, season=previous)
    current_match = Match.objects.create(
        home_team=current_team,
        away_team=graph.opponent_team,
        season=graph.season,
        start_time=timezone.now() + timedelta(days=2),
    )
    _mark_upcoming(current_match)
    old_match = Match.objects.create(
        home_team=graph.opponent_team,
        away_team=previous_team,
        season=previous,
        start_time=timezone.now() - timedelta(days=10),
    )
    _finish_match(old_match)

    response = client.get(
        f"/api/club/clubs/{graph.club.id_uuid}/overview/",
        {"season": str(uuid.uuid4())},
    )

    assert response.status_code == HTTPStatus.OK
    payload = response.json()
    assert payload["meta"]["season_id"] == str(graph.season.id_uuid)
    assert payload["meta"]["season_name"] == graph.season.name
    assert [team["name"] for team in payload["teams"]] == ["Current Team"]
    assert payload["matches"]["upcoming"]
    assert payload["matches"]["recent"] == []


@pytest.mark.parametrize(
    ("viewer_role", "expected"),
    [("anonymous", False), ("viewer", False), ("admin", True)],
)
def test_club_overview_reports_admin_status(
    client: Client,
    viewer_role: Literal["anonymous", "viewer", "admin"],
    expected: bool,
) -> None:
    """Report whether each kind of viewer is a club administrator."""
    club = Club.objects.create(name=f"{viewer_role} Club")
    if viewer_role == "viewer":
        viewer = User.objects.create(username="viewer_meta")
        client.force_login(viewer)
    elif viewer_role == "admin":
        _login_as_admin(client, club, "admin_meta")

    response = client.get(f"/api/club/clubs/{club.id_uuid}/overview/")

    assert response.status_code == HTTPStatus.OK
    assert response.json()["meta"]["viewer_is_admin"] is expected


def test_club_settings_requires_admin(client: Client) -> None:
    """Reject club settings requests from a non-admin viewer."""
    club = Club.objects.create(name="Admin Club")
    viewer = User.objects.create(username="viewer")
    client.force_login(viewer)

    response = client.get(f"/api/club/clubs/{club.id_uuid}/settings/")

    assert response.status_code == HTTPStatus.FORBIDDEN


def test_club_settings_returns_admin_roster(client: Client) -> None:
    """Return the club identity and administrator roster to an admin."""
    club = Club.objects.create(name="Admin Club")
    _login_as_admin(client, club, "club_admin")
    member = _make_player("member")
    PlayerClubMembership.objects.create(
        club=club, player=member, start_date=timezone.localdate()
    )

    response = client.get(f"/api/club/clubs/{club.id_uuid}/settings/")

    assert response.status_code == HTTPStatus.OK
    payload = response.json()
    assert payload["club"]["id_uuid"] == str(club.id_uuid)
    assert payload["admins"][0]["username"] == "club_admin"
    assert [row["player"]["username"] for row in payload["members"]] == ["member"]


def test_club_admin_can_add_membership(client: Client) -> None:
    """Create an active membership with the default local start date."""
    club = Club.objects.create(name="Membership Club")
    _login_as_admin(client, club, "club_admin")
    User.objects.create(username="member")

    response = client.post(
        f"/api/club/clubs/{club.id_uuid}/memberships/",
        data={"username": "member"},
        content_type="application/json",
    )

    assert response.status_code == HTTPStatus.CREATED
    assert response.json()["player"]["username"] == "member"
    membership = PlayerClubMembership.objects.get(
        club_id=club.id_uuid,
        player__user__username="member",
        end_date__isnull=True,
    )
    assert membership.start_date == timezone.localdate()


def test_club_admin_can_remove_membership(client: Client) -> None:
    """Close an active membership on the local date."""
    club = Club.objects.create(name="Membership Club")
    _login_as_admin(client, club, "club_admin")
    member = _make_player("member")
    membership = PlayerClubMembership.objects.create(
        player=member,
        club=club,
        start_date=timezone.localdate() - timedelta(days=1),
    )

    response = client.delete(
        f"/api/club/clubs/{club.id_uuid}/memberships/{member.id_uuid}/"
    )

    assert response.status_code == HTTPStatus.NO_CONTENT
    membership.refresh_from_db()
    assert membership.end_date == timezone.localdate()


def test_club_user_search_requires_admin(client: Client) -> None:
    """Reject user searches from a non-admin viewer."""
    club = Club.objects.create(name="Search Club")
    viewer = User.objects.create(username="viewer")
    client.force_login(viewer)

    response = client.get(
        f"/api/club/clubs/{club.id_uuid}/settings/user-search/",
        {"search": "ad"},
    )

    assert response.status_code == HTTPStatus.FORBIDDEN


def test_club_user_search_rejects_short_terms(client: Client) -> None:
    """Return no user results for a one-character search term."""
    club = Club.objects.create(name="Search Club")
    _login_as_admin(client, club, "club_admin")

    response = client.get(
        f"/api/club/clubs/{club.id_uuid}/settings/user-search/",
        {"search": "a"},
    )

    assert response.status_code == HTTPStatus.OK
    assert response.json() == {"results": []}


def test_club_user_search_returns_player_identity(client: Client) -> None:
    """Return matching usernames with their player identifiers."""
    club = Club.objects.create(name="Search Club")
    _login_as_admin(client, club, "club_admin")
    player = _make_player("member_user")

    response = client.get(
        f"/api/club/clubs/{club.id_uuid}/settings/user-search/",
        {"search": "member"},
    )

    assert response.status_code == HTTPStatus.OK
    assert response.json()["results"] == [
        {
            "user_id": player.user_id,
            "username": "member_user",
            "player_id": str(player.id_uuid),
        }
    ]


def test_club_admin_add_membership_duplicate_active_returns_400(
    client: Client,
) -> None:
    """Reject adding a player who already has an active membership."""
    club = Club.objects.create(name="Dup Membership Club")
    _login_as_admin(client, club, "club_admin")
    member = _make_player("member")
    PlayerClubMembership.objects.create(
        club=club, player=member, start_date=timezone.localdate()
    )
    url = f"/api/club/clubs/{club.id_uuid}/memberships/"

    response = client.post(
        url, data={"username": "member"}, content_type="application/json"
    )

    assert response.status_code == HTTPStatus.BAD_REQUEST
    assert (
        response.json()["detail"] == "Player is already an active member of this club."
    )


def test_club_admin_add_membership_unknown_user_returns_400(client: Client) -> None:
    """Return a validation error when the requested user is unknown."""
    club = Club.objects.create(name="Missing User Club")
    _login_as_admin(client, club, "club_admin")

    response = client.post(
        f"/api/club/clubs/{club.id_uuid}/memberships/",
        data={"username": "does_not_exist"},
        content_type="application/json",
    )

    assert response.status_code == HTTPStatus.BAD_REQUEST
    assert response.json()["detail"] == "Player/user not found."


def test_club_admin_remove_membership_missing_returns_404(client: Client) -> None:
    """Return not found when the player has no active membership."""
    club = Club.objects.create(name="Remove Missing Club")
    _login_as_admin(client, club, "club_admin")
    member = _make_player("member")

    response = client.delete(
        f"/api/club/clubs/{club.id_uuid}/memberships/{member.id_uuid}/"
    )

    assert response.status_code == HTTPStatus.NOT_FOUND
    assert response.json()["detail"] == "Active membership not found."


def test_club_eligibility_dashboard_requires_admin(client: Client) -> None:
    """Reject eligibility dashboard requests from non-admin viewers."""
    club = Club.objects.create(name="Eligibility Club")
    viewer = User.objects.create(username="elig_viewer")
    client.force_login(viewer)

    response = client.get(f"/api/club/clubs/{club.id_uuid}/eligibility-dashboard/")

    assert response.status_code == HTTPStatus.FORBIDDEN


def test_club_eligibility_dashboard_returns_own_team_and_distances(
    client: Client,
) -> None:
    """Calculate a player's own team, restrictions, and lock distances."""
    graph = _make_club_graph(club_name="Eligibility Club")
    player = _make_player("elig_player")
    team_1 = _make_ranked_team(graph, name="1", rank=1)
    team_2 = _make_ranked_team(graph, name="2", rank=2, players=(player,))
    for days_ago, team in ((21, team_2), (14, team_2), (7, team_1)):
        _make_finished_match(
            graph, team, timezone.now() - timedelta(days=days_ago), player
        )
    _login_as_admin(client, graph.club, "elig_admin")

    response = client.get(
        f"/api/club/clubs/{graph.club.id_uuid}/eligibility-dashboard/",
        {"season": str(graph.season.id_uuid)},
    )

    assert response.status_code == HTTPStatus.OK
    payload = response.json()
    assert payload["season_id"] == str(graph.season.id_uuid)
    assert payload["players"]
    player_row = next(
        row
        for row in payload["players"]
        if row["player"]["id_uuid"] == str(player.id_uuid)
    )
    assert player_row["played_matches_count"] == EXPECTED_PLAYED_MATCHES
    assert player_row["restrictions_active"] is True
    assert player_row["own_team_id"] == str(team_2.id_uuid)
    by_team = {row["team_id"]: row for row in player_row["by_team"]}
    assert by_team[str(team_2.id_uuid)]["allowed_for_team"] is True
    assert by_team[str(team_2.id_uuid)]["distance_to_lock"] == 0
    assert by_team[str(team_1.id_uuid)]["distance_to_lock"] == EXPECTED_DISTANCE_TO_LOCK


def test_club_eligibility_limits_players_moving_one_team_lower(
    client: Client,
) -> None:
    """Allow only two higher-team players to move one team lower."""
    today = timezone.localdate()
    graph = _make_club_graph(
        club_name="Limit Club",
        season_name="2026/2027",
        start_date=today - timedelta(days=60),
    )
    players = tuple(_make_player(f"limit_player_{index}") for index in range(1, 4))
    team_2 = _make_ranked_team(
        graph,
        name="2",
        rank=2,
        competition="Reserve 2e klasse",
        players=players,
    )
    team_3 = _make_ranked_team(graph, name="3", rank=3, competition="Reserve 3e klasse")
    week_anchor = today - timedelta(days=(today.isoweekday() - 2) % 7)
    for index, player in enumerate(players):
        base_week = 5 - index
        for team, weeks_ago in (
            (team_3, base_week),
            (team_2, base_week - 1),
            (team_2, base_week - 2),
        ):
            start_time = timezone.make_aware(
                datetime.combine(week_anchor - timedelta(weeks=weeks_ago), time(20))
            )
            _make_finished_match(graph, team, start_time, player)
    _login_as_admin(client, graph.club, "limit_admin")

    response = client.get(
        f"/api/club/clubs/{graph.club.id_uuid}/eligibility-dashboard/",
        {"season": str(graph.season.id_uuid)},
    )

    assert response.status_code == HTTPStatus.OK
    rows = {
        row["player"]["username"]: row
        for row in response.json()["players"]
        if row["player"]["username"].startswith("limit_player_")
    }
    team_3_allowed = [
        next(
            team
            for team in rows[f"limit_player_{index}"]["by_team"]
            if team["team_id"] == str(team_3.id_uuid)
        )["allowed_for_team"]
        for index in range(1, 4)
    ]
    assert team_3_allowed == [True, True, False]


def test_club_eligibility_counts_lowest_a_team_per_speelweek(client: Client) -> None:
    """Count the lowest A-team appearance when a week has two matches."""
    today = timezone.localdate()
    graph = _make_club_graph(
        club_name="Speelweek Club",
        season_name="2027/2028",
        start_date=today - timedelta(days=60),
    )
    player = _make_player("speelweek_player")
    team_1 = _make_ranked_team(graph, name="1", rank=1, competition="Reserve 2e klasse")
    team_2 = _make_ranked_team(
        graph,
        name="2",
        rank=2,
        competition="Reserve 3e klasse",
        players=(player,),
    )
    week_start = today - timedelta(days=(today.isoweekday() - 2) % 7, weeks=2)
    match_schedule = (
        (team_1, week_start - timedelta(days=7), time(20)),
        (team_1, week_start + timedelta(days=1), time(19, 30)),
        (team_2, week_start + timedelta(days=3), time(20)),
        (team_2, week_start + timedelta(days=8), time(20)),
    )
    for team, match_date, match_time in match_schedule:
        start_time = timezone.make_aware(datetime.combine(match_date, match_time))
        _make_finished_match(graph, team, start_time, player)
    _login_as_admin(client, graph.club, "speelweek_admin")

    response = client.get(
        f"/api/club/clubs/{graph.club.id_uuid}/eligibility-dashboard/",
        {"season": str(graph.season.id_uuid)},
    )

    assert response.status_code == HTTPStatus.OK
    player_row = next(
        row
        for row in response.json()["players"]
        if row["player"]["id_uuid"] == str(player.id_uuid)
    )
    assert player_row["played_matches_count"] == EXPECTED_PLAYED_MATCHES
    assert player_row["own_team_id"] == str(team_2.id_uuid)
