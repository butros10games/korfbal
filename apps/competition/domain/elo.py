"""Reproducible Elo baseline, isolated by connected schedules and sport."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from itertools import groupby


INITIAL_RATING = 1500.0
K_FACTOR = 24.0
RATING_SCALE = 400.0
PROVISIONAL_GAMES = 10
MODEL_VERSION = "elo-v1"


@dataclass(frozen=True)
class RatedResult:
    """A completed, non-awarded match with two known scores."""

    source_id: str
    starts_at: datetime
    home: int
    away: int
    home_score: int
    away_score: int


@dataclass
class Rating:
    """A relative score and its sample size; never a calibrated win percentage."""

    value: float = INITIAL_RATING
    games: int = 0
    comparison_group: int = 0


def calculate(sports: dict[int, str], matches: list[RatedResult]) -> dict[int, Rating]:
    """Rebuild scores chronologically so corrections cannot be double counted.

    Matches sharing a timestamp use the ratings at the start of that timestamp,
    eliminating arbitrary provider-ID order effects. No uncalibrated home bonus
    or goal-margin multiplier is applied.
    """
    ratings = {team: Rating(comparison_group=team) for team in sports}
    parents = {team: team for team in sports}
    ordered = sorted(matches, key=lambda match: (match.starts_at, match.source_id))
    for _, simultaneous in groupby(ordered, key=lambda match: match.starts_at):
        changes: dict[int, float] = defaultdict(float)
        for match in simultaneous:
            if not _eligible(match, sports):
                continue
            home = ratings[match.home]
            away = ratings[match.away]
            gap = (away.value - home.value) / RATING_SCALE
            expected = 1 / (1 + 10 ** max(-100, min(100, gap)))
            outcome = (
                0.5
                if match.home_score == match.away_score
                else float(match.home_score > match.away_score)
            )
            change = K_FACTOR * (outcome - expected)
            changes[match.home] += change
            changes[match.away] -= change
            home.games += 1
            away.games += 1
            first, second = _root(parents, match.home), _root(parents, match.away)
            parents[max(first, second)] = min(first, second)
        for team, change in changes.items():
            ratings[team].value += change
    for team, rating in ratings.items():
        rating.comparison_group = _root(parents, team)
    return ratings


def _root(parents: dict[int, int], team: int) -> int:
    """Resolve a connected schedule group with path compression."""
    while parents[team] != team:
        parents[team] = parents[parents[team]]
        team = parents[team]
    return team


def _eligible(match: RatedResult, sports: dict[int, str]) -> bool:
    """Exclude malformed or cross-sport records from the rating population."""
    return (
        match.home != match.away
        and match.home in sports
        and match.away in sports
        and bool(sports[match.home])
        and sports[match.home] == sports[match.away]
        and min(match.home_score, match.away_score) >= 0
    )
