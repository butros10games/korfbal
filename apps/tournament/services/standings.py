"""Pool standings calculated from finalized or provisional tournament results."""

from __future__ import annotations

from collections.abc import Callable
from functools import cmp_to_key
from typing import TypedDict

from apps.tournament.models import Tournament, TournamentMatch, TournamentPool


class StandingRow(TypedDict):
    """One calculated public pool-table row."""

    team_id: str
    team_name: str
    short_name: str
    played: int
    won: int
    drawn: int
    lost: int
    goals_for: int
    goals_against: int
    goal_difference: int
    points: int
    adjustment: int
    position: int
    seed: int


DirectResults = dict[tuple[str, str], tuple[int, int]]


def _initial_rows(pool: TournamentPool) -> dict[str, StandingRow]:
    rows: dict[str, StandingRow] = {}
    for entry in pool.entries.select_related("team").prefetch_related("adjustments"):
        adjustment = sum(item.points for item in entry.adjustments.all())
        rows[str(entry.team_id)] = {
            "team_id": str(entry.team_id),
            "team_name": entry.team.name,
            "short_name": entry.team.short_name,
            "played": 0,
            "won": 0,
            "drawn": 0,
            "lost": 0,
            "goals_for": 0,
            "goals_against": 0,
            "goal_difference": 0,
            "points": adjustment,
            "adjustment": adjustment,
            "position": 0,
            "seed": entry.seed_order,
        }
    return rows


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


def _apply_match(
    rows: dict[str, StandingRow],
    match: TournamentMatch,
    tournament: Tournament,
) -> None:
    home = rows.get(str(match.home_team_id))
    away = rows.get(str(match.away_team_id))
    if home is None or away is None:
        return
    home_score = int(match.home_score or 0)
    away_score = int(match.away_score or 0)
    home_points, away_points = _match_points(tournament, home_score, away_score)
    home["played"] += 1
    away["played"] += 1
    home["goals_for"] += home_score
    home["goals_against"] += away_score
    away["goals_for"] += away_score
    away["goals_against"] += home_score
    home["points"] += home_points
    away["points"] += away_points
    if home_score > away_score:
        home["won"] += 1
        away["lost"] += 1
    elif away_score > home_score:
        away["won"] += 1
        home["lost"] += 1
    else:
        home["drawn"] += 1
        away["drawn"] += 1


def _direct_results(
    matches: list[TournamentMatch], tournament: Tournament
) -> DirectResults:
    direct: DirectResults = {}
    for match in matches:
        home_id = str(match.home_team_id)
        away_id = str(match.away_team_id)
        home_score = int(match.home_score or 0)
        away_score = int(match.away_score or 0)
        home_points, away_points = _match_points(tournament, home_score, away_score)
        previous_home = direct.get((home_id, away_id), (0, 0))
        previous_away = direct.get((away_id, home_id), (0, 0))
        direct[home_id, away_id] = (
            previous_home[0] + home_points,
            previous_home[1] + home_score - away_score,
        )
        direct[away_id, home_id] = (
            previous_away[0] + away_points,
            previous_away[1] + away_score - home_score,
        )
    return direct


def _numeric_value(row: StandingRow, rule: str) -> int:
    values = {
        "points": row["points"],
        "goal_difference": row["goal_difference"],
        "goals_for": row["goals_for"],
    }
    return values.get(rule, 0)


def _comparison(
    rules: list[str], direct: DirectResults
) -> Callable[[StandingRow, StandingRow], int]:
    def compare(left: StandingRow, right: StandingRow) -> int:
        for rule in rules:
            if rule == "head_to_head":
                left_value = direct.get((left["team_id"], right["team_id"]), (0, 0))
                right_value = direct.get((right["team_id"], left["team_id"]), (0, 0))
            elif rule == "seed":
                left_value = (-left["seed"],)
                right_value = (-right["seed"],)
            elif rule == "name":
                left_name = left["team_name"].casefold()
                right_name = right["team_name"].casefold()
                if left_name != right_name:
                    return -1 if left_name < right_name else 1
                continue
            else:
                left_value = (_numeric_value(left, rule),)
                right_value = (_numeric_value(right, rule),)
            if left_value != right_value:
                return -1 if left_value > right_value else 1
        return 0

    return compare


def calculate_pool_standings(
    pool: TournamentPool, *, include_live_matches: bool = False
) -> list[StandingRow]:
    """Calculate ordered standings for one pool, optionally including live scores."""
    tournament: Tournament = pool.tournament
    rows = _initial_rows(pool)
    included_statuses = [TournamentMatch.Status.FINAL]
    if include_live_matches:
        included_statuses.append(TournamentMatch.Status.LIVE)
    included_matches = list(
        pool.matches.filter(
            status__in=included_statuses,
            home_team__isnull=False,
            away_team__isnull=False,
            home_score__isnull=False,
            away_score__isnull=False,
        )
    )
    included_matches = [
        match
        for match in included_matches
        if str(match.home_team_id) in rows and str(match.away_team_id) in rows
    ]
    for match in included_matches:
        _apply_match(rows, match, tournament)
    for row in rows.values():
        row["goal_difference"] = row["goals_for"] - row["goals_against"]

    allowed = {
        "points",
        "goal_difference",
        "goals_for",
        "head_to_head",
        "seed",
        "name",
    }
    rules = [rule for rule in tournament.tiebreakers if rule in allowed]
    if "name" not in rules:
        rules.append("name")
    ordered = sorted(
        rows.values(),
        key=cmp_to_key(
            _comparison(rules, _direct_results(included_matches, tournament))
        ),
    )
    for index, row in enumerate(ordered, start=1):
        row["position"] = index
    return ordered


def rank_rows_across_pools(
    tournament: Tournament,
    rows: list[StandingRow],
) -> list[StandingRow]:
    """Order same-position teams from separate pools without head-to-head data."""
    allowed = {"points", "goal_difference", "goals_for", "seed", "name"}
    rules = [rule for rule in tournament.tiebreakers if rule in allowed]
    if "name" not in rules:
        rules.append("name")
    return sorted(rows, key=cmp_to_key(_comparison(rules, {})))
