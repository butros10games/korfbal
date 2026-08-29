"""Framework-independent match-impact scoring policy."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from decimal import Decimal
import math
from typing import Any, Literal

from .win_probability import match_outcome_probabilities


LATEST_MATCH_IMPACT_ALGORITHM_VERSION = "v8"

# Historical tracker data does not contain an unbiased shot-type selection for
# open-play misses. Keep one deliberately conservative open-play baseline and
# only distinguish set pieces, whose type is consistently captured.
V7_OPEN_PLAY_XG = 0.18
V7_FREE_PASS_XG = 0.35
V7_PENALTY_XG = 0.75
# A possession won or lost is valued like one ordinary open-play opportunity.
# This keeps the combined score in expected-goal units and makes a fully
# tracked loss/interception pair zero-sum.
V8_POSSESSION_VALUE = V7_OPEN_PLAY_XG
V8_FAST_GOAL_SECONDS = 10
V8_FAST_GOAL_BONUS = V8_POSSESSION_VALUE * 0.5
V8_CLOSE_GAME_WINDOW_MINUTES = 5.0
V8_CLOSE_GAME_MULTIPLIERS = {0: 1.75, 1: 1.5, 2: 1.25}
MIN_SHOTS_FOR_EFFICIENCY_SCALING = 5
EFFICIENCY_RATE_VERY_GOOD = 0.5
EFFICIENCY_RATE_GOOD = 1.0 / 3.0
EFFICIENCY_RATE_FINE = 0.2

Side = Literal["home", "away"]
ImpactCategory = Literal[
    "offense_goal_above_expected",
    "offense_miss_below_expected",
    "defense_stop_above_expected",
    "defense_goal_below_expected",
    "possession_gain",
    "possession_loss",
]
ImpactSourceType = Literal["shot", "possession_change"]


@dataclass(frozen=True)
class MatchImpactContribution:
    """One auditable contribution from a tracked shot or possession change."""

    player_id: str
    time: str
    category: ImpactCategory
    points: float
    source_type: ImpactSourceType
    expected_goals: float | None = None
    scored: bool | None = None
    for_team: bool | None = None
    shot_type: str | None = None
    possession_kind: str | None = None
    base_points: float | None = None
    leverage_multiplier: float = 1.0
    transition_bonus: float = 0.0
    linked_goal_event_id: str | None = None
    source_event_id: str | None = None
    team_id: str | None = None
    win_expectancy_before: float | None = None
    win_expectancy_after: float | None = None
    win_probability_added: float = 0.0


def expected_goal_probability(shot_type: str | None) -> float:
    """Return the v7 expected-goal baseline for a tracked shot type."""
    normalised = normalise_goal_type(shot_type or "")
    if "straf" in normalised or "penalty" in normalised:
        return V7_PENALTY_XG
    if "vrije" in normalised or "free pass" in normalised:
        return V7_FREE_PASS_XG
    return V7_OPEN_PLAY_XG


def compute_v7_contributions(
    shots: Sequence[Mapping[str, Any]],
) -> list[MatchImpactContribution]:
    """Score shots as goals above expected for the responsible player.

    ``for_team=True`` identifies the attacking player. ``for_team=False``
    identifies the directly responsible defender while ``team_id`` remains the
    shooting team. This is the tracker write contract.
    """
    contributions: list[MatchImpactContribution] = []
    for shot in shots:
        player_id = str(shot.get("player_id") or "").strip()
        if not player_id:
            continue

        shot_type = str(shot.get("shot_type") or "")
        expected = expected_goal_probability(shot_type)
        scored = bool(shot.get("scored"))
        for_team = bool(shot.get("for_team", True))

        if for_team:
            points = (1.0 - expected) if scored else -expected
            category: ImpactCategory = (
                "offense_goal_above_expected"
                if scored
                else "offense_miss_below_expected"
            )
        else:
            points = -(1.0 - expected) if scored else expected
            category = (
                "defense_goal_below_expected"
                if scored
                else "defense_stop_above_expected"
            )

        contributions.append(
            MatchImpactContribution(
                player_id=player_id,
                time=str(shot.get("time") or "?"),
                category=category,
                points=points,
                source_type="shot",
                expected_goals=expected,
                scored=scored,
                for_team=for_team,
                shot_type=shot_type,
                base_points=points,
                source_event_id=(
                    str(shot.get("event_id") or shot.get("source_id") or "").strip()
                    or None
                ),
                team_id=str(shot.get("team_id") or "").strip() or None,
            )
        )
    return contributions


def _event_timestamp(event: Mapping[str, Any]) -> datetime | None:
    value = str(event.get("time_iso") or "").strip()
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _match_minute(value: object) -> float | None:
    label = str(value or "").strip()
    if not label:
        return None
    try:
        if "+" in label:
            regular, added = label.split("+", maxsplit=1)
            return float(regular) + float(added)
        return float(label)
    except ValueError:
        return None


def _event_elapsed_seconds(event: Mapping[str, Any]) -> float | None:
    raw_seconds = event.get("elapsed_seconds")
    if isinstance(raw_seconds, int | float) and math.isfinite(raw_seconds):
        return max(0.0, float(raw_seconds))
    minute = _match_minute(event.get("time"))
    return minute * 60.0 if minute is not None else None


def _side_for_team_id(
    team_id: str | None,
    *,
    home_team_id: str | None,
    away_team_id: str | None,
) -> Side | None:
    if team_id and home_team_id and team_id == home_team_id:
        return "home"
    if team_id and away_team_id and team_id == away_team_id:
        return "away"
    return None


def _opposite_side(side: Side) -> Side:
    return "away" if side == "home" else "home"


@dataclass(frozen=True)
class _ScoreState:
    home: int
    away: int


@dataclass(frozen=True)
class _PossessionTransition:
    before: Side
    after: Side


def _event_win_expectancy(
    *,
    score_before: _ScoreState,
    score_after: _ScoreState,
    seconds_remaining: float,
    possession: _PossessionTransition,
    perspective: Side,
) -> tuple[float, float, float]:
    before = match_outcome_probabilities(
        home_score=score_before.home,
        away_score=score_before.away,
        seconds_remaining=seconds_remaining,
        possession=possession.before,
    ).expectancy(perspective)
    after = match_outcome_probabilities(
        home_score=score_after.home,
        away_score=score_after.away,
        seconds_remaining=seconds_remaining,
        possession=possession.after,
    ).expectancy(perspective)
    return before, after, after - before


def _score_margin(score_by_team: Mapping[str, int]) -> int:
    scores = list(score_by_team.values())
    if not scores:
        return 0
    if len(scores) == 1:
        scores.append(0)
    return max(scores) - min(scores)


def _possession_leverage_multiplier(
    *,
    event: Mapping[str, Any],
    score_margin: int,
    match_duration_minutes: float,
) -> float:
    elapsed_minutes = _match_minute(event.get("time"))
    if elapsed_minutes is None or match_duration_minutes <= 0:
        return 1.0
    if elapsed_minutes < match_duration_minutes - V8_CLOSE_GAME_WINDOW_MINUTES:
        return 1.0
    return V8_CLOSE_GAME_MULTIPLIERS.get(score_margin, 1.0)


def _fast_goal_after_interception(
    *,
    events: Sequence[Mapping[str, Any]],
    event_index: int,
) -> Mapping[str, Any] | None:
    interception = events[event_index]
    interception_time = _event_timestamp(interception)
    interception_team_id = str(interception.get("team_id") or "").strip()
    match_part_id = str(interception.get("match_part_id") or "").strip()
    if interception_time is None or not interception_team_id:
        return None

    for later_event in events[event_index + 1 :]:
        later_time = _event_timestamp(later_event)
        if later_time is None:
            continue
        elapsed_seconds = (later_time - interception_time).total_seconds()
        if elapsed_seconds < 0:
            continue
        if elapsed_seconds > V8_FAST_GOAL_SECONDS:
            break
        later_part_id = str(later_event.get("match_part_id") or "").strip()
        if match_part_id and later_part_id and later_part_id != match_part_id:
            break

        later_type = later_event.get("type")
        later_team_id = str(later_event.get("team_id") or "").strip()
        if later_type == "goal":
            return later_event if later_team_id == interception_team_id else None
        if later_type != "possession_change":
            continue

        later_kind = str(later_event.get("kind") or "").strip()
        possession_changed_away = (
            later_team_id == interception_team_id and later_kind == "ball_loss"
        ) or (later_team_id != interception_team_id and later_kind == "interception")
        if possession_changed_away:
            break
    return None


@dataclass(frozen=True)
class _V8MatchContext:
    duration_minutes: float
    home_team_id: str | None
    away_team_id: str | None


@dataclass(frozen=True)
class _TimelineWpaContext:
    score_before_by_index: dict[int, _ScoreState]
    score_margin_by_index: dict[int, int]
    goal_expectancy_by_event: dict[str, tuple[float, float]]


def _event_id(event: Mapping[str, Any]) -> str | None:
    return str(event.get("event_id") or event.get("source_id") or "").strip() or None


def _build_timeline_wpa_context(
    events: Sequence[Mapping[str, Any]],
    match: _V8MatchContext,
) -> _TimelineWpaContext:
    team_ids = {
        str(event.get("team_id") or "").strip()
        for event in events
        if str(event.get("team_id") or "").strip()
    }
    score_by_team = dict.fromkeys(team_ids, 0)
    score = _ScoreState(home=0, away=0)
    score_before_by_index: dict[int, _ScoreState] = {}
    score_margin_by_index: dict[int, int] = {}
    goal_expectancy_by_event: dict[str, tuple[float, float]] = {}

    for event_index, event in enumerate(events):
        score_before_by_index[event_index] = score
        score_margin_by_index[event_index] = _score_margin(score_by_team)
        if event.get("type") != "goal":
            continue

        team_id = str(event.get("team_id") or "").strip()
        if team_id:
            score_by_team[team_id] = score_by_team.get(team_id, 0) + 1
        scoring_side = _side_for_team_id(
            team_id,
            home_team_id=match.home_team_id,
            away_team_id=match.away_team_id,
        )
        if scoring_side is None:
            continue

        score_after = _ScoreState(
            home=score.home + (1 if scoring_side == "home" else 0),
            away=score.away + (1 if scoring_side == "away" else 0),
        )
        elapsed_seconds = _event_elapsed_seconds(event)
        event_id = _event_id(event)
        if elapsed_seconds is not None and event_id is not None:
            before, after, _wpa = _event_win_expectancy(
                score_before=score,
                score_after=score_after,
                seconds_remaining=max(
                    0.0,
                    match.duration_minutes * 60.0 - elapsed_seconds,
                ),
                possession=_PossessionTransition(
                    before=scoring_side,
                    after=_opposite_side(scoring_side),
                ),
                perspective="home",
            )
            goal_expectancy_by_event[event_id] = (before, after)
        score = score_after

    return _TimelineWpaContext(
        score_before_by_index=score_before_by_index,
        score_margin_by_index=score_margin_by_index,
        goal_expectancy_by_event=goal_expectancy_by_event,
    )


def _possession_value(
    *,
    event: Mapping[str, Any],
    event_index: int,
    events: Sequence[Mapping[str, Any]],
) -> tuple[ImpactCategory, float, float, str | None] | None:
    kind = str(event.get("kind") or "").strip()
    if kind == "ball_loss":
        return "possession_loss", -V8_POSSESSION_VALUE, 0.0, None
    if kind != "interception":
        return None

    linked_goal = _fast_goal_after_interception(
        events=events,
        event_index=event_index,
    )
    if linked_goal is None:
        return "possession_gain", V8_POSSESSION_VALUE, 0.0, None
    return (
        "possession_gain",
        V8_POSSESSION_VALUE,
        V8_FAST_GOAL_BONUS,
        _event_id(linked_goal),
    )


def _possession_wpa(
    *,
    event: Mapping[str, Any],
    score: _ScoreState,
    player_side: Side | None,
    match: _V8MatchContext,
) -> tuple[float | None, float | None, float]:
    elapsed_seconds = _event_elapsed_seconds(event)
    if player_side is None or elapsed_seconds is None:
        return None, None, 0.0

    kind = str(event.get("kind") or "").strip()
    possession_before = (
        _opposite_side(player_side) if kind == "interception" else player_side
    )
    return _event_win_expectancy(
        score_before=score,
        score_after=score,
        seconds_remaining=max(
            0.0,
            match.duration_minutes * 60.0 - elapsed_seconds,
        ),
        possession=_PossessionTransition(
            before=possession_before,
            after=_opposite_side(possession_before),
        ),
        perspective=player_side,
    )


def _build_possession_contribution(
    *,
    event: Mapping[str, Any],
    event_index: int,
    events: Sequence[Mapping[str, Any]],
    timeline: _TimelineWpaContext,
    match: _V8MatchContext,
) -> MatchImpactContribution | None:
    player_id = str(event.get("player_id") or "").strip()
    value = _possession_value(event=event, event_index=event_index, events=events)
    if not player_id or value is None:
        return None

    category, base_points, transition_bonus, linked_goal_event_id = value
    team_id = str(event.get("team_id") or "").strip()
    player_side = _side_for_team_id(
        team_id,
        home_team_id=match.home_team_id,
        away_team_id=match.away_team_id,
    )
    leverage_multiplier = _possession_leverage_multiplier(
        event=event,
        score_margin=timeline.score_margin_by_index[event_index],
        match_duration_minutes=match.duration_minutes,
    )
    before, after, wpa = _possession_wpa(
        event=event,
        score=timeline.score_before_by_index[event_index],
        player_side=player_side,
        match=match,
    )
    return MatchImpactContribution(
        player_id=player_id,
        time=str(event.get("time") or "?"),
        category=category,
        points=(base_points + transition_bonus) * leverage_multiplier,
        source_type="possession_change",
        possession_kind=str(event.get("kind") or "").strip(),
        base_points=base_points,
        leverage_multiplier=leverage_multiplier,
        transition_bonus=transition_bonus,
        linked_goal_event_id=linked_goal_event_id,
        source_event_id=_event_id(event),
        team_id=team_id or None,
        win_expectancy_before=before,
        win_expectancy_after=after,
        win_probability_added=wpa,
    )


def _with_goal_wpa(
    contributions: Sequence[MatchImpactContribution],
    timeline: _TimelineWpaContext,
    match: _V8MatchContext,
) -> list[MatchImpactContribution]:
    result: list[MatchImpactContribution] = []
    for contribution in contributions:
        goal_expectancy = (
            timeline.goal_expectancy_by_event.get(contribution.source_event_id)
            if contribution.source_event_id
            else None
        )
        scoring_side = _side_for_team_id(
            contribution.team_id,
            home_team_id=match.home_team_id,
            away_team_id=match.away_team_id,
        )
        if not contribution.scored or goal_expectancy is None or scoring_side is None:
            result.append(contribution)
            continue

        player_side = (
            scoring_side if contribution.for_team else _opposite_side(scoring_side)
        )
        before_home, after_home = goal_expectancy
        before = before_home if player_side == "home" else 1.0 - before_home
        after = after_home if player_side == "home" else 1.0 - after_home
        result.append(
            replace(
                contribution,
                win_expectancy_before=before,
                win_expectancy_after=after,
                win_probability_added=after - before,
            )
        )
    return result


def compute_v8_contributions(
    shots: Sequence[Mapping[str, Any]],
    events: Sequence[Mapping[str, Any]],
    *,
    match_duration_minutes: float = 60.0,
    home_team_id: str | None = None,
    away_team_id: str | None = None,
) -> list[MatchImpactContribution]:
    """Combine xGVA and event-level Win Probability Added."""
    match = _V8MatchContext(
        duration_minutes=match_duration_minutes,
        home_team_id=home_team_id,
        away_team_id=away_team_id,
    )
    timeline = _build_timeline_wpa_context(events, match)
    contributions = compute_v7_contributions(shots)
    for event_index, event in enumerate(events):
        if event.get("type") != "possession_change":
            continue
        contribution = _build_possession_contribution(
            event=event,
            event_index=event_index,
            events=events,
            timeline=timeline,
            match=match,
        )
        if contribution is not None:
            contributions.append(contribution)
    return _with_goal_wpa(contributions, timeline, match)


def aggregate_v7_contributions(
    contributions: Sequence[MatchImpactContribution],
) -> dict[str, float]:
    """Aggregate versioned contributions by player without display rounding."""
    totals: dict[str, float] = {}
    for contribution in contributions:
        totals[contribution.player_id] = (
            totals.get(contribution.player_id, 0.0) + contribution.points
        )
    return totals


def aggregate_win_probability_added(
    contributions: Sequence[MatchImpactContribution],
) -> dict[str, float]:
    """Aggregate event-level WPA by responsible player."""
    totals: dict[str, float] = {}
    for contribution in contributions:
        totals[contribution.player_id] = (
            totals.get(contribution.player_id, 0.0) + contribution.win_probability_added
        )
    return totals


@dataclass(frozen=True)
class ShotImpactWeights:
    """Weights used for shot-related impact scoring."""

    miss_for_penalty: float
    shot_against_total: float
    goal_against_total: float
    miss_against_total: float


@dataclass(frozen=True)
class ShootingEfficiencyMultipliers:
    """Per-shooter multipliers derived from match shooting efficiency."""

    goal_points: float
    miss_penalty: float


def shot_impact_weights_for_version(version: str) -> ShotImpactWeights:
    """Return legacy shot weights, defaulting unknown legacy versions to v6."""
    if version == "v1":
        return ShotImpactWeights(0.9, -0.25, -6.2, 0.55)
    if version in {"v2", "v3", "v4", "v5"}:
        return ShotImpactWeights(0.6, -0.25, -6.2, 0.8)
    if version == "v6":
        return ShotImpactWeights(0.2, -0.17, -2.94, 0.31)
    return shot_impact_weights_for_version("v6")


def efficiency_multipliers_for_rate(
    *,
    goals: int,
    shots: int,
) -> ShootingEfficiencyMultipliers:
    """Return impact scaling for one player's shooting rate."""
    if shots < MIN_SHOTS_FOR_EFFICIENCY_SCALING:
        return ShootingEfficiencyMultipliers(1.0, 1.0)
    rate = (goals / shots) if shots else 0.0
    if rate >= EFFICIENCY_RATE_VERY_GOOD:
        return ShootingEfficiencyMultipliers(1.2, 0.7)
    if rate >= EFFICIENCY_RATE_GOOD:
        return ShootingEfficiencyMultipliers(1.1, 0.85)
    if rate >= EFFICIENCY_RATE_FINE:
        return ShootingEfficiencyMultipliers(1.0, 1.0)
    return ShootingEfficiencyMultipliers(0.9, 1.15)


def normalise_goal_type(value: str) -> str:
    """Normalize goal-type labels used by historical tracker data."""
    return " ".join((value or "").lower().split()).strip()


def goal_points(*, goal_type: str, streak: int) -> float:
    """Compute scorer impact for a goal type and team streak."""
    normalised = normalise_goal_type(goal_type)
    if "straf" in normalised:
        weight = 0.55
    elif "vrije" in normalised:
        weight = 0.65
    elif "korte" in normalised:
        weight = 1.35
    elif "doorloop" in normalised:
        weight = 1.25
    elif any(
        label in normalised
        for label in ("1/2 afstand", "halve afstand", "half afstand")
    ):
        weight = 1.1
    elif "afstand" in normalised:
        weight = 0.95
    else:
        weight = 1.0
    effective_streak = min(max(1, int(streak)), 4)
    streak_factor = 1 + (effective_streak - 1) * 0.12
    return 3.2 * weight * streak_factor


def next_streak_state(
    *,
    scoring_team_id: str | None,
    last_team_id: str | None,
    streak: int,
) -> tuple[str | None, int]:
    """Advance consecutive-team scoring state."""
    if scoring_team_id and scoring_team_id == last_team_id:
        return last_team_id, streak + 1
    return scoring_team_id, 1


def advance_score_state(
    *,
    home_score: int,
    away_score: int,
    scoring_team_id: str | None,
    home_team_id: str,
    away_team_id: str,
) -> tuple[int, int]:
    """Apply a goal to a home/away score pair."""
    if scoring_team_id == home_team_id:
        return home_score + 1, away_score
    if scoring_team_id == away_team_id:
        return home_score, away_score + 1
    return home_score, away_score


def opposing_side(
    *,
    team_id: str | None,
    home_team_id: str,
    away_team_id: str,
) -> Side | None:
    """Return the side opposing a participating team."""
    if team_id == home_team_id:
        return "away"
    if team_id == away_team_id:
        return "home"
    return None


def defending_side_for_shot(
    *,
    shot_team_id: str | None,
    home_team_id: str,
    away_team_id: str,
) -> Side | None:
    """Return the defending side for a shot."""
    return opposing_side(
        team_id=shot_team_id,
        home_team_id=home_team_id,
        away_team_id=away_team_id,
    )


def conceding_side_for_goal(
    *,
    scoring_team_id: str | None,
    home_team_id: str,
    away_team_id: str,
) -> Side | None:
    """Return the conceding side for a goal."""
    return opposing_side(
        team_id=scoring_team_id,
        home_team_id=home_team_id,
        away_team_id=away_team_id,
    )


def round_js_1dp(value: float) -> Decimal:
    """Round to one decimal using JavaScript ``Math.round`` semantics."""
    return Decimal(str(math.floor(value * 10.0 + 0.5) / 10.0))


def doorloop_concede_factor_for_version(version: str) -> float:
    """Return the per-defender doorloop concede penalty factor."""
    return 0.0 if version == "v6" else 0.06
