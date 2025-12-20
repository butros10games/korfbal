"""Tests for the HTTP match tracker service."""

from datetime import UTC, datetime, timedelta

from django.contrib.auth import get_user_model
from django.utils import timezone
import pytest

from apps.club.models import Club
from apps.game_tracker.models import (
    GoalType,
    GroupType,
    MatchData,
    MatchPart,
    Pause,
    PlayerChange,
    PlayerGroup,
    Timeout,
)
from apps.game_tracker.services.tracker_http import (
    TrackerCommandError,
    apply_tracker_command,
    get_tracker_state,
)
from apps.schedule.models import Match, Season
from apps.team.models import Team


TEST_PASSWORD = "testpass123"  # noqa: S105  # nosec B105 - test credential constant
MAX_WISSELS = 8
MAX_TIMEOUTS = 2


@pytest.mark.django_db
def test_part_end_closes_active_pause_even_without_active_part() -> None:
    """Ending a part must close any active pause, even if the part is already inactive.

    This protects against edge cases where state becomes inconsistent (e.g. a pause
    started but the part was ended without ending the pause), which can crash
    downstream timer calculations.
    """
    home_club = Club.objects.create(name="PartEnd Pause Home Club")
    away_club = Club.objects.create(name="PartEnd Pause Away Club")
    home_team = Team.objects.create(name="PartEnd Pause Home Team", club=home_club)
    away_team = Team.objects.create(name="PartEnd Pause Away Team", club=away_club)

    season = Season.objects.create(
        name="PartEnd Pause Season",
        start_date=timezone.now().date() - timedelta(days=1),
        end_date=timezone.now().date() + timedelta(days=365),
    )

    match = Match.objects.create(
        home_team=home_team,
        away_team=away_team,
        season=season,
        start_time=timezone.now() - timedelta(minutes=10),
    )

    match_data = MatchData.objects.get(match_link=match)
    match_data.status = "active"
    match_data.parts = 2
    match_data.current_part = 1
    match_data.save(update_fields=["status", "parts", "current_part"])

    # Simulate inconsistent state: the part is already inactive, but a pause is
    # still marked active for that part.
    part = MatchPart.objects.create(
        match_data=match_data,
        part_number=1,
        start_time=datetime.now(UTC),
        end_time=datetime.now(UTC),
        active=False,
    )
    pause = Pause.objects.create(
        match_data=match_data,
        match_part=part,
        start_time=datetime.now(UTC),
        active=True,
    )

    apply_tracker_command(match, team=home_team, payload={"command": "part_end"})

    pause.refresh_from_db()
    assert pause.active is False
    assert pause.end_time is not None


@pytest.mark.django_db
def test_substitute_reg_allowed_between_parts_and_next_part_can_start() -> None:
    """Wissels should be allowed in the break between parts.

    Regression: if a wissel is registered between parts, starting the next part
    must still work.
    """
    home_club = Club.objects.create(name="BetweenParts Home Club")
    away_club = Club.objects.create(name="BetweenParts Away Club")
    home_team = Team.objects.create(name="BetweenParts Home Team", club=home_club)
    away_team = Team.objects.create(name="BetweenParts Away Team", club=away_club)

    season = Season.objects.create(
        name="BetweenParts Season",
        start_date=timezone.now().date() - timedelta(days=1),
        end_date=timezone.now().date() + timedelta(days=365),
    )

    match = Match.objects.create(
        home_team=home_team,
        away_team=away_team,
        season=season,
        start_time=timezone.now() - timedelta(minutes=10),
    )

    match_data = MatchData.objects.get(match_link=match)
    match_data.status = "active"
    match_data.parts = 2
    # Simulate that part 1 ended, and we're now in the break before part 2.
    match_data.current_part = 2
    match_data.save(update_fields=["status", "parts", "current_part"])

    MatchPart.objects.create(
        match_data=match_data,
        part_number=1,
        start_time=datetime.now(UTC) - timedelta(minutes=30),
        end_time=datetime.now(UTC) - timedelta(minutes=1),
        active=False,
    )

    gt_attack = GroupType.objects.create(name="Aanval")
    gt_reserve = GroupType.objects.create(name="Reserve")

    player_out = (
        get_user_model()
        .objects.create_user(username="bp_player_out", password=TEST_PASSWORD)
        .player
    )
    player_in = (
        get_user_model()
        .objects.create_user(username="bp_player_in", password=TEST_PASSWORD)
        .player
    )

    reserve_group = PlayerGroup.objects.create(
        team=home_team,
        match_data=match_data,
        starting_type=gt_reserve,
        current_type=gt_reserve,
    )
    active_group = PlayerGroup.objects.create(
        team=home_team,
        match_data=match_data,
        starting_type=gt_attack,
        current_type=gt_attack,
    )
    active_group.players.add(player_out)
    reserve_group.players.add(player_in)

    apply_tracker_command(
        match,
        team=home_team,
        payload={
            "command": "substitute_reg",
            "new_player_id": str(player_in.id_uuid),
            "old_player_id": str(player_out.id_uuid),
        },
    )

    active_group.refresh_from_db()
    reserve_group.refresh_from_db()
    assert active_group.players.filter(id_uuid=player_in.id_uuid).exists()
    assert not active_group.players.filter(id_uuid=player_out.id_uuid).exists()
    assert reserve_group.players.filter(id_uuid=player_out.id_uuid).exists()

    # Starting the next part should still work.
    apply_tracker_command(match, team=home_team, payload={"command": "start/pause"})
    assert MatchPart.objects.filter(
        match_data=match_data,
        part_number=2,
        active=True,
    ).exists()


@pytest.mark.django_db
def test_timeout_command_requires_for_team_flag() -> None:
    """Timeout requires explicit `for_team` flag in the payload."""
    home_club = Club.objects.create(name="Timeout Req Home Club")
    away_club = Club.objects.create(name="Timeout Req Away Club")
    home_team = Team.objects.create(name="Timeout Req Home Team", club=home_club)
    away_team = Team.objects.create(name="Timeout Req Away Team", club=away_club)

    season = Season.objects.create(
        name="Timeout Req Season",
        start_date=timezone.now().date() - timedelta(days=1),
        end_date=timezone.now().date() + timedelta(days=365),
    )

    match = Match.objects.create(
        home_team=home_team,
        away_team=away_team,
        season=season,
        start_time=timezone.now() - timedelta(minutes=10),
    )

    match_data = MatchData.objects.get(match_link=match)
    match_data.status = "active"
    match_data.save(update_fields=["status"])

    MatchPart.objects.create(
        match_data=match_data,
        part_number=1,
        start_time=datetime.now(UTC),
        active=True,
    )

    with pytest.raises(TrackerCommandError) as exc:
        apply_tracker_command(match, team=home_team, payload={"command": "timeout"})

    assert exc.value.code == "bad_request"


@pytest.mark.django_db
def test_timeout_command_can_register_opponent_timeout_and_counts_in_state() -> None:
    """Opponent timeout registration increments `against` timeout count."""
    home_club = Club.objects.create(name="Timeout Opp Home Club")
    away_club = Club.objects.create(name="Timeout Opp Away Club")
    home_team = Team.objects.create(name="Timeout Opp Home Team", club=home_club)
    away_team = Team.objects.create(name="Timeout Opp Away Team", club=away_club)

    season = Season.objects.create(
        name="Timeout Opp Season",
        start_date=timezone.now().date() - timedelta(days=1),
        end_date=timezone.now().date() + timedelta(days=365),
    )

    match = Match.objects.create(
        home_team=home_team,
        away_team=away_team,
        season=season,
        start_time=timezone.now() - timedelta(minutes=10),
    )

    match_data = MatchData.objects.get(match_link=match)
    match_data.status = "active"
    match_data.save(update_fields=["status"])

    MatchPart.objects.create(
        match_data=match_data,
        part_number=1,
        start_time=datetime.now(UTC),
        active=True,
    )

    apply_tracker_command(
        match,
        team=home_team,
        payload={
            "command": "timeout",
            "for_team": False,
        },
    )

    assert Timeout.objects.filter(match_data=match_data, team=away_team).count() == 1

    state = get_tracker_state(match, team=home_team)
    assert state["timeouts"]["for"] == 0
    assert state["timeouts"]["against"] == 1
    assert state["timeouts"]["max"] == MAX_TIMEOUTS


@pytest.mark.django_db
def test_goal_reg_swaps_attack_defense_every_two_goals() -> None:
    """Every 2 scored goals (total) should swap aanval/verdediging for both teams."""
    home_club = Club.objects.create(name="Swap Home Club")
    away_club = Club.objects.create(name="Swap Away Club")
    home_team = Team.objects.create(name="Swap Home Team", club=home_club)
    away_team = Team.objects.create(name="Swap Away Team", club=away_club)

    season = Season.objects.create(
        name="Swap Season",
        start_date=timezone.now().date() - timedelta(days=1),
        end_date=timezone.now().date() + timedelta(days=365),
    )

    match = Match.objects.create(
        home_team=home_team,
        away_team=away_team,
        season=season,
        start_time=timezone.now() - timedelta(minutes=10),
    )

    match_data = MatchData.objects.get(match_link=match)
    match_data.status = "active"
    match_data.save(update_fields=["status"])

    MatchPart.objects.create(
        match_data=match_data,
        part_number=1,
        start_time=datetime.now(UTC),
        active=True,
    )

    goal_type = GoalType.objects.create(name="Doorloop")

    gt_attack = GroupType.objects.create(name="Aanval")
    gt_defense = GroupType.objects.create(name="Verdediging")

    home_scorer = (
        get_user_model()
        .objects.create_user(
            username="home_scorer_swap",
            password=TEST_PASSWORD,
        )
        .player
    )
    away_scorer = (
        get_user_model()
        .objects.create_user(
            username="away_scorer_swap",
            password=TEST_PASSWORD,
        )
        .player
    )

    home_pg_attack = PlayerGroup.objects.create(
        team=home_team,
        match_data=match_data,
        starting_type=gt_attack,
        current_type=gt_attack,
    )
    home_pg_defense = PlayerGroup.objects.create(
        team=home_team,
        match_data=match_data,
        starting_type=gt_defense,
        current_type=gt_defense,
    )
    home_pg_attack.players.add(home_scorer)

    away_pg_attack = PlayerGroup.objects.create(
        team=away_team,
        match_data=match_data,
        starting_type=gt_attack,
        current_type=gt_attack,
    )
    away_pg_defense = PlayerGroup.objects.create(
        team=away_team,
        match_data=match_data,
        starting_type=gt_defense,
        current_type=gt_defense,
    )
    away_pg_attack.players.add(away_scorer)

    # First goal: no swap
    apply_tracker_command(
        match,
        team=home_team,
        payload={
            "command": "goal_reg",
            "player_id": str(home_scorer.id_uuid),
            "goal_type": str(goal_type.id_uuid),
            "for_team": True,
        },
    )

    home_pg_attack.refresh_from_db()
    home_pg_defense.refresh_from_db()
    away_pg_attack.refresh_from_db()
    away_pg_defense.refresh_from_db()

    assert home_pg_attack.current_type.name == "Aanval"
    assert home_pg_defense.current_type.name == "Verdediging"
    assert away_pg_attack.current_type.name == "Aanval"
    assert away_pg_defense.current_type.name == "Verdediging"

    # Second goal: swap for both teams
    apply_tracker_command(
        match,
        team=home_team,
        payload={
            "command": "goal_reg",
            "player_id": str(home_scorer.id_uuid),
            "goal_type": str(goal_type.id_uuid),
            "for_team": True,
        },
    )

    home_pg_attack.refresh_from_db()
    home_pg_defense.refresh_from_db()
    away_pg_attack.refresh_from_db()
    away_pg_defense.refresh_from_db()

    assert home_pg_attack.current_type.name == "Verdediging"
    assert home_pg_defense.current_type.name == "Aanval"
    assert away_pg_attack.current_type.name == "Verdediging"
    assert away_pg_defense.current_type.name == "Aanval"


@pytest.mark.django_db
def test_remove_last_event_reverts_swap_when_goal_removed() -> None:
    """Removing a scored goal should revert the last aanval/verdediging swap."""
    home_club = Club.objects.create(name="Revert Home Club")
    away_club = Club.objects.create(name="Revert Away Club")
    home_team = Team.objects.create(name="Revert Home Team", club=home_club)
    away_team = Team.objects.create(name="Revert Away Team", club=away_club)

    season = Season.objects.create(
        name="Revert Season",
        start_date=timezone.now().date() - timedelta(days=1),
        end_date=timezone.now().date() + timedelta(days=365),
    )

    match = Match.objects.create(
        home_team=home_team,
        away_team=away_team,
        season=season,
        start_time=timezone.now() - timedelta(minutes=10),
    )

    match_data = MatchData.objects.get(match_link=match)
    match_data.status = "active"
    match_data.save(update_fields=["status"])

    MatchPart.objects.create(
        match_data=match_data,
        part_number=1,
        start_time=datetime.now(UTC),
        active=True,
    )

    goal_type = GoalType.objects.create(name="Vrijebal")

    gt_attack = GroupType.objects.create(name="Aanval")
    gt_defense = GroupType.objects.create(name="Verdediging")

    home_scorer = (
        get_user_model()
        .objects.create_user(
            username="home_scorer_revert",
            password=TEST_PASSWORD,
        )
        .player
    )
    away_scorer = (
        get_user_model()
        .objects.create_user(
            username="away_scorer_revert",
            password=TEST_PASSWORD,
        )
        .player
    )

    home_pg_attack = PlayerGroup.objects.create(
        team=home_team,
        match_data=match_data,
        starting_type=gt_attack,
        current_type=gt_attack,
    )
    home_pg_defense = PlayerGroup.objects.create(
        team=home_team,
        match_data=match_data,
        starting_type=gt_defense,
        current_type=gt_defense,
    )
    home_pg_attack.players.add(home_scorer)

    away_pg_attack = PlayerGroup.objects.create(
        team=away_team,
        match_data=match_data,
        starting_type=gt_attack,
        current_type=gt_attack,
    )
    PlayerGroup.objects.create(
        team=away_team,
        match_data=match_data,
        starting_type=gt_defense,
        current_type=gt_defense,
    )
    away_pg_attack.players.add(away_scorer)

    # Make 2 goals so the swap happens.
    for _ in range(2):
        apply_tracker_command(
            match,
            team=home_team,
            payload={
                "command": "goal_reg",
                "player_id": str(home_scorer.id_uuid),
                "goal_type": str(goal_type.id_uuid),
                "for_team": True,
            },
        )

    home_pg_attack.refresh_from_db()
    home_pg_defense.refresh_from_db()
    assert home_pg_attack.current_type.name == "Verdediging"
    assert home_pg_defense.current_type.name == "Aanval"

    # Remove last (scored) event => back to 1 goal total => revert swap.
    apply_tracker_command(
        match,
        team=home_team,
        payload={"command": "remove_last_event"},
    )

    home_pg_attack.refresh_from_db()
    home_pg_defense.refresh_from_db()

    assert home_pg_attack.current_type.name == "Aanval"
    assert home_pg_defense.current_type.name == "Verdediging"


@pytest.mark.django_db
def test_tracker_state_includes_substitutions_total() -> None:
    """Tracker state exposes substitutions_total and increments on substitute_reg."""
    home_club = Club.objects.create(name="Sub Home Club")
    away_club = Club.objects.create(name="Sub Away Club")
    home_team = Team.objects.create(name="Sub Home Team", club=home_club)
    away_team = Team.objects.create(name="Sub Away Team", club=away_club)

    season = Season.objects.create(
        name="Sub Season",
        start_date=timezone.now().date() - timedelta(days=1),
        end_date=timezone.now().date() + timedelta(days=365),
    )

    match = Match.objects.create(
        home_team=home_team,
        away_team=away_team,
        season=season,
        start_time=timezone.now() - timedelta(minutes=10),
    )

    match_data = MatchData.objects.get(match_link=match)
    match_data.status = "active"
    match_data.save(update_fields=["status"])

    MatchPart.objects.create(
        match_data=match_data,
        part_number=1,
        start_time=datetime.now(UTC),
        active=True,
    )

    gt_attack = GroupType.objects.create(name="Aanval")
    gt_defense = GroupType.objects.create(name="Verdediging")
    gt_reserve = GroupType.objects.create(name="Reserve")

    player_out = (
        get_user_model()
        .objects.create_user(
            username="sub_player_out",
            password=TEST_PASSWORD,
        )
        .player
    )
    player_in = (
        get_user_model()
        .objects.create_user(
            username="sub_player_in",
            password=TEST_PASSWORD,
        )
        .player
    )

    pg_attack = PlayerGroup.objects.create(
        team=home_team,
        match_data=match_data,
        starting_type=gt_attack,
        current_type=gt_attack,
    )
    PlayerGroup.objects.create(
        team=home_team,
        match_data=match_data,
        starting_type=gt_defense,
        current_type=gt_defense,
    )
    pg_reserve = PlayerGroup.objects.create(
        team=home_team,
        match_data=match_data,
        starting_type=gt_reserve,
        current_type=gt_reserve,
    )

    pg_attack.players.add(player_out)
    pg_reserve.players.add(player_in)

    # Ensure field is present and starts at 0.
    initial_state = get_tracker_state(match, team=home_team)
    assert initial_state["substitutions_total"] == 0
    assert initial_state["substitutions"]["for"] == 0
    assert initial_state["substitutions"]["against"] == 0
    assert initial_state["substitutions"]["max"] == MAX_WISSELS

    # Register a substitution.
    next_state = apply_tracker_command(
        match,
        team=home_team,
        payload={
            "command": "substitute_reg",
            "new_player_id": str(player_in.id_uuid),
            "old_player_id": str(player_out.id_uuid),
        },
    )

    assert next_state["substitutions_total"] == 1
    assert next_state["substitutions"]["for"] == 1


@pytest.mark.django_db
def test_substitute_reg_enforces_max_wissels_per_team() -> None:
    """HTTP tracker should enforce max 8 substitutions per team."""
    home_club = Club.objects.create(name="MaxSub Home Club")
    away_club = Club.objects.create(name="MaxSub Away Club")
    home_team = Team.objects.create(name="MaxSub Home Team", club=home_club)
    away_team = Team.objects.create(name="MaxSub Away Team", club=away_club)

    season = Season.objects.create(
        name="MaxSub Season",
        start_date=timezone.now().date() - timedelta(days=1),
        end_date=timezone.now().date() + timedelta(days=365),
    )

    match = Match.objects.create(
        home_team=home_team,
        away_team=away_team,
        season=season,
        start_time=timezone.now() - timedelta(minutes=10),
    )

    match_data = MatchData.objects.get(match_link=match)
    match_data.status = "active"
    match_data.save(update_fields=["status"])

    MatchPart.objects.create(
        match_data=match_data,
        part_number=1,
        start_time=datetime.now(UTC),
        active=True,
    )

    gt_attack = GroupType.objects.create(name="Aanval")
    gt_reserve = GroupType.objects.create(name="Reserve")

    player_a = (
        get_user_model()
        .objects.create_user(
            username="max_sub_a",
            password=TEST_PASSWORD,
        )
        .player
    )
    player_b = (
        get_user_model()
        .objects.create_user(
            username="max_sub_b",
            password=TEST_PASSWORD,
        )
        .player
    )

    pg_attack = PlayerGroup.objects.create(
        team=home_team,
        match_data=match_data,
        starting_type=gt_attack,
        current_type=gt_attack,
    )
    pg_reserve = PlayerGroup.objects.create(
        team=home_team,
        match_data=match_data,
        starting_type=gt_reserve,
        current_type=gt_reserve,
    )

    pg_attack.players.add(player_a)
    pg_reserve.players.add(player_b)

    # Do 8 substitutions by toggling A <-> B.
    for idx in range(8):
        new_player = player_b if idx % 2 == 0 else player_a
        old_player = player_a if idx % 2 == 0 else player_b
        apply_tracker_command(
            match,
            team=home_team,
            payload={
                "command": "substitute_reg",
                "new_player_id": str(new_player.id_uuid),
                "old_player_id": str(old_player.id_uuid),
            },
        )

    state_after = get_tracker_state(match, team=home_team)
    assert state_after["substitutions"]["for"] == MAX_WISSELS

    with pytest.raises(TrackerCommandError):
        apply_tracker_command(
            match,
            team=home_team,
            payload={
                "command": "substitute_reg",
                "new_player_id": str(player_b.id_uuid),
                "old_player_id": str(player_a.id_uuid),
            },
        )


@pytest.mark.django_db
def test_substitute_against_reg_registers_opponent_wissel_without_players() -> None:
    """Registering an opponent wissel should not require player_in/player_out."""
    home_club = Club.objects.create(name="OppSub Home Club")
    away_club = Club.objects.create(name="OppSub Away Club")
    home_team = Team.objects.create(name="OppSub Home Team", club=home_club)
    away_team = Team.objects.create(name="OppSub Away Team", club=away_club)

    season = Season.objects.create(
        name="OppSub Season",
        start_date=timezone.now().date() - timedelta(days=1),
        end_date=timezone.now().date() + timedelta(days=365),
    )

    match = Match.objects.create(
        home_team=home_team,
        away_team=away_team,
        season=season,
        start_time=timezone.now() - timedelta(minutes=10),
    )

    match_data = MatchData.objects.get(match_link=match)
    match_data.status = "active"
    match_data.save(update_fields=["status"])

    MatchPart.objects.create(
        match_data=match_data,
        part_number=1,
        start_time=datetime.now(UTC),
        active=True,
    )

    gt_reserve = GroupType.objects.create(name="Reserve")

    # The marker is attached to the opponent reserve group.
    PlayerGroup.objects.create(
        team=away_team,
        match_data=match_data,
        starting_type=gt_reserve,
        current_type=gt_reserve,
    )

    initial_state = get_tracker_state(match, team=home_team)
    assert initial_state["substitutions"]["against"] == 0

    next_state = apply_tracker_command(
        match,
        team=home_team,
        payload={
            "command": "substitute_against_reg",
        },
    )

    assert next_state["substitutions"]["against"] == 1

    change = (
        PlayerChange.objects
        .filter(match_data=match_data, player_group__team=away_team)
        .order_by("-time")
        .first()
    )
    assert change is not None
    assert change.player_in is None
    assert change.player_out is None


@pytest.mark.django_db
def test_substitute_against_reg_enforces_max_wissels_for_opponent() -> None:
    """Opponent substitution markers should also respect MAX_WISSELS."""
    home_club = Club.objects.create(name="OppSubMax Home Club")
    away_club = Club.objects.create(name="OppSubMax Away Club")
    home_team = Team.objects.create(name="OppSubMax Home Team", club=home_club)
    away_team = Team.objects.create(name="OppSubMax Away Team", club=away_club)

    season = Season.objects.create(
        name="OppSubMax Season",
        start_date=timezone.now().date() - timedelta(days=1),
        end_date=timezone.now().date() + timedelta(days=365),
    )

    match = Match.objects.create(
        home_team=home_team,
        away_team=away_team,
        season=season,
        start_time=timezone.now() - timedelta(minutes=10),
    )

    match_data = MatchData.objects.get(match_link=match)
    match_data.status = "active"
    match_data.save(update_fields=["status"])

    MatchPart.objects.create(
        match_data=match_data,
        part_number=1,
        start_time=datetime.now(UTC),
        active=True,
    )

    gt_reserve = GroupType.objects.create(name="Reserve")
    PlayerGroup.objects.create(
        team=away_team,
        match_data=match_data,
        starting_type=gt_reserve,
        current_type=gt_reserve,
    )

    for _ in range(MAX_WISSELS):
        apply_tracker_command(
            match,
            team=home_team,
            payload={
                "command": "substitute_against_reg",
            },
        )

    state_after = get_tracker_state(match, team=home_team)
    assert state_after["substitutions"]["against"] == MAX_WISSELS

    with pytest.raises(TrackerCommandError):
        apply_tracker_command(
            match,
            team=home_team,
            payload={
                "command": "substitute_against_reg",
            },
        )


@pytest.mark.django_db
def test_client_time_keeps_last_event_order_stable() -> None:
    """Client timestamps should drive event ordering.

    Regression: when multiple commands are sent quickly, server-side `now()` can
    cause later-arriving requests to appear as the last event even if the user
    action happened earlier.
    """
    home_club = Club.objects.create(name="ClientTime Home Club")
    away_club = Club.objects.create(name="ClientTime Away Club")
    home_team = Team.objects.create(name="ClientTime Home Team", club=home_club)
    away_team = Team.objects.create(name="ClientTime Away Team", club=away_club)

    season = Season.objects.create(
        name="ClientTime Season",
        start_date=timezone.now().date() - timedelta(days=1),
        end_date=timezone.now().date() + timedelta(days=365),
    )

    match = Match.objects.create(
        home_team=home_team,
        away_team=away_team,
        season=season,
        start_time=timezone.now() - timedelta(minutes=10),
    )

    match_data = MatchData.objects.get(match_link=match)
    match_data.status = "active"
    match_data.current_part = 1
    match_data.save(update_fields=["status", "current_part"])

    MatchPart.objects.create(
        match_data=match_data,
        part_number=1,
        start_time=datetime.now(UTC),
        active=True,
    )

    base = datetime.now(UTC).replace(microsecond=0)
    late = base + timedelta(seconds=2)
    early = base - timedelta(seconds=2)

    apply_tracker_command(
        match,
        team=home_team,
        payload={
            "command": "new_attack",
            "client_time_ms": int(late.timestamp() * 1000),
        },
    )

    # Intentionally send an earlier client time *after* the later one.
    apply_tracker_command(
        match,
        team=home_team,
        payload={
            "command": "timeout",
            "for_team": True,
            "client_time_ms": int(early.timestamp() * 1000),
        },
    )

    state = get_tracker_state(match, team=home_team)
    assert state["last_event"]["type"] == "attack"
    assert state["last_event"]["time_iso"] == late.isoformat()
