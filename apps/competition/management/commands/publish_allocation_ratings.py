"""Preview or activate allocation baselines for the existing live ratings API."""

from argparse import ArgumentParser
from datetime import datetime
import json
from pathlib import Path
from typing import Any

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from apps.competition.services.published_ratings import (
    configure_ratings,
    disable_ratings,
)
from apps.competition.services.rating_preview import PreviewParameters
from apps.schedule.models import Season


class Command(BaseCommand):
    """Activate only with --apply; deployment alone does not select source snapshots."""

    help = "Publish selected KNKV baselines to live ratings; defaults to dry-run."

    def add_arguments(self, parser: ArgumentParser) -> None:
        """Expose reviewed activation parameters and a reversible disable switch."""
        parser.add_argument("--season", required=True)
        parser.add_argument("--source", action="append", type=int)
        parser.add_argument("--effective-at", type=datetime.fromisoformat)
        parser.add_argument("--b-scale", type=float)
        parser.add_argument("--b-k-factor", type=float)
        parser.add_argument("--disable", action="store_true")
        parser.add_argument("--apply", action="store_true")
        parser.add_argument("--output", type=Path)

    @transaction.atomic
    def handle(self, *args: object, **options: object) -> None:
        """Preview valid inputs or apply exactly that configuration.

        Raises:
            CommandError: Input, sources or output destination are invalid.

        """
        values: dict[str, Any] = dict(options)
        try:
            season = Season.objects.get(name=values["season"])
            if values["disable"]:
                report = disable_ratings(season, apply=values["apply"])
            else:
                required = ("source", "effective_at", "b_scale", "b_k_factor")
                if any(values[key] is None for key in required):
                    raise CommandError(
                        "Require source, effective-at, b-scale and b-k-factor"
                    )
                report = configure_ratings(
                    season,
                    values["source"],
                    PreviewParameters(
                        values["effective_at"],
                        timezone.now(),
                        values["b_scale"],
                        values["b_k_factor"],
                    ),
                    apply=values["apply"],
                )
            output = json.dumps(report, indent=2, sort_keys=True)
            if values["output"]:
                values["output"].write_text(output + "\n")
            else:
                self.stdout.write(output)
        except (
            Season.DoesNotExist,
            Season.MultipleObjectsReturned,
            ValueError,
            OSError,
        ) as error:
            raise CommandError(str(error)) from error
