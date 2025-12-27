"""Audit and compare player impact scores.

This command is meant as a research/diagnostics utility to understand how
"impact" differs across:

- Stored match-impact rows (authoritative, computed from match timelines)
- The lightweight Team-page heuristic (computed from aggregated shot/goal stats)

Usage examples:
    uv run python manage.py audit_impact_scores --match-data <uuid>
    uv run python manage.py audit_impact_scores --team <uuid> --season <uuid>

Notes:
    We intentionally keep the math identical to the frontend heuristic used in
    `apps/node_projects/frontend/korfbal-web/src/pages/Team/TeamPage.tsx`.

"""

from __future__ import annotations

from dataclasses import dataclass
import math
import operator

from django.core.management.base import BaseCommand, CommandParser
from django.db import models

from apps.game_tracker.models import MatchData, PlayerMatchImpact, Shot
from apps.game_tracker.services.match_impact import (
    LATEST_MATCH_IMPACT_ALGORITHM_VERSION,
    compute_match_impact_rows,
)
from apps.team.models import Team


MIN_SPEARMAN_SAMPLES = 2
STORED_RECOMPUTED_MISMATCH_TOLERANCE = 0.05


def _safe_ratio(numerator: float, denominator: float) -> float:
    if denominator <= 0:
        return 0.0
    return numerator / denominator


def team_page_heuristic_impact(*, gf: int, ga: int, sf: int, sa: int) -> float:
    """Mirror the Team page heuristic impact formula.

    Frontend source:
        `TeamPage.tsx` -> `computeImpactFromAggregates`.

    """
    acc_for = _safe_ratio(gf, sf)
    acc_against = _safe_ratio(ga, sa)
    efficiency_delta = acc_for - acc_against

    raw = gf * 8 - ga * 6 + (sf - sa) * 1.25 + efficiency_delta * 10
    return round(float(raw), 1)


def _rank(values: list[float]) -> list[float]:
    """Compute average ranks (1..n), handling ties.

    Returns ranks aligned to original indices.
    """
    indexed = list(enumerate(values))
    indexed.sort(key=operator.itemgetter(1))

    ranks = [0.0] * len(values)
    i = 0
    n = len(values)
    while i < n:
        j = i
        while j + 1 < n and indexed[j + 1][1] == indexed[i][1]:
            j += 1

        # Average rank for ties.
        # Using 1-based rank positions.
        avg_rank = (i + 1 + j + 1) / 2.0
        for k in range(i, j + 1):
            original_index = indexed[k][0]
            ranks[original_index] = avg_rank

        i = j + 1

    return ranks


def spearman_rho(xs: list[float], ys: list[float]) -> float | None:
    """Compute Spearman's rank correlation coefficient.

    Returns:
        float | None: Correlation in [-1, 1], or None if not enough data.

    Raises:
        ValueError: If `xs` and `ys` have different lengths.

    """
    if len(xs) != len(ys):
        raise ValueError("xs and ys must have same length")
    if len(xs) < MIN_SPEARMAN_SAMPLES:
        return None

    rx = _rank(xs)
    ry = _rank(ys)

    mx = sum(rx) / len(rx)
    my = sum(ry) / len(ry)

    cov = sum((a - mx) * (b - my) for a, b in zip(rx, ry, strict=True))
    vx = sum((a - mx) ** 2 for a in rx)
    vy = sum((b - my) ** 2 for b in ry)
    if vx <= 0 or vy <= 0:
        return None

    return cov / math.sqrt(vx * vy)


@dataclass(frozen=True)
class PlayerAggregate:
    """Aggregated per-player stats and impact totals for a team/season."""

    username: str
    gf: int
    ga: int
    sf: int
    sa: int
    heuristic: float
    stored: float | None
    stored_matches: int


def _finished_matches_for_team(*, team: Team, season_id: str | None) -> list[MatchData]:
    qs = (
        MatchData.objects
        .select_related("match_link", "match_link__season")
        .filter(status="finished")
        .filter(
            models.Q(match_link__home_team_id=team.id_uuid)
            | models.Q(match_link__away_team_id=team.id_uuid)
        )
    )
    if season_id:
        qs = qs.filter(match_link__season_id=season_id)
    return list(qs)


def _build_team_aggregates(
    *,
    team: Team,
    matches: list[MatchData],
) -> list[PlayerAggregate]:
    """Compute per-player aggregates for a team over a set of matches."""
    shot_rows = list(
        Shot.objects
        .filter(match_data__in=matches)
        .values("player__user__username")
        .annotate(
            sf=models.Count("id_uuid", filter=models.Q(for_team=True)),
            sa=models.Count("id_uuid", filter=models.Q(for_team=False)),
            gf=models.Count("id_uuid", filter=models.Q(for_team=True, scored=True)),
            ga=models.Count(
                "id_uuid",
                filter=models.Q(for_team=False, scored=True),
            ),
        )
    )

    impact_rows = list(
        PlayerMatchImpact.objects
        .filter(
            match_data__in=matches,
            team=team,
            algorithm_version=LATEST_MATCH_IMPACT_ALGORITHM_VERSION,
        )
        .values("player__user__username")
        .annotate(
            total=models.Sum("impact_score"),
            match_count=models.Count("match_data", distinct=True),
        )
    )

    stored_by_username: dict[str, tuple[float, int]] = {}
    for row in impact_rows:
        username = str(row.get("player__user__username") or "").strip()
        total = row.get("total")
        match_count = int(row.get("match_count") or 0)
        if not username or total is None:
            continue
        stored_by_username[username] = (round(float(total), 1), match_count)

    aggregates: list[PlayerAggregate] = []
    for row in shot_rows:
        username = str(row.get("player__user__username") or "").strip()
        if not username:
            continue

        gf = int(row.get("gf") or 0)
        ga = int(row.get("ga") or 0)
        sf = int(row.get("sf") or 0)
        sa = int(row.get("sa") or 0)

        heuristic = team_page_heuristic_impact(gf=gf, ga=ga, sf=sf, sa=sa)

        stored_tuple = stored_by_username.get(username)
        stored_total = stored_tuple[0] if stored_tuple else None
        stored_matches = stored_tuple[1] if stored_tuple else 0

        aggregates.append(
            PlayerAggregate(
                username=username,
                gf=gf,
                ga=ga,
                sf=sf,
                sa=sa,
                heuristic=heuristic,
                stored=stored_total,
                stored_matches=stored_matches,
            )
        )

    return aggregates


class Command(BaseCommand):
    """Django management command to audit impact score differences."""

    help = "Audit stored match-impact vs Team-page heuristic impact."

    def add_arguments(self, parser: CommandParser) -> None:
        """Register CLI arguments."""
        parser.add_argument(
            "--match-data",
            dest="match_data_id",
            type=str,
            help="MatchData id_uuid to audit (per-match output).",
        )
        parser.add_argument(
            "--team",
            dest="team_id",
            type=str,
            help="Team id_uuid to audit (season/team aggregation output).",
        )
        parser.add_argument(
            "--season",
            dest="season_id",
            type=str,
            default=None,
            help="Season id_uuid (optional; only used with --team).",
        )
        parser.add_argument(
            "--limit",
            dest="limit",
            type=int,
            default=30,
            help="Max number of players to show in delta lists.",
        )

    def handle(self, *args: object, **options: object) -> None:
        """Run the audit based on provided arguments."""
        match_data_id = str(options.get("match_data_id") or "").strip()
        team_id = str(options.get("team_id") or "").strip()
        season_id = str(options.get("season_id") or "").strip()

        raw_limit = options.get("limit")
        limit = raw_limit if isinstance(raw_limit, int) else 30

        if match_data_id:
            self._audit_match(match_data_id)
            return

        if team_id:
            self._audit_team(
                team_id=team_id,
                season_id=season_id or None,
                limit=limit,
            )
            return

        self.stdout.write(
            "Provide either --match-data <uuid> or --team <uuid> [--season <uuid>]."
        )

    def _audit_match(self, match_data_id: str) -> None:
        match_data = (
            MatchData.objects
            .filter(id_uuid=match_data_id)
            .select_related("match_link")
            .first()
        )
        if not match_data:
            self.stdout.write(f"No MatchData found for {match_data_id}")
            return

        self.stdout.write(
            "MatchData "
            f"{match_data.id_uuid} status={match_data.status} "
            f"algo={LATEST_MATCH_IMPACT_ALGORITHM_VERSION}"
        )

        computed_rows = compute_match_impact_rows(
            match_data=match_data,
            algorithm_version=LATEST_MATCH_IMPACT_ALGORITHM_VERSION,
        )
        computed_by_player: dict[str, float] = {
            r.player_id: float(r.impact_score) for r in computed_rows
        }

        stored_qs = PlayerMatchImpact.objects.filter(
            match_data=match_data,
            algorithm_version=LATEST_MATCH_IMPACT_ALGORITHM_VERSION,
        ).select_related("player__user")

        stored_by_player: dict[str, float] = {
            str(row.player_id): float(row.impact_score) for row in stored_qs
        }

        shot_rows = list(
            Shot.objects
            .filter(match_data=match_data)
            .values("player__id_uuid", "player__user__username")
            .annotate(
                sf=models.Count("id_uuid", filter=models.Q(for_team=True)),
                sa=models.Count("id_uuid", filter=models.Q(for_team=False)),
                gf=models.Count("id_uuid", filter=models.Q(for_team=True, scored=True)),
                ga=models.Count(
                    "id_uuid",
                    filter=models.Q(for_team=False, scored=True),
                ),
            )
        )

        stats_by_player: dict[str, tuple[str, int, int, int, int]] = {}
        for row in shot_rows:
            pid = str(row.get("player__id_uuid") or "").strip()
            username = str(row.get("player__user__username") or "").strip()
            if not pid or not username:
                continue
            stats_by_player[pid] = (
                username,
                int(row.get("gf") or 0),
                int(row.get("ga") or 0),
                int(row.get("sf") or 0),
                int(row.get("sa") or 0),
            )

        all_player_ids = sorted(
            set(computed_by_player.keys())
            | set(stored_by_player.keys())
            | set(stats_by_player.keys())
        )

        if not all_player_ids:
            self.stdout.write("No players found (no impacts, no shots).")
            return

        self.stdout.write(
            "\nPer-player (stored vs recomputed vs heuristic-from-shot-aggregates):"
        )

        mismatches: list[tuple[str, float]] = []
        missing_stored = 0
        for pid in all_player_ids:
            computed = computed_by_player.get(pid)
            stored = stored_by_player.get(pid)
            if stored is None:
                missing_stored += 1

            username, gf, ga, sf, sa = stats_by_player.get(pid, (pid[:8], 0, 0, 0, 0))
            heuristic = team_page_heuristic_impact(gf=gf, ga=ga, sf=sf, sa=sa)

            delta = None
            if stored is not None and computed is not None:
                delta = round(float(stored - computed), 3)
                if abs(delta) >= STORED_RECOMPUTED_MISMATCH_TOLERANCE:
                    mismatches.append((username, delta))

            self.stdout.write(
                f"- {username:24s} stored={stored if stored is not None else '—':>6} "
                f"recomputed={computed if computed is not None else '—':>6} "
                f"Δ(stored-recomputed)={delta if delta is not None else '—':>6} "
                f"heur={heuristic:>5.1f} GF/GA={gf}/{ga} SF/SA={sf}/{sa}"
            )

        self.stdout.write(
            f"\nStored rows missing for {missing_stored}/{len(all_player_ids)} players."
        )
        if mismatches:
            mismatches.sort(key=lambda x: abs(x[1]), reverse=True)
            worst = mismatches[0]
            self.stdout.write(
                "WARNING: "
                f"{len(mismatches)} players differ between stored and recomputed by >= "
                f"{STORED_RECOMPUTED_MISMATCH_TOLERANCE}. Worst: "
                f"{worst[0]} Δ={worst[1]:+.3f}"
            )
        else:
            self.stdout.write(
                "Stored and recomputed impacts match "
                f"(within {STORED_RECOMPUTED_MISMATCH_TOLERANCE}) "
                "for all players with data."
            )

    def _audit_team(self, *, team_id: str, season_id: str | None, limit: int) -> None:
        team = Team.objects.filter(id_uuid=team_id).first()
        if not team:
            self.stdout.write(f"No Team found for {team_id}")
            return

        matches = _finished_matches_for_team(team=team, season_id=season_id)
        if not matches:
            self.stdout.write("No finished matches found for the given team/season.")
            return

        self.stdout.write(
            "Team "
            f"{team.name} ({team.id_uuid}) matches={len(matches)} "
            f"algo={LATEST_MATCH_IMPACT_ALGORITHM_VERSION}"
        )

        aggregates = _build_team_aggregates(team=team, matches=matches)

        # Correlation (only players with stored totals).
        with_stored = [a for a in aggregates if a.stored is not None]
        stored_values: list[float] = [
            float(a.stored) for a in with_stored if a.stored is not None
        ]
        heuristic_values: list[float] = [a.heuristic for a in with_stored]
        rho = spearman_rho(
            stored_values,
            heuristic_values,
        )

        if rho is None:
            self.stdout.write("Not enough stored rows to compute correlation.")
        else:
            self.stdout.write(f"Spearman correlation stored vs heuristic: {rho:.3f}")

        # Largest absolute deltas.
        deltas: list[tuple[str, float, float, float, int, int, int, int, int]] = []
        for a in aggregates:
            if a.stored is None:
                continue
            stored_total = a.stored
            deltas.append((
                a.username,
                stored_total - a.heuristic,
                stored_total,
                a.heuristic,
                a.gf,
                a.ga,
                a.sf,
                a.sa,
                a.stored_matches,
            ))

        deltas.sort(key=lambda x: abs(x[1]), reverse=True)

        self.stdout.write("\nTop deltas (stored - heuristic):")
        for (
            username,
            delta,
            stored,
            heuristic,
            gf,
            ga,
            sf,
            sa,
            stored_matches,
        ) in deltas[: max(0, limit)]:
            self.stdout.write(
                f"- {username:24s} Δ={delta:+6.1f} "
                f"stored={stored:6.1f} heur={heuristic:6.1f} "
                f"GF/GA={gf}/{ga} SF/SA={sf}/{sa} "
                f"stored_matches={stored_matches}"
            )

        # Show top performers by stored impact.
        by_stored = sorted(
            with_stored,
            key=lambda a: a.stored,
            reverse=True,
        )
        self.stdout.write("\nTop players by stored impact:")
        for a in by_stored[: max(0, limit)]:
            stored_total = a.stored
            self.stdout.write(
                f"- {a.username:24s} stored={stored_total:6.1f} "
                f"heur={a.heuristic:6.1f} GF/GA={a.gf}/{a.ga} "
                f"SF/SA={a.sf}/{a.sa} stored_matches={a.stored_matches}"
            )
