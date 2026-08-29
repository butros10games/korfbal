"""Compare legacy v6 and goals-above-expected v7 impact scores."""

from __future__ import annotations

from argparse import ArgumentParser
import json
from typing import cast

from django.core.management.base import BaseCommand, CommandError

from apps.game_tracker.models import MatchData
from apps.game_tracker.services.match_impact import (
    MatchImpactRow,
    compute_match_impact_rows,
)
from apps.player.models.player import Player


class Command(BaseCommand):
    """Produce a read-only player-by-player v6/v7 comparison."""

    help = "Compare legacy v6 impact with v7 goals above expected."

    def add_arguments(self, parser: ArgumentParser) -> None:
        """Register comparison filters and output format."""
        parser.add_argument(
            "--match-data-id",
            action="append",
            dest="match_data_ids",
            help="MatchData UUID; repeat for multiple matches",
        )
        parser.add_argument(
            "--limit",
            type=int,
            default=10,
            help="Maximum finished matches when no UUID is provided (default: 10)",
        )
        parser.add_argument(
            "--format",
            choices=("table", "json"),
            default="table",
            dest="output_format",
        )

    def handle(self, *args: object, **options: object) -> None:
        """Compute both versions without changing persisted scores.

        Raises:
            CommandError: If the limit is invalid or no matches are found.

        """
        limit = cast(int, options["limit"])
        if limit < 1:
            raise CommandError("--limit must be at least 1")

        queryset = MatchData.objects.select_related(
            "match_link__home_team", "match_link__away_team"
        ).order_by("-match_link__start_time")
        match_data_ids = cast(list[str] | None, options.get("match_data_ids")) or []
        if match_data_ids:
            queryset = queryset.filter(id_uuid__in=match_data_ids)
        else:
            queryset = queryset.filter(status="finished")[:limit]

        matches = list(queryset)
        if not matches:
            raise CommandError("No matching match data found")

        comparison: list[dict[str, object]] = []
        all_player_ids: set[str] = set()
        computed: list[
            tuple[MatchData, dict[str, MatchImpactRow], dict[str, MatchImpactRow]]
        ] = []
        for match_data in matches:
            old_rows = {
                row.player_id: row
                for row in compute_match_impact_rows(
                    match_data=match_data, algorithm_version="v6"
                )
            }
            new_rows = {
                row.player_id: row
                for row in compute_match_impact_rows(
                    match_data=match_data, algorithm_version="v7"
                )
            }
            all_player_ids.update(old_rows)
            all_player_ids.update(new_rows)
            computed.append((match_data, old_rows, new_rows))

        usernames = {
            str(player_id): username
            for player_id, username in Player.objects.filter(
                id_uuid__in=all_player_ids
            ).values_list("id_uuid", "user__username")
        }

        for match_data, old_rows_raw, new_rows_raw in computed:
            old_rows = old_rows_raw
            new_rows = new_rows_raw
            player_ids = sorted(set(old_rows) | set(new_rows))
            old_rank = self._ranks(old_rows)
            new_rank = self._ranks(new_rows)
            match = match_data.match_link
            label = (
                f"{match.home_team.name} - {match.away_team.name}"
                if match
                else str(match_data.id_uuid)
            )
            for player_id in player_ids:
                old_score = float(old_rows[player_id].impact_score)
                new_score = float(new_rows[player_id].impact_score)
                comparison.append({
                    "match_data_id": str(match_data.id_uuid),
                    "match": label,
                    "player_id": player_id,
                    "player": usernames.get(player_id, player_id),
                    "v6": old_score,
                    "v7_gax": new_score,
                    "delta": round(new_score - old_score, 3),
                    "v6_rank": old_rank[player_id],
                    "v7_rank": new_rank[player_id],
                    "rank_change": old_rank[player_id] - new_rank[player_id],
                })

        if options["output_format"] == "json":
            self.stdout.write(json.dumps(comparison, indent=2, sort_keys=True))
            return

        self.stdout.write("Match | Player | v6 | v7 GAX | Delta | Rank v6→v7")
        self.stdout.write("-" * 88)
        for row in comparison:
            self.stdout.write(
                f"{row['match']} | {row['player']} | {row['v6']:+.1f} | "
                f"{row['v7_gax']:+.3f} | {row['delta']:+.3f} | "
                f"{row['v6_rank']}→{row['v7_rank']}"
            )
        self.stdout.write(
            self.style.SUCCESS(
                f"Compared {len(comparison)} players across {len(matches)} matches."
            )
        )

    @staticmethod
    def _ranks(rows: dict[str, MatchImpactRow]) -> dict[str, int]:
        ordered = sorted(
            rows,
            key=lambda player_id: (
                -float(rows[player_id].impact_score),
                player_id,
            ),
        )
        return {player_id: rank for rank, player_id in enumerate(ordered, start=1)}
