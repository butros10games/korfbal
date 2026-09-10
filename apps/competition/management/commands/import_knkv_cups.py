"""Index existing KNKV cup fixtures without making provider requests."""

from django.core.management.base import BaseCommand, CommandParser

from apps.competition.models import CupCompetition, Match
from apps.competition.services.cups import OBSERVED_CUPS, import_cup_fixture


class Command(BaseCommand):
    """Create source cup identities for previously imported fixtures."""

    help = "Index KNKV cups from existing source fixtures; no network requests."

    def add_arguments(self, parser: CommandParser) -> None:
        """Require an explicit imported season scope."""
        parser.add_argument("--season-id", required=True)

    def handle(self, *args: object, **options: object) -> None:
        """Retain unknown rounds and keep official results on native source matches."""
        count = 0
        for match in (
            Match.objects
            .filter(season_id=options["season_id"], pool__class_name__in=OBSERVED_CUPS)
            .select_related("pool", "season")
            .iterator(chunk_size=500)
        ):
            import_cup_fixture(match)
            count += 1
        self.stdout.write(f"Indexed {count} KNKV cup fixtures.")
        for cup in CupCompetition.objects.filter(
            season_id=options["season_id"]
        ).order_by("name", "sport"):
            self.stdout.write(f"Cup {cup.pk}: {cup.name} ({cup.sport})")
