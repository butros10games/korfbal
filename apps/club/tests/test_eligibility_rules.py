"""Focused tests for the club eligibility policy boundaries."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from decimal import Decimal

from django.contrib.auth.models import User
from django.utils import timezone
import pytest

from apps.club.models import Club
from apps.club.services import eligibility_dashboard as eligibility
from apps.game_tracker.models import MatchData, MatchPlayer, PlayerMatchMinutes
from apps.game_tracker.models.player_match_minutes import LATEST_MATCH_MINUTES_VERSION
from apps.player.models import Player
from apps.schedule.models import Match, Season
from apps.team.models import Team, TeamData


FULL_PERCENT = 100


def _played_entry(
    *,
    played_at: datetime,
    team_id: str = "team-1",
    team_rank: int = 1,
    family: str = "SENIOR_A",
    wedstrijd_sport: bool = True,
) -> eligibility.PlayedEntry:
    return eligibility.PlayedEntry(
        played_at=played_at,
        week_start=eligibility._week_start_for(played_at),
        team_id=team_id,
        team_rank=team_rank,
        family=family,
        wedstrijd_sport=wedstrijd_sport,
    )


@pytest.mark.parametrize(
    ("raw_rank", "team_name", "expected"),
    [
        (3, "Name without rank", 3),
        (None, "KWT 7", 7),
        (0, "KWT 0", 1),
        (None, "Recreanten", 9999),
    ],
)
def test_team_rank_uses_explicit_value_then_name_fallback(
    raw_rank: int | None,
    team_name: str,
    expected: int,
) -> None:
    """Eligibility ordering must stay deterministic with incomplete team metadata."""
    assert eligibility._coerce_rank(raw_rank, team_name) == expected


@pytest.mark.parametrize(
    ("team_name", "wedstrijd_sport", "expected"),
    [
        ("U19-2", True, "U19"),
        ("u 17_3", True, "U17"),
        ("J4", False, "J"),
        ("KWT 3", True, "SENIOR_A"),
        ("KWT 6", False, "SENIOR_B"),
    ],
)
def test_team_family_preserves_age_and_competition_boundaries(
    team_name: str,
    wedstrijd_sport: bool,
    expected: str,
) -> None:
    """Youth categories must not collapse into senior A/B policy families."""
    assert (
        eligibility._infer_family(
            team_name=team_name,
            wedstrijd_sport=wedstrijd_sport,
        )
        == expected
    )


def test_match_counts_at_exactly_seventy_five_percent() -> None:
    """The played-match threshold is inclusive at exactly 75 percent."""
    match_data = MatchData(parts=2, part_length=1800)

    assert (
        eligibility._is_played_match(minutes_played=45, match_data=match_data) is True
    )
    assert (
        eligibility._is_played_match(minutes_played=44.99, match_data=match_data)
        is False
    )


def test_same_week_prefers_lowest_a_team_over_later_b_team() -> None:
    """Only one match counts per week, with A-category appearances taking priority."""
    week = timezone.make_aware(datetime.combine(date(2026, 8, 25), time(19)))
    higher_a = _played_entry(played_at=week, team_id="a-1", team_rank=1)
    lower_a = _played_entry(
        played_at=week + timedelta(days=1),
        team_id="a-2",
        team_rank=2,
    )
    later_b = _played_entry(
        played_at=week + timedelta(days=3),
        team_id="b-4",
        team_rank=4,
        family="SENIOR_B",
        wedstrijd_sport=False,
    )

    assert (
        eligibility._pick_counted_match_for_week([higher_a, later_b, lower_a])
        == lower_a
    )


@pytest.mark.parametrize(
    ("gap_days", "expected_count"),
    [
        (eligibility.INACTIVITY_RESET_DAYS, 2),
        (eligibility.INACTIVITY_RESET_DAYS + 1, 1),
    ],
)
def test_inactivity_resets_only_after_more_than_45_days(
    gap_days: int,
    expected_count: int,
) -> None:
    """The 45-day reset boundary is exclusive and discards older appearances."""
    first_at = timezone.make_aware(datetime.combine(date(2026, 1, 1), time(12)))
    entries = [
        _played_entry(played_at=first_at),
        _played_entry(played_at=first_at + timedelta(days=gap_days)),
    ]

    assert len(eligibility._trim_by_inactivity(entries)) == expected_count


def test_own_team_threshold_is_strictly_greater_than_65_percent() -> None:
    """Exactly 65 percent is insufficient, while two of three appearances lock."""
    assert eligibility._threshold_passes(numerator=13, denominator=20) is False
    assert eligibility._threshold_passes(numerator=2, denominator=3) is True
    assert eligibility._distance_to_lock(current_q=13, current_n=20) == 1


def test_own_team_uses_cumulative_appearances_at_or_above_candidate() -> None:
    """Two higher-team appearances out of three identify the higher own team."""
    club = Club(name="Policy Club")
    team_1 = Team(name="1", club=club)
    team_2 = Team(name="2", club=club)
    contexts = {
        str(team_1.id_uuid): eligibility.TeamContext(
            team_1, True, 1, "SENIOR_A", "Class A"
        ),
        str(team_2.id_uuid): eligibility.TeamContext(
            team_2, True, 2, "SENIOR_A", "Class B"
        ),
    }
    played_at = timezone.make_aware(datetime.combine(date(2026, 1, 1), time(12)))
    entries = [
        _played_entry(
            played_at=played_at + timedelta(weeks=index),
            team_id=str(team.id_uuid),
            team_rank=rank,
        )
        for index, (team, rank) in enumerate(((team_1, 1), (team_1, 1), (team_2, 2)))
    ]

    assert eligibility._own_team_id(entries=entries, teams=contexts) == str(
        team_1.id_uuid
    )


def test_only_lowest_a_team_can_cross_to_same_stage_b_family() -> None:
    """The A-to-B exception applies only to the lowest A rank in the age stage."""
    club = Club(name="Cross Category Club")
    lowest_a = eligibility.TeamContext(
        Team(name="3", club=club), True, 3, "SENIOR_A", ""
    )
    higher_a = eligibility.TeamContext(
        Team(name="2", club=club), True, 2, "SENIOR_A", ""
    )
    senior_b = eligibility.TeamContext(
        Team(name="4", club=club), False, 4, "SENIOR_B", ""
    )
    youth_b = eligibility.TeamContext(Team(name="J4", club=club), False, 4, "J", "")
    lowest_by_family = {"SENIOR_A": 3}

    assert eligibility._can_lowest_a_play_b(
        own_team=lowest_a,
        target_team=senior_b,
        lowest_a_rank_by_family=lowest_by_family,
    )
    assert not eligibility._can_lowest_a_play_b(
        own_team=higher_a,
        target_team=senior_b,
        lowest_a_rank_by_family=lowest_by_family,
    )
    assert not eligibility._can_lowest_a_play_b(
        own_team=lowest_a,
        target_team=youth_b,
        lowest_a_rank_by_family=lowest_by_family,
    )


@pytest.mark.django_db
def test_lower_team_slot_limit_turns_off_after_three_quarters(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The seasonal two-player limit applies through, but not after, 75 percent."""
    season = Season.objects.create(
        name="Boundary Season",
        start_date=date(2026, 1, 1),
        end_date=date(2026, 5, 1),
    )
    monkeypatch.setattr(timezone, "localdate", lambda: date(2026, 4, 1))
    assert eligibility._season_before_three_quarters(season) is True

    monkeypatch.setattr(timezone, "localdate", lambda: date(2026, 4, 2))
    assert eligibility._season_before_three_quarters(season) is False


@pytest.mark.django_db
def test_dashboard_uses_designated_team_and_current_minutes_algorithm() -> None:
    """Dashboard appearances use lineup team attribution and the latest calculation."""
    season = Season.objects.create(
        name="Minutes Season",
        start_date=date(2026, 1, 1),
        end_date=date(2026, 12, 31),
    )
    club = Club.objects.create(name="Minutes Club")
    team_1 = Team.objects.create(name="1", club=club)
    team_2 = Team.objects.create(name="2", club=club)
    team_1_data = TeamData.objects.create(
        team=team_1,
        season=season,
        wedstrijd_sport=True,
        team_rank=1,
    )
    team_2_data = TeamData.objects.create(
        team=team_2,
        season=season,
        wedstrijd_sport=True,
        team_rank=2,
    )
    exact_user = User.objects.create_user(username="exact-75")
    below_user = User.objects.create_user(username="below-75")
    stale_user = User.objects.create_user(username="stale-version")
    exact_player = Player.objects.get(user=exact_user)
    below_player = Player.objects.get(user=below_user)
    stale_player = Player.objects.get(user=stale_user)
    team_1_data.players.add(exact_player, below_player, stale_player)
    team_2_data.players.add(exact_player)

    match = Match.objects.create(
        home_team=team_1,
        away_team=team_2,
        season=season,
        start_time=timezone.make_aware(datetime.combine(date(2026, 3, 1), time(12))),
    )
    match_data = MatchData.objects.get(match_link=match)
    match_data.status = "finished"
    match_data.save(update_fields=["status"])
    MatchPlayer.objects.create(
        match_data=match_data,
        player=exact_player,
        team=team_2,
    )
    for player, version, minutes in (
        (exact_player, LATEST_MATCH_MINUTES_VERSION, Decimal("45.00")),
        (below_player, LATEST_MATCH_MINUTES_VERSION, Decimal("44.99")),
        (stale_player, "obsolete", Decimal("60.00")),
    ):
        PlayerMatchMinutes.objects.create(
            match_data=match_data,
            player=player,
            algorithm_version=version,
            minutes_played=minutes,
        )

    payload = eligibility.build_club_eligibility_dashboard(club=club, season=season)
    players = {row["player"]["username"]: row for row in payload["players"]}

    assert players["exact-75"]["played_matches_count"] == 1
    assert players["below-75"]["played_matches_count"] == 0
    assert players["stale-version"]["played_matches_count"] == 0
    team_2_row = next(
        row
        for row in players["exact-75"]["by_team"]
        if row["team_id"] == str(team_2.id_uuid)
    )
    assert team_2_row["played_ratio_percent"] == FULL_PERCENT
