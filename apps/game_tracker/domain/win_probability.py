"""Framework-independent Korfbal win/draw/loss probability model."""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
import math
from typing import Literal


WPA_MODEL_VERSION = "poisson-possession-v1"
WPA_GOALS_PER_TEAM_MINUTE = 0.35
WPA_POSSESSION_GOAL_PROBABILITY = 0.18

Side = Literal["home", "away"]


@dataclass(frozen=True)
class MatchOutcomeProbabilities:
    """Terminal result probabilities from the home team's perspective."""

    home_win: float
    draw: float
    away_win: float

    def expectancy(self, side: Side) -> float:
        """Return expected result share, treating a draw as half a win."""
        if side == "home":
            return self.home_win + 0.5 * self.draw
        return self.away_win + 0.5 * self.draw


def _terminal_outcome(*, home_score: int, away_score: int) -> MatchOutcomeProbabilities:
    if home_score > away_score:
        return MatchOutcomeProbabilities(home_win=1.0, draw=0.0, away_win=0.0)
    if home_score < away_score:
        return MatchOutcomeProbabilities(home_win=0.0, draw=0.0, away_win=1.0)
    return MatchOutcomeProbabilities(home_win=0.0, draw=1.0, away_win=0.0)


@lru_cache(maxsize=4096)
def _poisson_distribution(expected_goals_milli: int) -> tuple[float, ...]:
    expected_goals = max(0.0, expected_goals_milli / 1000.0)
    if expected_goals == 0:
        return (1.0,)

    max_goals = max(
        12,
        math.ceil(expected_goals + 8 * math.sqrt(expected_goals) + 8),
    )
    probabilities = [math.exp(-expected_goals)]
    for goals in range(1, max_goals + 1):
        probabilities.append(probabilities[-1] * expected_goals / goals)

    total = sum(probabilities)
    if total <= 0:
        return (1.0,)
    return tuple(probability / total for probability in probabilities)


@lru_cache(maxsize=16384)
def _score_only_outcome(
    *,
    home_score: int,
    away_score: int,
    seconds_remaining: int,
) -> MatchOutcomeProbabilities:
    if seconds_remaining <= 0:
        return _terminal_outcome(home_score=home_score, away_score=away_score)

    expected_goals = WPA_GOALS_PER_TEAM_MINUTE * max(0, seconds_remaining) / 60.0
    distribution = _poisson_distribution(round(expected_goals * 1000))
    home_win = 0.0
    draw = 0.0
    away_win = 0.0
    for home_goals, home_probability in enumerate(distribution):
        for away_goals, away_probability in enumerate(distribution):
            probability = home_probability * away_probability
            final_home = home_score + home_goals
            final_away = away_score + away_goals
            if final_home > final_away:
                home_win += probability
            elif final_home < final_away:
                away_win += probability
            else:
                draw += probability

    total = home_win + draw + away_win
    return MatchOutcomeProbabilities(
        home_win=home_win / total,
        draw=draw / total,
        away_win=away_win / total,
    )


def match_outcome_probabilities(
    *,
    home_score: int,
    away_score: int,
    seconds_remaining: float,
    possession: Side | None,
) -> MatchOutcomeProbabilities:
    """Estimate win/draw/loss from score, time, and the current possession.

    The remaining score follows independent Poisson processes. Current
    possession adds one Bernoulli open-play opportunity to the possessing team,
    which makes a possession change immediately valuable without assigning a
    later goal to the player who won the ball.
    """
    remaining = max(0, round(seconds_remaining))
    without_possession = _score_only_outcome(
        home_score=home_score,
        away_score=away_score,
        seconds_remaining=remaining,
    )
    if possession is None or remaining <= 0:
        return without_possession

    with_possession_goal = _score_only_outcome(
        home_score=home_score + (1 if possession == "home" else 0),
        away_score=away_score + (1 if possession == "away" else 0),
        seconds_remaining=remaining,
    )
    goal_probability = WPA_POSSESSION_GOAL_PROBABILITY
    miss_probability = 1.0 - goal_probability
    return MatchOutcomeProbabilities(
        home_win=(
            without_possession.home_win * miss_probability
            + with_possession_goal.home_win * goal_probability
        ),
        draw=(
            without_possession.draw * miss_probability
            + with_possession_goal.draw * goal_probability
        ),
        away_win=(
            without_possession.away_win * miss_probability
            + with_possession_goal.away_win * goal_probability
        ),
    )
