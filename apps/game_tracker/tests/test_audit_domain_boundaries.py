"""Boundary tests for framework-independent tracker domain policy."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from apps.game_tracker.domain.command_time import (
    CLIENT_TIME_MAX_SKEW_SECONDS,
    command_time_from_payload,
    parse_client_time_iso,
)
from apps.game_tracker.domain.impact_scoring import (
    MatchImpactContribution,
    ShootingEfficiencyMultipliers,
    advance_score_state,
    aggregate_win_probability_added,
    compute_v7_contributions,
    compute_v8_contributions,
    conceding_side_for_goal,
    defending_side_for_shot,
    doorloop_concede_factor_for_version,
    efficiency_multipliers_for_rate,
    goal_points,
    next_streak_state,
    opposing_side,
    shot_impact_weights_for_version,
)
from apps.game_tracker.domain.win_probability import match_outcome_probabilities


@pytest.mark.parametrize("offset_seconds", [-300, 300])
def test_client_time_accepts_both_edges_of_the_clock_skew_window(
    offset_seconds: int,
) -> None:
    """The documented five-minute limit is inclusive in either direction."""
    server_now = datetime(2026, 1, 1, 12, tzinfo=UTC)
    client_now = server_now + timedelta(seconds=offset_seconds)

    assert (
        command_time_from_payload(
            {"client_time_ms": int(client_now.timestamp() * 1000)},
            server_now=server_now,
        )
        == client_now
    )


@pytest.mark.parametrize("direction", [-1, 1])
def test_client_time_rejects_values_just_outside_the_clock_skew_window(
    direction: int,
) -> None:
    """One second outside either trust boundary falls back to server time."""
    server_now = datetime(2026, 1, 1, 12, tzinfo=UTC)
    client_now = server_now + timedelta(
        seconds=direction * (CLIENT_TIME_MAX_SKEW_SECONDS + 1)
    )

    assert (
        command_time_from_payload(
            {"client_time_iso": client_now.isoformat()},
            server_now=server_now,
        )
        == server_now
    )


def test_invalid_client_milliseconds_fall_back_to_a_valid_iso_timestamp() -> None:
    """A corrupt preferred representation must not hide a credible fallback."""
    server_now = datetime(2026, 1, 1, 12, tzinfo=UTC)
    client_now = server_now - timedelta(seconds=10)

    assert (
        command_time_from_payload(
            {
                "client_time_ms": 10**100,
                "client_time_iso": client_now.isoformat(),
            },
            server_now=server_now,
        )
        == client_now
    )


def test_iso_parser_normalizes_offsets_and_treats_naive_values_as_utc() -> None:
    """All accepted client timestamps enter the domain as UTC-aware values."""
    assert parse_client_time_iso("2026-01-01T13:00:00+01:00") == datetime(
        2026, 1, 1, 12, tzinfo=UTC
    )
    assert parse_client_time_iso(" 2026-01-01T12:00:00 ") == datetime(
        2026, 1, 1, 12, tzinfo=UTC
    )
    assert parse_client_time_iso("not-a-time") is None


@pytest.mark.parametrize(
    "payload",
    [{}, {"client_time_ms": "not-an-integer", "client_time_iso": "not-a-time"}],
)
def test_command_time_uses_server_time_when_no_client_time_is_usable(
    payload: dict[str, object],
) -> None:
    """Missing and malformed client clocks both use the authoritative clock."""
    server_now = datetime(2026, 1, 1, 12, tzinfo=UTC)

    assert command_time_from_payload(payload, server_now=server_now) == server_now


@pytest.mark.parametrize(
    ("goals", "shots", "expected"),
    [
        (4, 4, ShootingEfficiencyMultipliers(1.0, 1.0)),
        (3, 5, ShootingEfficiencyMultipliers(1.2, 0.7)),
        (2, 6, ShootingEfficiencyMultipliers(1.1, 0.85)),
        (1, 5, ShootingEfficiencyMultipliers(1.0, 1.0)),
        (0, 5, ShootingEfficiencyMultipliers(0.9, 1.15)),
    ],
)
def test_shooting_efficiency_thresholds_have_stable_boundary_behavior(
    goals: int,
    shots: int,
    expected: ShootingEfficiencyMultipliers,
) -> None:
    """Small samples are neutral and each configured rate band is covered."""
    assert efficiency_multipliers_for_rate(goals=goals, shots=shots) == expected


def test_shot_scoring_ignores_unattributed_rows_without_affecting_valid_rows() -> None:
    """Historical incomplete shots cannot create a blank-player impact bucket."""
    contributions = compute_v7_contributions([
        {"player_id": "  ", "scored": True, "for_team": True},
        {
            "player_id": "scorer",
            "scored": True,
            "for_team": True,
            "shot_type": "Vrije bal",
        },
    ])

    assert [item.player_id for item in contributions] == ["scorer"]
    assert contributions[0].points == pytest.approx(0.65)


@pytest.mark.parametrize(
    ("scoring_team_id", "expected"),
    [("home", (9, 7)), ("away", (8, 8)), ("spectator-team", (8, 7))],
)
def test_only_a_participating_scoring_team_can_mutate_the_score(
    scoring_team_id: str,
    expected: tuple[int, int],
) -> None:
    """Home and away advance independently while foreign teams are no-ops."""
    assert (
        advance_score_state(
            home_score=8,
            away_score=7,
            scoring_team_id=scoring_team_id,
            home_team_id="home",
            away_team_id="away",
        )
        == expected
    )


@pytest.mark.parametrize(
    ("scoring_team_id", "expected"),
    [("away", ("away", 1)), (None, (None, 1))],
)
def test_scoring_streak_resets_when_the_scoring_team_changes_or_is_unknown(
    scoring_team_id: str | None,
    expected: tuple[str | None, int],
) -> None:
    """A stale streak cannot carry over to another or unidentified team."""
    assert (
        next_streak_state(
            scoring_team_id=scoring_team_id,
            last_team_id="home",
            streak=4,
        )
        == expected
    )


@pytest.mark.parametrize(
    ("goal_delay_seconds", "goal_team", "goal_part", "expected_bonus"),
    [
        (10, "home", "part-1", True),
        (11, "home", "part-1", False),
        (5, "away", "part-1", False),
        (5, "home", "part-2", False),
    ],
)
def test_fast_goal_bonus_respects_time_team_and_period_boundaries(
    goal_delay_seconds: int,
    goal_team: str,
    goal_part: str,
    expected_bonus: bool,
) -> None:
    """Transition credit applies only to the same attack within ten seconds."""
    start = datetime(2026, 1, 1, 12, tzinfo=UTC)
    contributions = compute_v8_contributions(
        [],
        [
            {
                "type": "possession_change",
                "kind": "interception",
                "player_id": "interceptor",
                "team_id": "home",
                "match_part_id": "part-1",
                "time_iso": start.isoformat(),
            },
            {
                "type": "goal",
                "event_id": "candidate-goal",
                "team_id": goal_team,
                "match_part_id": goal_part,
                "time_iso": (start + timedelta(seconds=goal_delay_seconds)).isoformat(),
            },
        ],
    )

    contribution = contributions[0]
    assert (contribution.transition_bonus > 0) is expected_bonus
    assert (contribution.linked_goal_event_id == "candidate-goal") is expected_bonus


def test_fast_goal_scan_ignores_mixed_naive_and_aware_timestamps() -> None:
    """One malformed client timestamp cannot crash match-impact recomputation."""
    contributions = compute_v8_contributions(
        [],
        [
            {
                "type": "possession_change",
                "kind": "interception",
                "player_id": "interceptor",
                "team_id": "home",
                "time_iso": "2026-01-01T12:00:00+00:00",
            },
            {
                "type": "goal",
                "event_id": "malformed-client-goal",
                "team_id": "home",
                "time_iso": "2026-01-01T12:00:05",
            },
        ],
    )

    assert contributions[0].points == pytest.approx(0.18)
    assert contributions[0].transition_bonus == pytest.approx(0.0)
    assert contributions[0].linked_goal_event_id is None


def test_away_goal_wpa_is_zero_sum_from_each_players_perspective() -> None:
    """Away scoring attribution correctly inverts home win expectancy."""
    contributions = compute_v8_contributions(
        [
            {
                "event_id": "away-goal",
                "player_id": "away-scorer",
                "team_id": "away",
                "shot_type": "Afstand schot",
                "scored": True,
                "for_team": True,
            },
            {
                "event_id": "away-goal",
                "player_id": "home-defender",
                "team_id": "away",
                "shot_type": "Afstand schot",
                "scored": True,
                "for_team": False,
            },
        ],
        [
            {
                "type": "goal",
                "event_id": "away-goal",
                "team_id": "away",
                "elapsed_seconds": 3590,
            }
        ],
        match_duration_minutes=60,
        home_team_id="home",
        away_team_id="away",
    )
    by_player = {item.player_id: item for item in contributions}

    assert by_player["away-scorer"].win_probability_added > 0
    assert by_player["home-defender"].win_probability_added < 0
    assert (
        by_player["away-scorer"].win_probability_added
        + by_player["home-defender"].win_probability_added
    ) == pytest.approx(0.0)


@pytest.mark.parametrize("non_finite_elapsed", [float("nan"), float("inf")])
def test_non_finite_elapsed_time_uses_the_match_minute_fallback(
    non_finite_elapsed: float,
) -> None:
    """Malformed numeric clocks cannot suppress otherwise usable WPA timing."""
    contributions = compute_v8_contributions(
        [],
        [
            {
                "type": "possession_change",
                "event_id": "late-interception",
                "kind": "interception",
                "player_id": "interceptor",
                "team_id": "home",
                "elapsed_seconds": non_finite_elapsed,
                "time": "59",
            }
        ],
        match_duration_minutes=60,
        home_team_id="home",
        away_team_id="away",
    )

    assert contributions[0].win_probability_added > 0
    assert contributions[0].win_expectancy_before is not None
    assert contributions[0].win_expectancy_after is not None


def test_added_time_is_included_when_applying_late_game_leverage() -> None:
    """A ``54+2`` label is treated as minute 56, inside the final window."""
    contributions = compute_v8_contributions(
        [],
        [
            {
                "type": "possession_change",
                "kind": "ball_loss",
                "player_id": "late-loser",
                "team_id": "home",
                "time": "54+2",
            }
        ],
        match_duration_minutes=60,
    )

    assert contributions[0].leverage_multiplier == pytest.approx(1.75)


def test_wpa_aggregation_sums_each_players_event_contributions() -> None:
    """Positive and negative event WPA accumulate independently per player."""
    contributions = [
        MatchImpactContribution(
            player_id="one",
            time="1",
            category="possession_gain",
            points=0.18,
            source_type="possession_change",
            win_probability_added=0.2,
        ),
        MatchImpactContribution(
            player_id="one",
            time="2",
            category="possession_loss",
            points=-0.18,
            source_type="possession_change",
            win_probability_added=-0.05,
        ),
        MatchImpactContribution(
            player_id="two",
            time="3",
            category="defense_goal_below_expected",
            points=-0.82,
            source_type="shot",
            win_probability_added=-0.3,
        ),
    ]

    assert aggregate_win_probability_added(contributions) == pytest.approx({
        "one": 0.15,
        "two": -0.3,
    })


@pytest.mark.parametrize(
    ("goal_type", "expected_points"),
    [
        ("Strafworp", 1.76),
        ("Vrije bal", 2.08),
        ("Korte kans", 4.32),
        ("Doorloopbal", 4.0),
        ("1/2 afstand", 3.52),
        ("Afstand schot", 3.04),
        ("Onbekend", 3.2),
    ],
)
def test_goal_type_weights_cover_each_historical_scoring_family(
    goal_type: str,
    expected_points: float,
) -> None:
    """Historical labels retain their relative scoring values."""
    assert goal_points(goal_type=goal_type, streak=1) == pytest.approx(expected_points)


def test_goal_streak_multiplier_is_clamped_at_both_ends() -> None:
    """Malformed low streaks and oversized streaks stay within policy bounds."""
    assert goal_points(goal_type="Onbekend", streak=-3) == pytest.approx(3.2)
    assert goal_points(goal_type="Onbekend", streak=4) == pytest.approx(4.352)
    assert goal_points(goal_type="Onbekend", streak=99) == pytest.approx(4.352)


@pytest.mark.parametrize(
    ("version", "expected"),
    [
        ("v1", (0.9, -0.25, -6.2, 0.55)),
        ("v4", (0.6, -0.25, -6.2, 0.8)),
        ("v6", (0.2, -0.17, -2.94, 0.31)),
        ("future-version", (0.2, -0.17, -2.94, 0.31)),
    ],
)
def test_legacy_shot_policy_dispatch_has_a_safe_default(
    version: str,
    expected: tuple[float, float, float, float],
) -> None:
    """Unknown stored versions use the latest legacy-compatible weights."""
    weights = shot_impact_weights_for_version(version)

    assert (
        weights.miss_for_penalty,
        weights.shot_against_total,
        weights.goal_against_total,
        weights.miss_against_total,
    ) == expected


@pytest.mark.parametrize(
    ("team_id", "expected_side"),
    [("home", "away"), ("away", "home"), ("outsider", None), (None, None)],
)
def test_defending_and_conceding_side_aliases_share_the_opposition_rule(
    team_id: str | None,
    expected_side: str | None,
) -> None:
    """All defensive attribution entry points reject non-participating teams."""
    kwargs = {
        "home_team_id": "home",
        "away_team_id": "away",
    }

    assert opposing_side(team_id=team_id, **kwargs) == expected_side
    assert defending_side_for_shot(shot_team_id=team_id, **kwargs) == expected_side
    assert conceding_side_for_goal(scoring_team_id=team_id, **kwargs) == expected_side


def test_doorloop_concede_factor_only_exempts_v6() -> None:
    """The legacy v6 exception does not leak into newer policy names."""
    assert doorloop_concede_factor_for_version("v6") == pytest.approx(0.0)
    assert doorloop_concede_factor_for_version("v8") == pytest.approx(0.06)


def test_negative_remaining_time_uses_the_terminal_away_win() -> None:
    """Overrun clocks cannot give a trailing home team another scoring chance."""
    probabilities = match_outcome_probabilities(
        home_score=10,
        away_score=11,
        seconds_remaining=-0.1,
        possession="home",
    )

    assert probabilities.home_win == pytest.approx(0.0)
    assert probabilities.draw == pytest.approx(0.0)
    assert probabilities.away_win == pytest.approx(1.0)
