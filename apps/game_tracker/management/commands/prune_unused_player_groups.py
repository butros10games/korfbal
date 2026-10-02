"""Remove the empty lineups that were created for every imported match."""

from __future__ import annotations

from datetime import timedelta

from django.core.management.base import BaseCommand, CommandParser
from django.utils import timezone

from apps.game_tracker.services.player_groups import prune_unused_player_groups


class Command(BaseCommand):
    """Delete never-used player groups of matches that have started."""

    help = (
        "Delete player groups of started matches whose lineup was never used "
        "(no players, starting lineup or substitutions)."
    )

    def add_arguments(self, parser: CommandParser) -> None:
        """Register command arguments."""
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Count the lineups and groups that would be deleted.",
        )
        parser.add_argument("--batch-size", type=int, default=2000)

    def handle(self, *args: object, **options: object) -> None:
        """Prune lineups of matches that started over a day ago."""
        dry_run = bool(options["dry_run"])
        result = prune_unused_player_groups(
            started_before=timezone.now() - timedelta(days=1),
            batch_size=max(1, int(str(options["batch_size"]))),
            dry_run=dry_run,
        )
        verb = "Would delete" if dry_run else "Deleted"
        self.stdout.write(
            f"{verb} {result.groups} player groups of {result.matches} matches."
        )
