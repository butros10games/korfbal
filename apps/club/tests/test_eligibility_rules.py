"""Focused tests for the club eligibility policy boundaries."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal

from django.contrib.auth.models import User
from django.utils import timezone
import pytest

from apps.club.models import Club
from apps.club.services import eligibility_dashboard as eligibility
from apps.competition.models import (
    Club as SourceClub,
    CompetitionClass,
    CompetitionEdition,
    Pool,
    PoolEntry,
    Team as SourceTeam,
)
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
        ("Fortuna U17-2", True, "U17"),
        ("Fortuna J12", False, "J"),
        ("Recreanten", False, "UNKNOWN"),
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


def test_same_week_counts_lowest_team_including_b() -> None:
    """Article 21 counts the lowest team, without an A-category override."""
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
        == later_b
    )


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


@pytest.mark.django_db
def test_dashboard_uses_designated_team_and_current_minutes_algorithm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Dashboard appearances use lineup team attribution and the latest calculation."""
    monkeypatch.setattr(timezone, "localdate", lambda: date(2026, 3, 10))
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


def _context(
    name: str, rank: int = 1, *, a_category: bool = True
) -> eligibility.TeamContext:
    return eligibility.TeamContext(
        Team(name=name, club=Club(name="Synthetic club")),
        a_category,
        rank,
        eligibility._infer_family(team_name=name, wedstrijd_sport=a_category),
        "",
    )


def _dashboard_row(
    *,
    born: date | None,
    target: eligibility.TeamContext,
    own: eligibility.TeamContext | None = None,
    weeks: int = 3,
) -> dict:
    player = Player(name="Synthetic player", date_of_birth=born)
    source = own or target
    contexts = {str(team.team.pk): team for team in (source, target)}
    entries = [
        _played_entry(
            played_at=timezone.now() - timedelta(weeks=index + 1),
            team_id=str(source.team.pk),
            team_rank=source.team_rank,
            family=source.family,
            wedstrijd_sport=source.wedstrijd_sport,
        )
        for index in reversed(range(weeks))
    ]
    state = eligibility.PlayerState(
        player,
        entries,
        weeks,
        weeks >= eligibility.MIN_MATCHES_FOR_RESTRICTIONS,
        source.family,
        str(source.team.pk)
        if weeks >= eligibility.MIN_MATCHES_FOR_RESTRICTIONS
        else None,
        source.team_rank,
    )
    return eligibility._build_player_payloads(
        player_states={str(player.pk): state},
        team_context_by_id=contexts,
        season=Season(
            name="2026/2027", start_date=date(2026, 8, 1), end_date=date(2027, 6, 30)
        ),
        roster_teams={str(player.pk): [str(source.team.pk)]},
        lowest_a_rank_by_family=eligibility._build_lowest_a_rank_by_family(contexts),
    )[0]


@pytest.mark.parametrize(
    ("family", "year"), [("U19", 2008), ("U17", 2010), ("U15", 2012)]
)
@pytest.mark.parametrize("relative_year", [-1, 0, 1])
def test_youth_birth_year_boundary_applies_even_before_third_week(
    family: str, year: int, relative_year: int
) -> None:
    """Being unrestricted by vastspelen must never override an age restriction."""
    target = _context(f"Club {family}-1")
    row = _dashboard_row(born=date(year + relative_year, 1, 1), target=target, weeks=1)
    check = row["by_team"][0]
    assert check["eligibility_status"] == (
        "blocked" if relative_year < 0 else "available"
    )
    assert check["allowed_for_team"] is (relative_year >= 0)
    assert "date_of_birth" not in row["player"]
    assert "age" not in row["player"]


def test_missing_birth_date_is_a_check_not_permission_or_a_block() -> None:
    """A roster category is not evidence of a player's age."""
    row = _dashboard_row(born=None, target=_context("U17-1"))
    assert row["birth_date_known"] is False
    assert row["by_team"][0]["eligibility_status"] == "check"
    assert row["by_team"][0]["allowed_for_team"] is False


@pytest.mark.parametrize("born", [None, date(2012, 1, 1), date(2000, 1, 1)])
def test_b_youth_needs_the_published_cutoff_not_a_j_number(born: date | None) -> None:
    """No invented ages or permission from numbering or a rounded team average."""
    row = _dashboard_row(born=born, target=_context("J18", a_category=False))
    assert row["by_team"][0]["eligibility_status"] == "check"
    assert row["by_team"][0]["distance_to_lock"] is None


def test_young_player_can_move_up_from_u17_to_u19_and_seniors() -> None:
    """Age families form a hierarchy, rather than impermeable groups."""
    own = _context("U17-1")
    for target in (_context("U19-1"), _context("1")):
        row = _dashboard_row(born=date(2011, 1, 1), target=target, own=own)
        check = next(
            check for check in row["by_team"] if check["team_id"] == str(target.team.pk)
        )
        assert check["eligibility_status"] == "available"


def test_own_team_counts_across_age_categories() -> None:
    """Two senior appearances plus a youth appearance bind to the senior team."""
    senior, youth = _context("1"), _context("U19-1")
    teams = {str(team.team.pk): team for team in (senior, youth)}
    entries = [
        _played_entry(
            played_at=timezone.now() - timedelta(weeks=index + 1),
            team_id=str(team.team.pk),
            family=team.family,
        )
        for index, team in enumerate((senior, senior, youth))
    ]
    assert eligibility._own_team_id(entries=entries, teams=teams) == str(senior.team.pk)
    assert eligibility._pick_counted_match_for_week(entries).family == "U19"


def test_third_appearance_only_changes_permission_after_monday(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The entire third playing week is exempt from binding restrictions."""
    monday = datetime(2026, 9, 14, 12, tzinfo=UTC)
    player, target = Player(name="Week boundary"), _context("1")
    entries = [
        _played_entry(
            played_at=monday - timedelta(weeks=index), team_id=str(target.team.pk)
        )
        for index in range(3)
    ]
    for now, expected in ((monday, 2), (monday + timedelta(days=1), 3)):
        monkeypatch.setattr(timezone, "now", lambda now=now: now)
        state = eligibility._build_player_states(
            players_by_id={str(player.pk): player},
            entries_by_player={str(player.pk): entries},
            team_context_by_id={str(target.team.pk): target},
        )[str(player.pk)]
        assert state.total_counted == expected
        assert state.restrictions_active is (
            expected == eligibility.MIN_MATCHES_FOR_RESTRICTIONS
        )


@pytest.mark.parametrize("gap", [44, 45, 46])
def test_inactivity_requires_an_official_restart_check(
    monkeypatch: pytest.MonkeyPatch, gap: int
) -> None:
    """Do not silently make a bound returning player unrestricted."""
    now = datetime(2026, 9, 15, 12, tzinfo=UTC)
    monkeypatch.setattr(timezone, "now", lambda: now)
    player, target = Player(name="Returning player"), _context("1")
    entries = [
        _played_entry(
            played_at=now - timedelta(days=gap + week * 7), team_id=str(target.team.pk)
        )
        for week in range(3)
    ]
    state = eligibility._build_player_states(
        players_by_id={str(player.pk): player},
        entries_by_player={str(player.pk): entries},
        team_context_by_id={str(target.team.pk): target},
    )[str(player.pk)]
    assert state.history_needs_check is (gap >= eligibility.INACTIVITY_RESET_DAYS)
    if state.history_needs_check:
        assert (
            eligibility._binding_check(
                state, target, {str(target.team.pk): target}, {("", "SENIOR_A"): 1}
            )[0]
            == "check"
        )


@pytest.mark.django_db
def test_dashboard_includes_rosters_without_finished_matches() -> None:
    """Preseason still has usable player rows, with unknown history explicit."""
    season = Season.objects.create(
        name="Preseason", start_date=date(2026, 8, 1), end_date=date(2027, 6, 30)
    )
    club = Club.objects.create(name="Roster club")
    team = Team.objects.create(name="U17-1", club=club)
    data = TeamData.objects.create(team=team, season=season, wedstrijd_sport=True)
    player = Player.objects.create(name="No history", date_of_birth=date(2011, 1, 1))
    data.players.add(player)
    payload = eligibility.build_club_eligibility_dashboard(club=club, season=season)
    assert len(payload["players"]) == 1
    row = payload["players"][0]
    assert row["roster_team_ids"] == [str(team.pk)]
    assert row["played_matches_count"] == 0
    assert row["by_team"][0]["eligibility_status"] == "check"
    assert (
        row["by_team"][0]["distance_to_lock"]
        == eligibility.MIN_MATCHES_FOR_RESTRICTIONS
    )


@pytest.mark.django_db
def test_imported_teams_use_official_categories_and_numbers() -> None:
    """An imported A-team must not inherit the manual B/rank-1 defaults."""
    season = Season.objects.create(
        name="Official classification",
        start_date=date(2026, 8, 1),
        end_date=date(2027, 6, 30),
    )
    club = Club.objects.create(name="Classified club")
    team = Team.objects.create(name="Classified club 3", club=club)
    data = TeamData.objects.create(team=team, season=season)
    source_club = SourceClub.objects.create(
        external_id="synthetic-club", name=club.name
    )
    source_team = SourceTeam.objects.create(
        season=season,
        external_id="synthetic-team",
        club=source_club,
        local_team_data=data,
        name=team.name,
        sport="KORFBALL-VE-WK",
    )
    context = eligibility._build_team_context_by_id(
        TeamData.objects.filter(pk=data.pk).select_related("team")
    )[str(team.pk)]
    assert context.classification_known is False
    edition = CompetitionEdition.objects.create(
        season=season, discipline="outdoor", phase="autumn", gender="mixed"
    )
    classification = CompetitionClass.objects.create(
        edition=edition,
        code="class_2",
        category="a",
        age_group="senior",
        team_kind="reserve",
        colour="unknown",
        playing_format="eight",
    )
    pool = Pool.objects.create(
        season=season, external_id="synthetic-pool", competition_class=classification
    )
    PoolEntry.objects.create(pool=pool, team=source_team)
    context = eligibility._build_team_context_by_id(
        TeamData.objects.filter(pk=data.pk).select_related("team")
    )[str(team.pk)]
    assert context.classification_known is True
    assert context.wedstrijd_sport is True
    assert context.family == "SENIOR_A"
    assert context.team_rank == int(team.name.rsplit(" ", 1)[-1])
    assert context.competition == str(classification.pk)
    spring = CompetitionEdition.objects.create(
        season=season, discipline="outdoor", phase="spring", gender="mixed"
    )
    other_class = CompetitionClass.objects.create(
        edition=spring,
        code="class_2",
        category="a",
        age_group="senior",
        team_kind="reserve",
        colour="unknown",
        playing_format="eight",
    )
    other_pool = Pool.objects.create(
        season=season, external_id="synthetic-spring", competition_class=other_class
    )
    PoolEntry.objects.create(pool=other_pool, team=source_team)
    context = eligibility._build_team_context_by_id(
        TeamData.objects.filter(pk=data.pk).select_related("team")
    )[str(team.pk)]
    assert context.classification_known is False


@pytest.mark.django_db
def test_opponent_minutes_do_not_create_club_players() -> None:
    """The explicit opponent team must win over a single-club-match fallback."""
    season = Season.objects.create(
        name="Opponent attribution",
        start_date=date(2026, 8, 1),
        end_date=date(2027, 6, 30),
    )
    club = Club.objects.create(name="Home club")
    away_club = Club.objects.create(name="Away club")
    home = Team.objects.create(name="1", club=club)
    away = Team.objects.create(name="1", club=away_club)
    TeamData.objects.create(team=home, season=season, wedstrijd_sport=True)
    opponent = Player.objects.create(name="Opponent")
    match = Match.objects.create(
        home_team=home,
        away_team=away,
        season=season,
        start_time=timezone.now() - timedelta(weeks=1),
    )
    data = MatchData.objects.get(match_link=match)
    data.status = "finished"
    data.save(update_fields=["status"])
    MatchPlayer.objects.create(match_data=data, player=opponent, team=away)
    PlayerMatchMinutes.objects.create(
        match_data=data,
        player=opponent,
        algorithm_version=LATEST_MATCH_MINUTES_VERSION,
        minutes_played=Decimal(60),
    )
    payload = eligibility.build_club_eligibility_dashboard(club=club, season=season)
    assert payload["players"] == []


@pytest.mark.django_db
@pytest.mark.parametrize(("birth_year", "count"), [(2009, 0), (2010, 1)])
def test_age_ineligible_appearances_do_not_count_towards_binding(
    birth_year: int, count: int
) -> None:
    """Article 21.5 excludes a known ineligible appearance from the history."""
    season = Season.objects.create(
        name="History age boundary",
        start_date=date(2026, 8, 1),
        end_date=date(2027, 6, 30),
    )
    club = Club.objects.create(name="History club")
    other = Club.objects.create(name="Opponent club")
    team = Team.objects.create(name="U17-1", club=club)
    away = Team.objects.create(name="U17-1", club=other)
    roster = TeamData.objects.create(team=team, season=season, wedstrijd_sport=True)
    player = Player.objects.create(
        name="Age boundary", date_of_birth=date(birth_year, 1, 1)
    )
    roster.players.add(player)
    match = Match.objects.create(
        home_team=team,
        away_team=away,
        season=season,
        start_time=timezone.now() - timedelta(weeks=1),
    )
    data = MatchData.objects.get(match_link=match)
    data.status = "finished"
    data.save(update_fields=["status"])
    MatchPlayer.objects.create(match_data=data, player=player, team=team)
    PlayerMatchMinutes.objects.create(
        match_data=data,
        player=player,
        algorithm_version=LATEST_MATCH_MINUTES_VERSION,
        minutes_played=Decimal(60),
    )
    row = eligibility.build_club_eligibility_dashboard(club=club, season=season)[
        "players"
    ][0]
    assert row["played_matches_count"] == count


def test_korfbal_league_exception_is_not_limited_to_one_team_down() -> None:
    """Article 21.9 permits the reserve league for the entire season."""
    own = replace(_context("1"), competition="league:standard")
    target = replace(_context("4", rank=4), competition="league:reserve")
    row = _dashboard_row(born=date(2000, 1, 1), target=target, own=own)
    check = next(
        check for check in row["by_team"] if check["team_id"] == str(target.team.pk)
    )
    assert check["eligibility_status"] == "available"
    assert "21.9" in check["allowed_reason"]
