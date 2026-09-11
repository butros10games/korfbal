"""Bind the private worker session to explicitly selected native team identities."""

from typing import Any, cast

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError, CommandParser
from django.db import transaction

from apps.competition.models import MatchFormAccess
from apps.team.models import Team


class Command(BaseCommand):
    """Configure the account, team bindings and optional automatic uploads."""

    help = (
        "Bind the configured Sportlink session to one account and explicit team UUIDs."
    )

    def add_arguments(self, parser: CommandParser) -> None:
        """Require stable identities instead of matching ambiguous team names."""
        parser.add_argument("--username", required=True)
        parser.add_argument("--team", action="append", required=True)
        parser.add_argument("--auto-substitutions", action="append", default=[])

    @transaction.atomic
    def handle(self, *args: object, **options: object) -> None:
        """Bind without reading or displaying secrets or sending provider writes.

        Raises:
            CommandError: Missing session, conflicting binding,
                or invalid account/team choices.

        """
        options = cast(dict[str, Any], options)
        if not settings.SPORTLINK_SYNC_SESSION_FILE:
            raise CommandError(
                "Configure SPORTLINK_SYNC_SESSION_FILE on the worker first."
            )
        user = (
            get_user_model()
            .objects.filter(username=options["username"], is_active=True)
            .first()
        )
        if user is None:
            raise CommandError("Active local account not found.")
        if MatchFormAccess.objects.exclude(user=user).exists():
            raise CommandError(
                "The shared Sportlink session is already bound to another account."
            )
        requested = set(options["team"])
        automatic = set(options["auto_substitutions"])
        if not automatic <= requested:
            raise CommandError(
                "Automatic substitutions must name one of the selected teams."
            )
        teams = list(Team.objects.filter(pk__in=requested))
        if len(teams) != len(requested):
            raise CommandError("A selected team does not exist.")
        for team in teams:
            MatchFormAccess.objects.update_or_create(
                team=team,
                defaults={
                    "user": user,
                    "enabled": True,
                    "auto_substitutions": str(team.pk) in automatic,
                },
            )
        self.stdout.write(
            self.style.SUCCESS(
                f"Connected {len(teams)} teams; "
                f"automatic substitutions: {len(automatic)} (A-category only)."
            )
        )
