"""Preview the queued photo backlog the enrichment lane would serve."""

from argparse import ArgumentParser
import json

from django.core.management.base import BaseCommand
from django.utils import timezone

from apps.competition.services.catalog_metadata import preview_photos
from apps.competition.services.provider_scheduler import active_live_season


class Command(BaseCommand):
    """Read-only: no provider requests, credentials, lease or writes."""

    help = (
        "Classify queued player photos outside the live season: distinct eligible "
        "people (one request each), local settlements and skipped identities."
    )

    def add_arguments(self, parser: ArgumentParser) -> None:
        """Keep the report compact unless asked for indentation."""
        parser.add_argument("--indent", type=int, default=None)

    def handle(self, *args: object, **options: object) -> None:
        """Print one JSON report from a database snapshot."""
        report = preview_photos(active_live_season(), timezone.now())
        indent = options["indent"]
        self.stdout.write(
            json.dumps(
                report,
                sort_keys=True,
                indent=indent if isinstance(indent, int) else None,
            )
        )
