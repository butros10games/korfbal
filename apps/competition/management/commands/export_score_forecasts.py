"""Export non-personal score revisions for reproducible offline forecasting."""

from argparse import ArgumentParser
import json
from pathlib import Path
from typing import Any
from uuid import UUID

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from apps.competition.models import Match
from apps.competition.queries.forecast_export import export_rows
from apps.competition.services.match_prediction import rating_prediction


class Command(BaseCommand):
    """Read-only export; never contacts Sportlink or mutates production records."""

    help = (
        "Export anonymous forecast data with observed result revisions and stable IDs."
    )

    def add_arguments(self, parser: ArgumentParser) -> None:
        """Require explicit season UUID and destination outside source control."""
        parser.add_argument("--season", required=True, type=UUID)
        parser.add_argument("--output", required=True, type=Path)

    def handle(self, *args: object, **options: object) -> None:
        """Write an export and report aggregate coverage.

        Raises:
            CommandError: No fixtures have supported metadata.

        """
        values: dict[str, Any] = dict(options)
        report = export_rows(str(values["season"]), timezone.now())
        if not report["rows"]:
            raise CommandError("No supported league fixtures for this source season")
        sources = Match.objects.filter(
            pk__in=[row["match"] for row in report["rows"]]
        ).select_related("local_match__season")
        legacy = {
            str(source.pk): rating_prediction(source.local_match)
            if source.local_match
            else {"status": "unavailable", "reason": "not_published"}
            for source in sources.iterator(chunk_size=100)
        }
        for row in report["rows"]:
            row["legacy_prediction"] = legacy[row["match"]]
        values["output"].write_text(json.dumps(report, sort_keys=True) + "\n")
        values["output"].chmod(0o600)
        self.stdout.write(
            f"Exported {len(report['rows'])} fixtures; exclusions: {report['excluded']}"
        )
