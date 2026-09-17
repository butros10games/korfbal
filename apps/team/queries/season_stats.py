"""Complete season results and explicitly team-oriented registered shot totals."""

from typing import Any

from django.db.models import Count, F, Q, QuerySet

from apps.game_tracker.models import MatchData, Shot
from apps.team.models import Team


def team_season_statistics(team: Team, matches: QuerySet[MatchData]) -> dict[str, Any]:
    """Keep final scores separate from potentially incomplete event registration."""
    finished = matches.filter(status="finished").order_by(
        "match_link__start_time", "match_link_id"
    )
    general: dict[str, Any] = {
        "shots_for": 0,
        "shots_against": 0,
        "goals_for": 0,
        "goals_against": 0,
        "team_goal_stats": {},
        "goal_types": [],
    }
    tracked = set()
    # Shot.team is the scoring/shooting team. for_team describes the recorder's
    # perspective and can be reversed when viewing the other team's season.
    rows = (
        Shot.objects
        .filter(match_data__in=finished)
        .filter(
            Q(team_id=F("match_data__match_link__home_team_id"))
            | Q(team_id=F("match_data__match_link__away_team_id"))
        )
        .values("match_data_id", "team_id", "shot_type__name")
        .annotate(shots=Count("pk"), goals=Count("pk", filter=Q(scored=True)))
    )
    for row in rows:
        tracked.add(row["match_data_id"])
        own_team = row["team_id"] == team.pk
        side = "for" if own_team else "against"
        general[f"shots_{side}"] += row["shots"]
        general[f"goals_{side}"] += row["goals"]
        if row["goals"]:
            goal_type = row["shot_type__name"] or "Onbekend"
            breakdown = general["team_goal_stats"].setdefault(
                goal_type, {"goals_by_player": 0, "goals_against_player": 0}
            )
            breakdown["goals_by_player" if own_team else "goals_against_player"] += row[
                "goals"
            ]

    results = []
    for entry in finished:
        match = entry.match_link
        is_home = match.home_team_id == team.pk
        opponent = match.away_team if is_home else match.home_team
        results.append({
            "match_id": str(match.pk),
            "start_time": match.start_time.isoformat(),
            "opponent": f"{opponent.club.name} {opponent.name}",
            "is_home": is_home,
            "goals_for": entry.home_score if is_home else entry.away_score,
            "goals_against": entry.away_score if is_home else entry.home_score,
            "has_shots": entry.pk in tracked,
        })

    return {
        "general": general if tracked else None,
        "season": {"matches": results},
    }
