"""Fast tests for framework-independent tracker rules."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from apps.game_tracker.domain.command_time import command_time_from_payload
from apps.game_tracker.domain.impact_scoring import (
    goal_points,
    next_streak_state,
    opposing_side,
    round_js_1dp,
    shot_impact_weights_for_version,
)


def test_command_time_accepts_credible_client_time() -> None:
    """A nearby client timestamp preserves event ordering."""
    server_now = datetime(2026, 1, 1, 12, tzinfo=UTC)
    client_now = server_now - timedelta(seconds=20)

    result = command_time_from_payload(
        {"client_time_iso": client_now.isoformat()},
        server_now=server_now,
    )

    assert result == client_now


def test_command_time_rejects_excessive_clock_skew() -> None:
    """Untrusted client clocks cannot move events outside the skew window."""
    server_now = datetime(2026, 1, 1, 12, tzinfo=UTC)

    result = command_time_from_payload(
        {"client_time_ms": int((server_now + timedelta(hours=1)).timestamp() * 1000)},
        server_now=server_now,
    )

    assert result == server_now


def test_impact_policy_preserves_version_and_streak_rules() -> None:
    """Scoring rules are usable without Django or persistence."""
    assert shot_impact_weights_for_version("v6").miss_for_penalty == pytest.approx(0.2)
    assert (
        opposing_side(
            team_id="home-id",
            home_team_id="home-id",
            away_team_id="away-id",
        )
        == "away"
    )
    assert next_streak_state(
        scoring_team_id="home-id",
        last_team_id="home-id",
        streak=2,
    ) == ("home-id", 3)
    assert round_js_1dp(goal_points(goal_type="doorloopbal", streak=1)) == Decimal(
        "4.0"
    )
