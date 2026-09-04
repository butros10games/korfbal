"""Determine provisional and mathematically secured tournament qualifiers."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum

from apps.tournament.models import Tournament, TournamentMatch, TournamentPool
from apps.tournament.services.standings import (
    StandingRow,
    calculate_pool_standings,
    rank_rows_across_pools,
)


MAX_MATCH_SCORE = 999


@dataclass(frozen=True, slots=True)
class QualifierDecision:
    """The visible leader and optional guaranteed entrant for one source."""

    current_team_id: str | None
    decided_team_id: str | None

    @property
    def is_decided(self) -> bool:
        """Return whether the qualifier can safely be written to a bracket."""
        return self.decided_team_id is not None


type _ValueBounds = dict[str, tuple[int, int]]


@dataclass(frozen=True, slots=True)
class _ComparisonContext:
    tournament: Tournament
    bounds: dict[str, _ValueBounds]
    pool: TournamentPool | None


class _RuleOutcome(Enum):
    LEFT = "left"
    RIGHT = "right"
    EQUAL = "equal"
    UNKNOWN = "unknown"


def _pool_standings_match(pool: TournamentPool, match: TournamentMatch) -> bool:
    pool_team_ids = {entry.team_id for entry in pool.entries.all()}
    return match.home_team_id in pool_team_ids and match.away_team_id in pool_team_ids


def _valid_final_matches(pool: TournamentPool) -> list[TournamentMatch]:
    return [
        match
        for match in pool.matches.all()
        if match.status == TournamentMatch.Status.FINAL
        and match.home_team_id is not None
        and match.away_team_id is not None
        and match.home_score is not None
        and match.away_score is not None
        and _pool_standings_match(pool, match)
    ]


def _remaining_matches(pool: TournamentPool) -> list[TournamentMatch]:
    return [
        match
        for match in pool.matches.all()
        if match.status
        not in {TournamentMatch.Status.FINAL, TournamentMatch.Status.CANCELLED}
        and match.home_team_id is not None
        and match.away_team_id is not None
        and _pool_standings_match(pool, match)
    ]


def _rules(tournament: Tournament, *, across_pools: bool = False) -> list[str]:
    allowed = {"points", "goal_difference", "goals_for", "seed", "name"}
    if not across_pools:
        allowed.add("head_to_head")
    rules = [rule for rule in tournament.tiebreakers if rule in allowed]
    if "name" not in rules:
        rules.append("name")
    return rules


def _bounds_for_pool(
    pool: TournamentPool,
    standings: list[StandingRow],
) -> dict[str, _ValueBounds]:
    remaining_by_team = {row["team_id"]: 0 for row in standings}
    for match in _remaining_matches(pool):
        for team_id in (str(match.home_team_id), str(match.away_team_id)):
            if team_id in remaining_by_team:
                remaining_by_team[team_id] += 1

    possible_points = (
        pool.tournament.win_points,
        pool.tournament.draw_points,
        pool.tournament.loss_points,
    )
    minimum_points = min(possible_points)
    maximum_points = max(possible_points)
    bounds: dict[str, _ValueBounds] = {}
    for row in standings:
        remaining = remaining_by_team[row["team_id"]]
        bounds[row["team_id"]] = {
            "points": (
                row["points"] + remaining * minimum_points,
                row["points"] + remaining * maximum_points,
            ),
            "goal_difference": (
                row["goal_difference"] - remaining * MAX_MATCH_SCORE,
                row["goal_difference"] + remaining * MAX_MATCH_SCORE,
            ),
            "goals_for": (
                row["goals_for"],
                row["goals_for"] + remaining * MAX_MATCH_SCORE,
            ),
        }
    return bounds


def _match_points(
    tournament: Tournament,
    home_score: int,
    away_score: int,
) -> tuple[int, int]:
    if home_score > away_score:
        return tournament.win_points, tournament.loss_points
    if away_score > home_score:
        return tournament.loss_points, tournament.win_points
    return tournament.draw_points, tournament.draw_points


def _head_to_head_value(
    pool: TournamentPool,
    left_id: str,
    right_id: str,
) -> tuple[tuple[int, int], tuple[int, int]] | None:
    relevant = [
        match
        for match in pool.matches.all()
        if {str(match.home_team_id), str(match.away_team_id)} == {left_id, right_id}
    ]
    if any(
        match.status
        not in {TournamentMatch.Status.FINAL, TournamentMatch.Status.CANCELLED}
        for match in relevant
    ):
        return None

    direct: dict[str, tuple[int, int]] = {}
    for match in relevant:
        if (
            match.status != TournamentMatch.Status.FINAL
            or match.home_score is None
            or match.away_score is None
        ):
            continue
        home_points, away_points = _match_points(
            pool.tournament,
            match.home_score,
            match.away_score,
        )
        direct[str(match.home_team_id)] = (
            home_points,
            match.home_score - match.away_score,
        )
        direct[str(match.away_team_id)] = (
            away_points,
            match.away_score - match.home_score,
        )
    return direct.get(left_id, (0, 0)), direct.get(right_id, (0, 0))


def _numeric_outcome(
    rule: str,
    left: StandingRow,
    right: StandingRow,
    context: _ComparisonContext,
) -> _RuleOutcome:
    left_low, left_high = context.bounds[left["team_id"]][rule]
    right_low, right_high = context.bounds[right["team_id"]][rule]
    if left_low > right_high:
        return _RuleOutcome.LEFT
    if left_high < right_low:
        return _RuleOutcome.RIGHT
    if left_low == left_high == right_low == right_high:
        return _RuleOutcome.EQUAL
    return _RuleOutcome.UNKNOWN


def _ordered_outcome(comparison: int) -> _RuleOutcome:
    if comparison == 0:
        return _RuleOutcome.EQUAL
    return _RuleOutcome.LEFT if comparison > 0 else _RuleOutcome.RIGHT


def _rule_outcome(
    rule: str,
    left: StandingRow,
    right: StandingRow,
    context: _ComparisonContext,
) -> _RuleOutcome:
    if rule in {"points", "goal_difference", "goals_for"}:
        return _numeric_outcome(rule, left, right, context)
    if rule == "head_to_head":
        if context.pool is None:
            return _RuleOutcome.EQUAL
        values = _head_to_head_value(
            context.pool,
            left["team_id"],
            right["team_id"],
        )
        if values is None:
            return _RuleOutcome.UNKNOWN
        left_value, right_value = values
        return _ordered_outcome(
            0 if left_value == right_value else 1 if left_value > right_value else -1
        )
    if rule == "seed":
        return _ordered_outcome(right["seed"] - left["seed"])
    left_name = left["team_name"].casefold()
    right_name = right["team_name"].casefold()
    return _ordered_outcome(
        0 if left_name == right_name else 1 if left_name < right_name else -1
    )


def _guaranteed_before(
    left: StandingRow,
    right: StandingRow,
    context: _ComparisonContext,
) -> bool:
    for rule in _rules(
        context.tournament,
        across_pools=context.pool is None,
    ):
        outcome = _rule_outcome(rule, left, right, context)
        if outcome == _RuleOutcome.LEFT:
            return True
        if outcome in {_RuleOutcome.RIGHT, _RuleOutcome.UNKNOWN}:
            return False
    return False


def _guaranteed_at_rank(
    standings: list[StandingRow],
    bounds: dict[str, _ValueBounds],
    *,
    tournament: Tournament,
    rank: int,
    pool: TournamentPool | None,
) -> StandingRow | None:
    context = _ComparisonContext(tournament=tournament, bounds=bounds, pool=pool)
    for candidate in standings:
        others = [row for row in standings if row["team_id"] != candidate["team_id"]]
        guaranteed_above = sum(
            _guaranteed_before(
                other,
                candidate,
                context,
            )
            for other in others
        )
        guaranteed_below = sum(
            _guaranteed_before(
                candidate,
                other,
                context,
            )
            for other in others
        )
        if guaranteed_above == rank - 1 and guaranteed_below == len(standings) - rank:
            return candidate
    return None


def evaluate_pool_rank(
    pool: TournamentPool,
    rank: int,
    *,
    standings: list[StandingRow] | None = None,
) -> QualifierDecision:
    """Return the current and guaranteed team at an exact pool rank."""
    standings = standings or calculate_pool_standings(pool)
    if rank < 1 or rank > len(standings) or not _valid_final_matches(pool):
        return QualifierDecision(None, None)

    current = standings[rank - 1]
    remaining = _remaining_matches(pool)
    if not remaining:
        return QualifierDecision(current["team_id"], current["team_id"])

    decided = _guaranteed_at_rank(
        standings,
        _bounds_for_pool(pool, standings),
        tournament=pool.tournament,
        rank=rank,
        pool=pool,
    )
    decided_id = decided["team_id"] if decided else None
    return QualifierDecision(decided_id or current["team_id"], decided_id)


def evaluate_best_rank(
    tournament: Tournament,
    pools: list[TournamentPool],
    rank: int,
    *,
    standings_by_pool_id: Mapping[str, list[StandingRow]] | None = None,
) -> QualifierDecision:
    """Return the best current and guaranteed same-rank team across pools."""
    standings_by_pool = {
        str(pool.id_uuid): (
            standings_by_pool_id[str(pool.id_uuid)]
            if standings_by_pool_id is not None
            else calculate_pool_standings(pool)
        )
        for pool in pools
    }
    evaluations = [
        evaluate_pool_rank(
            pool,
            rank,
            standings=standings_by_pool[str(pool.id_uuid)],
        )
        for pool in pools
    ]
    if any(evaluation.current_team_id is None for evaluation in evaluations):
        return QualifierDecision(None, None)

    current_rows = [
        next(
            row
            for row in standings_by_pool[str(pool.id_uuid)]
            if row["team_id"] == evaluation.current_team_id
        )
        for pool, evaluation in zip(pools, evaluations, strict=True)
    ]
    current = rank_rows_across_pools(tournament, current_rows)[0]
    if any(evaluation.decided_team_id is None for evaluation in evaluations):
        return QualifierDecision(current["team_id"], None)

    decided_rows: list[StandingRow] = []
    decided_bounds: dict[str, _ValueBounds] = {}
    for pool, evaluation in zip(pools, evaluations, strict=True):
        standings = standings_by_pool[str(pool.id_uuid)]
        row = next(
            item for item in standings if item["team_id"] == evaluation.decided_team_id
        )
        decided_rows.append(row)
        decided_bounds[row["team_id"]] = _bounds_for_pool(pool, standings)[
            row["team_id"]
        ]

    decided = _guaranteed_at_rank(
        decided_rows,
        decided_bounds,
        tournament=tournament,
        rank=1,
        pool=None,
    )
    decided_id = decided["team_id"] if decided else None
    return QualifierDecision(decided_id or current["team_id"], decided_id)
