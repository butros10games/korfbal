"""Edge-case tests for match stats player side resolution.

The match stats payload attempts to put every player on a single side (home/away)
using a preference order:

1. Match roster (MatchPlayer)
2. Match-tracking assignment (PlayerGroup membership)
3. Season roster membership (TeamData)
4. Shot-side heuristics (shot team sets), then finally per-team shot counts

These tests cover ambiguity scenarios where a player appears on both sides in
some sources (bad data / historical migrations) and the resolver must still
behave deterministically.
"""

from __future__ import annotations

from http import HTTPStatus

from django.contrib.auth import get_user_model
from django.test import override_settings
from django.test.client import Client
from django.utils import timezone
import pytest

from apps.club.models import Club
from apps.game_tracker.models import GoalType, GroupType, MatchData, PlayerGroup, Shot
from apps.schedule.models import Match, Season
from apps.team.models import Team, TeamData


@pytest.mark.django_db
@override_settings(SECURE_SSL_REDIRECT=False)
def test_match_stats_shot_only_player_falls_back_to_shot_count_majority(
    client: Client,
) -> None:
    """When everything is missing/ambiguous, the resolver uses per-team shot counts."""
    today = timezone.now().date()
    season = Season.objects.create(name="2025", start_date=today, end_date=today)

    home_team = Team.objects.create(
        name="Home",
        club=Club.objects.create(name="Home Club"),
    )
    away_team = Team.objects.create(
        name="Away",
        club=Club.objects.create(name="Away Club"),
    )

    match = Match.objects.create(
        home_team=home_team,
        away_team=away_team,
        season=season,
        start_time=timezone.now(),
    )

    match_data = MatchData.objects.get(match_link=match)
    match_data.status = "finished"
    match_data.save(update_fields=["status"])

    user = get_user_model().objects.create_user(
        username="shot_only",
        password="pass1234",  # noqa: S106  # nosec
    )
    player = user.player

    doorloop = GoalType.objects.create(name="Doorloop")

    # Player has shots for BOTH teams (bad data), but more for away.
    for _ in range(2):
        Shot.objects.create(
            match_data=match_data,
            team=home_team,
            player=player,
            scored=False,
            shot_type=doorloop,
        )
    for _ in range(3):
        Shot.objects.create(
            match_data=match_data,
            team=away_team,
            player=player,
            scored=False,
            shot_type=doorloop,
        )

    resp = client.get(f"/api/matches/{match.id_uuid}/stats/")
    assert resp.status_code == HTTPStatus.OK

    payload = resp.json()
    players_home = payload["players"]["home"]
    players_away = payload["players"]["away"]

    assert not any(line["username"] == "shot_only" for line in players_home)
    assert any(line["username"] == "shot_only" for line in players_away)


@pytest.mark.django_db
@override_settings(SECURE_SSL_REDIRECT=False)
def test_match_stats_shot_only_player_tie_breaks_to_home(
    client: Client,
) -> None:
    """Tie-breaker should be deterministic: equal shots => home (>=)."""
    today = timezone.now().date()
    season = Season.objects.create(name="2025", start_date=today, end_date=today)

    home_team = Team.objects.create(
        name="Home",
        club=Club.objects.create(name="Home Club"),
    )
    away_team = Team.objects.create(
        name="Away",
        club=Club.objects.create(name="Away Club"),
    )

    match = Match.objects.create(
        home_team=home_team,
        away_team=away_team,
        season=season,
        start_time=timezone.now(),
    )

    match_data = MatchData.objects.get(match_link=match)
    match_data.status = "finished"
    match_data.save(update_fields=["status"])

    user = get_user_model().objects.create_user(
        username="shot_only_tie",
        password="pass1234",  # noqa: S106  # nosec
    )
    player = user.player

    doorloop = GoalType.objects.create(name="Doorloop")

    # Equal number of shots logged to both teams.
    for _ in range(2):
        Shot.objects.create(
            match_data=match_data,
            team=home_team,
            player=player,
            scored=False,
            shot_type=doorloop,
        )
        Shot.objects.create(
            match_data=match_data,
            team=away_team,
            player=player,
            scored=False,
            shot_type=doorloop,
        )

    resp = client.get(f"/api/matches/{match.id_uuid}/stats/")
    assert resp.status_code == HTTPStatus.OK

    payload = resp.json()
    players_home = payload["players"]["home"]
    players_away = payload["players"]["away"]

    assert any(line["username"] == "shot_only_tie" for line in players_home)
    assert not any(line["username"] == "shot_only_tie" for line in players_away)


@pytest.mark.django_db
@override_settings(SECURE_SSL_REDIRECT=False)
def test_match_stats_playergroup_ambiguity_falls_back_to_teamdata(
    client: Client,
) -> None:
    """If a player is (incorrectly) in both sides' groups, TeamData resolves side."""
    today = timezone.now().date()
    season = Season.objects.create(name="2025", start_date=today, end_date=today)

    home_team = Team.objects.create(
        name="Home",
        club=Club.objects.create(name="Home Club"),
    )
    away_team = Team.objects.create(
        name="Away",
        club=Club.objects.create(name="Away Club"),
    )

    match = Match.objects.create(
        home_team=home_team,
        away_team=away_team,
        season=season,
        start_time=timezone.now(),
    )

    match_data = MatchData.objects.get(match_link=match)
    match_data.status = "finished"
    match_data.save(update_fields=["status"])

    user = get_user_model().objects.create_user(
        username="both_groups",
        password="pass1234",  # noqa: S106  # nosec
    )
    player = user.player

    reserve_type = GroupType.objects.create(name="Reserve", order=0)

    # Bad data: player is present in both teams' PlayerGroup lists.
    home_group = PlayerGroup.objects.create(
        match_data=match_data,
        team=home_team,
        starting_type=reserve_type,
        current_type=reserve_type,
    )
    away_group = PlayerGroup.objects.create(
        match_data=match_data,
        team=away_team,
        starting_type=reserve_type,
        current_type=reserve_type,
    )
    home_group.players.add(player)
    away_group.players.add(player)

    # TeamData is exclusive: player belongs to home for the season.
    TeamData.objects.create(team=home_team, season=season).players.add(player)
    TeamData.objects.create(team=away_team, season=season)

    doorloop = GoalType.objects.create(name="Doorloop")

    # Also create mixed shots so shot heuristics are ambiguous too.
    Shot.objects.create(
        match_data=match_data,
        team=home_team,
        player=player,
        scored=False,
        shot_type=doorloop,
    )
    Shot.objects.create(
        match_data=match_data,
        team=away_team,
        player=player,
        scored=False,
        shot_type=doorloop,
    )

    resp = client.get(f"/api/matches/{match.id_uuid}/stats/")
    assert resp.status_code == HTTPStatus.OK

    payload = resp.json()
    players_home = payload["players"]["home"]
    players_away = payload["players"]["away"]

    assert any(line["username"] == "both_groups" for line in players_home)
    assert not any(line["username"] == "both_groups" for line in players_away)


@pytest.mark.django_db
@override_settings(SECURE_SSL_REDIRECT=False)
def test_match_stats_teamdata_and_groups_ambiguity_falls_back_to_shot_sets(
    client: Client,
) -> None:
    """If TeamData and PlayerGroup are ambiguous, shot-side sets resolve side."""
    today = timezone.now().date()
    season = Season.objects.create(name="2025", start_date=today, end_date=today)

    home_team = Team.objects.create(
        name="Home",
        club=Club.objects.create(name="Home Club"),
    )
    away_team = Team.objects.create(
        name="Away",
        club=Club.objects.create(name="Away Club"),
    )

    match = Match.objects.create(
        home_team=home_team,
        away_team=away_team,
        season=season,
        start_time=timezone.now(),
    )

    match_data = MatchData.objects.get(match_link=match)
    match_data.status = "finished"
    match_data.save(update_fields=["status"])

    user = get_user_model().objects.create_user(
        username="ambiguous_sources",
        password="pass1234",  # noqa: S106  # nosec
    )
    player = user.player

    reserve_type = GroupType.objects.create(name="Reserve", order=0)

    # Ambiguous PlayerGroup: player is in both.
    PlayerGroup.objects.create(
        match_data=match_data,
        team=home_team,
        starting_type=reserve_type,
        current_type=reserve_type,
    ).players.add(player)
    PlayerGroup.objects.create(
        match_data=match_data,
        team=away_team,
        starting_type=reserve_type,
        current_type=reserve_type,
    ).players.add(player)

    # Ambiguous TeamData: player in both teams for the season.
    TeamData.objects.create(team=home_team, season=season).players.add(player)
    TeamData.objects.create(team=away_team, season=season).players.add(player)

    doorloop = GoalType.objects.create(name="Doorloop")

    # Only away shots exist, so shot-set resolution should place player on away.
    for _ in range(2):
        Shot.objects.create(
            match_data=match_data,
            team=away_team,
            player=player,
            scored=False,
            shot_type=doorloop,
        )

    resp = client.get(f"/api/matches/{match.id_uuid}/stats/")
    assert resp.status_code == HTTPStatus.OK

    payload = resp.json()
    players_home = payload["players"]["home"]
    players_away = payload["players"]["away"]

    assert not any(line["username"] == "ambiguous_sources" for line in players_home)
    assert any(line["username"] == "ambiguous_sources" for line in players_away)
