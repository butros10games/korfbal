"""Connect source cup fixtures to an explicitly selected owned tournament."""

from django.core.management.base import BaseCommand, CommandError, CommandParser

from apps.competition.services.cup_publication import publish_cup
from apps.tournament.composition import touch_tournament
from apps.tournament.models import Tournament
from apps.tournament.services.cups import CupError


class Command(BaseCommand):
    """Publish without creating an implicit owner or fabricated bracket."""

    help = "Adopt a source cup into an empty tournament with confirmed cup rules."

    def add_arguments(self, parser: CommandParser) -> None:
        """Require both source and owned destination identifiers."""
        parser.add_argument("--cup-id", required=True, type=int)
        parser.add_argument("--tournament-id", required=True)

    def handle(self, *args: object, **options: object) -> None:
        """Create operational fixtures with links back to official source results.

        Raises:
            CommandError: If the destination cannot safely receive the cup.

        """
        try:
            count = publish_cup(
                int(str(options["cup_id"])), str(options["tournament_id"])
            )
        except CupError as exc:
            raise CommandError(str(exc)) from exc
        if count:
            touch_tournament(Tournament.objects.get(pk=options["tournament_id"]))
        self.stdout.write(
            f"Published {count} cup fixtures; "
            "assign unknown rounds before starting them."
        )
