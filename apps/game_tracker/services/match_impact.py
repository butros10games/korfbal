"""Public facade for match impact scoring helpers."""

from .match_impact_persistence import (
    compute_match_impact_breakdown_cached,
    persist_match_impact_rows,
    persist_match_impact_rows_with_breakdowns,
)
from .match_impact_scorer import (
    LATEST_MATCH_IMPACT_ALGORITHM_VERSION,
    MatchImpactRow,
    MatchTeamImpactFeatures,
    PlayerImpactBreakdown,
    _compute_shooting_efficiency_multipliers,
    compute_match_impact_breakdown,
    compute_match_impact_rows,
    compute_match_team_impact_features,
    doorloop_concede_factor_for_version,
    round_js_1dp,
)
from .match_impact_timeline import (
    Interval,
    RoleIntervals,
    build_match_player_role_timeline,
    compute_match_end_minutes,
)


__all__ = [
    "LATEST_MATCH_IMPACT_ALGORITHM_VERSION",
    "Interval",
    "MatchImpactRow",
    "MatchTeamImpactFeatures",
    "PlayerImpactBreakdown",
    "RoleIntervals",
    "_compute_shooting_efficiency_multipliers",
    "build_match_player_role_timeline",
    "compute_match_end_minutes",
    "compute_match_impact_breakdown",
    "compute_match_impact_breakdown_cached",
    "compute_match_impact_rows",
    "compute_match_team_impact_features",
    "doorloop_concede_factor_for_version",
    "persist_match_impact_rows",
    "persist_match_impact_rows_with_breakdowns",
    "round_js_1dp",
]
