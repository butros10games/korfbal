"""Export experimental allocation-based ratings without database mutations."""

from argparse import ArgumentParser
from datetime import datetime
import json
from pathlib import Path
from typing import Any

from django.core.management.base import BaseCommand, CommandError

from apps.competition.services.rating_preview import PreviewParameters, preview_ratings
from apps.schedule.models import Season


class Command(BaseCommand):
    """Require explicit source provenance, effective window and uncalibrated scale."""

    help = "Read-only KNKV-seeded Elo experiment; exports a JSON report."

    def add_arguments(self, parser: ArgumentParser) -> None:
        """Do not silently select a new snapshot or reinterpret publication dates."""
        parser.add_argument("--season", required=True)
        parser.add_argument("--source", required=True, action="append", type=int)
        parser.add_argument(
            "--effective-at", required=True, type=datetime.fromisoformat
        )
        parser.add_argument("--through", required=True, type=datetime.fromisoformat)
        parser.add_argument("--b-scale", required=True, type=float)
        parser.add_argument("--b-k-factor", required=True, type=float)
        parser.add_argument("--output", required=True, type=Path)

    def handle(self, *args: object, **options: object) -> None:
        """Validate input and export a reproducible report.

        Raises:
            CommandError: The season, sources, dates or rating parameters are invalid.

        """
        values: dict[str, Any] = dict(options)
        try:
            season = Season.objects.get(name=values["season"])
            report = preview_ratings(
                season,
                values["source"],
                PreviewParameters(
                    effective_at=values["effective_at"],
                    through=values["through"],
                    scale=values["b_scale"],
                    k_factor=values["b_k_factor"],
                ),
            )
            values["output"].write_text(
                json.dumps(report, indent=2, sort_keys=True) + "\n"
            )
        except (
            Season.DoesNotExist,
            Season.MultipleObjectsReturned,
            ValueError,
            OSError,
        ) as error:
            raise CommandError(str(error)) from error
        self.stdout.write(
            f"Preview: {len(report['results'])} allocations, "
            f"{report['used_results']} results; no ratings applied"
        )
