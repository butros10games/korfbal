"""Preview or apply explicitly reviewed imported-player/account pairs."""

from argparse import ArgumentParser
import json
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from apps.competition.services.player_linking import link_players
from apps.player.models import Player


class Command(BaseCommand):
    """Keep name matching outside the write path and default to a dry run."""

    def add_arguments(self, parser: ArgumentParser) -> None:
        """Require a private mapping file; writes require an explicit flag."""
        parser.add_argument("--links", type=Path, required=True)
        parser.add_argument("--apply", action="store_true")

    def handle(self, *args: object, **options: object) -> None:
        """Validate every pair before changing any profile.

        Raises:
            CommandError: The mapping is malformed or a pair needs review.
            ValueError: The mapping is not a list (wrapped as CommandError).

        """
        try:
            links = json.loads(Path(str(options["links"])).read_text(encoding="utf-8"))
            if not isinstance(links, list) or any(
                not isinstance(row, dict) for row in links
            ):
                raise ValueError(
                    "Links must be a list of explicit identity/account pairs"
                )
            result = link_players(links, apply=bool(options["apply"]))
        except (
            ValueError,
            TypeError,
            AttributeError,
            OSError,
            Player.DoesNotExist,
        ) as exc:
            raise CommandError(
                "Player linking failed; check the pairs and importer state"
            ) from exc
        self.stdout.write(json.dumps(result))
