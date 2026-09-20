"""Export non-personal score revisions for reproducible offline forecasting."""

from argparse import ArgumentParser
import json
from pathlib import Path
from typing import Any
from uuid import UUID

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from apps.competition.domain.score_forecast import timestamp
from apps.competition.domain.score_observations import snapshot
from apps.competition.queries.forecast_export import export_rows
from apps.competition.queries.forecast_legacy import predictions


class Command(BaseCommand):
    """Read-only export; never contacts Sportlink or mutates production records."""

    help = (
        "Export anonymous forecast data with observed result revisions and stable IDs."
    )

    def add_arguments(self, parser: ArgumentParser) -> None:
        """Require explicit season UUID and destination outside source control."""
        parser.add_argument("--season", required=True, type=UUID)
        parser.add_argument("--output", required=True, type=Path)
        parser.add_argument("--legacy-from", type=timestamp)
        parser.add_argument("--through", type=timestamp)

    def handle(self, *args: object, **options: object) -> None:
        """Write an export and report aggregate coverage.

        Raises:
            CommandError: No fixtures have supported metadata.

        """
        values: dict[str, Any] = dict(options)
        through = values.get("through") or timezone.now()
        report = export_rows(str(values["season"]), through)
        if not report["rows"]:
            raise CommandError("No supported league fixtures for this source season")
        scored = snapshot(report["rows"], through)
        start = values.get("legacy_from")
        identities = [
            row["match"]
            for row in scored
            if (start is None or timestamp(row["starts_at"]) >= start)
            and timestamp(row["duration_observed_at"]) <= timestamp(row["starts_at"])
        ]
        self.stdout.write(
            f"Legacy replay selected {len(identities)}/{len(report['rows'])} fixtures"
        )
        legacy = predictions(identities, through, self.stdout.write)
        for row in report["rows"]:
            if row["match"] in legacy:
                row["legacy_prediction"] = legacy[row["match"]]
        values["output"].write_text(json.dumps(report, sort_keys=True) + "\n")
        values["output"].chmod(0o600)
        self.stdout.write(
            f"Exported {len(report['rows'])} fixtures; exclusions: {report['excluded']}"
        )
