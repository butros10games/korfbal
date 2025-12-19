"""Recompute and persist impact scores for one or more matches."""

from __future__ import annotations

from argparse import ArgumentParser

from django.core.management.base import BaseCommand

from apps.game_tracker.models import MatchData
from apps.game_tracker.services.match_impact import (
    persist_match_impact_rows,
    persist_match_impact_rows_with_breakdowns,
)


class Command(BaseCommand):
    """Django management command to recompute persisted match impact rows."""

    help = "Recompute PlayerMatchImpact rows for a match (or all finished matches)."

    def add_arguments(self, parser: ArgumentParser) -> None:
        """Register CLI arguments for this command."""
        parser.add_argument(
            "--match-data-id", dest="match_data_id", help="MatchData UUID"
        )
        parser.add_argument(
            "--finished",
            action="store_true",
            help="Recompute all finished matches",
        )
        parser.add_argument(
            "--skip-breakdowns",
            action="store_true",
            help=(
                "Only compute PlayerMatchImpact rows. If omitted, also persists "
                "PlayerMatchImpactBreakdown rows for fast Team-page breakdowns."
            ),
        )

    def handle(self, *args: object, **options: object) -> None:
        """Execute the recomputation for the requested match set."""
        match_data_id = options.get("match_data_id")
        finished = bool(options.get("finished"))
        skip_breakdowns = bool(options.get("skip_breakdowns"))

        if not match_data_id and not finished:
            self.stderr.write("Provide --match-data-id or --finished")
            return

        qs = MatchData.objects.select_related("match_link")
        if match_data_id:
            qs = qs.filter(id_uuid=match_data_id)
        if finished:
            qs = qs.filter(status="finished")

        total = 0
        for md in qs.iterator():
            if skip_breakdowns:
                rows = persist_match_impact_rows(match_data=md)
            else:
                rows = persist_match_impact_rows_with_breakdowns(match_data=md)
            total += rows
            self.stdout.write(f"{md.id_uuid}: {rows} rows")

        self.stdout.write(self.style.SUCCESS(f"Done. Upserted {total} rows."))
