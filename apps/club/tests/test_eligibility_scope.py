"""Eligibility follows competition history, not the displayed season half."""

from __future__ import annotations

from datetime import UTC, date, datetime

from django.utils import timezone
import pytest

from apps.club.models import Club
from apps.club.queries.eligibility_scope import eligibility_scope
from apps.club.queries.overview import club_seasons
from apps.club.services import eligibility_dashboard as eligibility
from apps.competition.domain.match_rules import RuleContext, resolve_rules
from apps.game_tracker.domain.match_rules import OFFICIAL_TIMING, MatchRules
from apps.game_tracker.models import MatchData
from apps.player.models import Player
from apps.schedule.domain.competition_context import AUTUMN, FULL_SEASON, SPRING
from apps.schedule.models import Match
from apps.schedule.tests.season_builders import playing_season
from apps.team.models import Team, TeamData


def entry(day: date, period: str, team_id: str = "team-1") -> eligibility.PlayedEntry:
    """Return one counted appearance in a competition period."""
    played_at = datetime(day.year, day.month, day.day, 12, tzinfo=UTC)
    return eligibility.PlayedEntry(
        played_at=played_at,
        week_start=eligibility._week_start_for(played_at),
        team_id=team_id,
        team_rank=1,
        family="SENIOR_A",
        wedstrijd_sport=True,
        period=period,
    )


def state(
    entries: list[eligibility.PlayedEntry],
    winter_break: tuple[datetime, datetime] | None = None,
) -> eligibility.PlayerState:
    """Build one player's state from the given entries."""
    player = Player(name="Synthetic player")
    return eligibility._build_player_states(
        players_by_id={str(player.pk): player},
        entries_by_player={str(player.pk): entries},
        team_context_by_id={},
        winter_break=winter_break,
    )[str(player.pk)]


@pytest.mark.django_db
def test_folded_halves_read_the_continuous_season() -> None:
    """Either outdoor half shows the full-year roster and its history."""
    whole = playing_season(FULL_SEASON, 2026)
    autumn = playing_season(AUTUMN, 2026)
    spring = playing_season(SPRING, 2026)
    club = Club.objects.create(name="Synthetic home")
    home = Team.objects.create(name="Synthetic 1", club=club)
    roster = TeamData.objects.create(team=home, season=whole)
    roster.players.add(Player.objects.create(name="Synthetic roster player"))
    assert {season.pk for season in club_seasons(club)} == {autumn.pk, spring.pk}
    for half in (autumn, spring):
        payload = eligibility.build_club_eligibility_dashboard(club=club, season=half)
        assert [team["name"] for team in payload["teams"]] == ["Synthetic 1"]
        assert len(payload["players"]) == 1
        scope = payload["competition_scope"]
        assert scope["display_season_id"] == str(half.pk)
        assert scope["continuous_season_id"] == str(whole.pk)
        assert set(scope["season_ids"]) == {str(half.pk), str(whole.pk)}


@pytest.mark.django_db
def test_winter_break_comes_from_the_clubs_own_fixtures() -> None:
    """The break is the gap around New Year in the club's continuous fixtures."""
    whole = playing_season(FULL_SEASON, 2026)
    club = Club.objects.create(name="Break club")
    home = Team.objects.create(name="Break 1", club=club)
    away = Team.objects.create(name="Away 1", club=Club.objects.create(name="Away"))
    for day in (date(2026, 10, 10), date(2027, 3, 27)):
        Match.objects.create(
            home_team=home,
            away_team=away,
            season=whole,
            start_time=datetime(day.year, day.month, day.day, 14, tzinfo=UTC),
        )
    scope = eligibility_scope(whole, [str(home.pk)])
    assert scope.continuous == whole
    assert scope.winter_break is not None
    assert scope.winter_break[0].date() == date(2026, 10, 10)
    assert scope.winter_break[1].date() == date(2027, 3, 27)


def test_continuous_competition_ignores_the_winter_break(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """RvW art. 21: the winter break does not reset continuous outdoor history."""
    monkeypatch.setattr(timezone, "localdate", lambda: date(2027, 4, 10))
    entries = [
        entry(date(2026, 9, 26), FULL_SEASON),
        entry(date(2026, 10, 3), FULL_SEASON),
        entry(date(2026, 10, 10), FULL_SEASON),
        entry(date(2027, 4, 3), FULL_SEASON),
    ]
    winter = (
        datetime(2026, 10, 10, 12, tzinfo=UTC),
        datetime(2027, 3, 27, 12, tzinfo=UTC),
    )
    assert not state(entries, winter).history_needs_check
    # Without the exception the same gap needs an official restart check.
    assert state(entries).history_needs_check


def test_independent_spring_competition_starts_a_new_history(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Autumn appearances do not count towards an independent spring competition."""
    monkeypatch.setattr(timezone, "localdate", lambda: date(2027, 4, 20))
    monkeypatch.setattr(timezone, "now", lambda: datetime(2027, 4, 20, tzinfo=UTC))
    entries = [
        entry(date(2026, 9, 26), AUTUMN),
        entry(date(2026, 10, 3), AUTUMN),
        entry(date(2026, 10, 10), AUTUMN),
        entry(date(2027, 4, 3), SPRING),
        entry(date(2027, 4, 10), SPRING),
    ]
    result = state(entries)
    assert result.total_counted == 2  # noqa: PLR2004 - spring weeks only
    assert {item.period for item in result.counted_entries} == {SPRING}
    assert not result.restrictions_active


def tracker(rules: MatchRules | None) -> MatchData:
    """Build an unsaved tracker with a rule profile, or the legacy assumption."""
    if rules is None:
        return MatchData(parts=2, part_length=1800)
    return MatchData(
        parts=2,
        part_length=1500,
        rules=rules.as_snapshot(),
        rules_source=rules.source,
    )


def test_played_threshold_uses_the_resolved_duration() -> None:
    """A 50-minute match counts from 37.5 minutes, not 45."""
    rules = resolve_rules(
        RuleContext(edition=2026, category="a", age_group="senior"),
        minutes=50,
        periods=[
            {"Description": "1e helft", "PlayTime": 25},
            {"Description": "2e helft", "PlayTime": 25},
        ],
    )
    assert rules.source == OFFICIAL_TIMING
    match = tracker(rules)
    assert eligibility._is_played_match(minutes_played=37.5, match_data=match)
    assert not eligibility._is_played_match(minutes_played=37.4, match_data=match)


def test_assumed_duration_makes_partial_appearances_uncertain() -> None:
    """Without a resolved duration, 40 of an assumed 60 minutes is not 'not played'."""
    legacy = tracker(None)
    assert (
        eligibility._played_verdict(minutes_played=40, match_data=legacy)
        == eligibility.UNCERTAIN
    )
    assert (
        eligibility._played_verdict(minutes_played=45, match_data=legacy)
        == eligibility.PLAYED
    )
    unclear = eligibility.PlayerState(
        Player(name="Unclear"),
        [],
        0,
        restrictions_active=False,
        active_family=None,
        own_team_id=None,
        last_week_team_rank=None,
        duration_needs_check=True,
    )
    target = eligibility.TeamContext(
        Team(name="1", club=Club(name="Club")), True, 1, "SENIOR_A", ""
    )
    status, reason = eligibility._binding_check(
        unclear, target, {str(target.team.pk): target}, {}
    )
    assert status == "check"
    assert "Wedstrijdduur" in reason


@pytest.mark.django_db
def test_independent_autumn_display_stays_separate() -> None:
    """An autumn-only display has no continuous history or winter break."""
    autumn = playing_season(AUTUMN, 2025)
    scope = eligibility_scope(autumn, [])
    assert scope.seasons == (autumn,)
    assert scope.continuous is None
    assert scope.winter_break is None
