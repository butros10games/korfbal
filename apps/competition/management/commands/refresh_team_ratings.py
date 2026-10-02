"""Replay cross-season club-team Elo over the complete imported history."""

from argparse import ArgumentParser
import json

from django.core.management.base import BaseCommand

from apps.competition.services.team_elo import refresh_team_ratings


class Command(BaseCommand):
    """Rebuild ratings locally; never contacts Sportlink."""

    help = "Apply changed results to club-team Elo; --full replays all history."

    def add_arguments(self, parser: ArgumentParser) -> None:
        """Allow a full replay, for example after changing the model."""
        parser.add_argument("--full", action="store_true")

    def handle(self, *args: object, **options: object) -> None:
        """Report written row counts."""
        result = refresh_team_ratings(full=bool(options["full"]))
        self.stdout.write(json.dumps(result, sort_keys=True))
