"""Backfill the shared application models from saved competition snapshots."""

import json

from django.core.management.base import BaseCommand, CommandError

from apps.competition.composition import schedule_change_dispatcher
from apps.competition.services.publishing import publish_catalogue


class Command(BaseCommand):
    """Create native clubs, teams, seasonal teams, poules and fixtures idempotently."""

    help = "Publish saved KNKV data to native models without provider requests."

    def handle(self, *args: object, **options: object) -> None:
        """Print created identities and any unresolved mappings.

        Raises:
            CommandError: Another import owns the lease or a saved link conflicts.

        """
        try:
            result = publish_catalogue(schedule_changes=schedule_change_dispatcher())
        except ValueError as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(json.dumps(result, sort_keys=True))
