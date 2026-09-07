"""Run an incremental Sportlink import without storing credentials in Django."""

from __future__ import annotations

from argparse import ArgumentParser
import json
import os
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from apps.competition.adapters.outbound.sportlink import SportlinkClient
from apps.competition.composition import competition_client
from apps.competition.services.sync import sync
from apps.schedule.models import Season


class Command(BaseCommand):
    """Import the currently published competition feeds for an explicit season."""

    help = "Import Sportlink clubs/teams/poules/fixtures/results; rerun to resume."

    def add_arguments(self, parser: ArgumentParser) -> None:
        """Accept a season and a protected token file or environment variable."""
        parser.add_argument(
            "--season", required=True, help="Existing schedule.Season name"
        )
        credentials = parser.add_mutually_exclusive_group()
        credentials.add_argument(
            "--session-file",
            type=Path,
            help="Private OAuth JSON session, with automatic refresh",
        )
        credentials.add_argument(
            "--token-file",
            type=Path,
            help="File containing only the access token (mode 600)",
        )
        parser.add_argument("--max-requests", type=int, default=100)

    def handle(self, *args: object, **options: object) -> None:
        """Sync a bounded batch without exposing credentials.

        Raises:
            CommandError: Configuration is invalid or a provider resource failed.

        """
        try:
            season = Season.objects.get(name=options["season"])
        except Season.DoesNotExist as exc:
            raise CommandError(
                "Create the season and its dates before importing"
            ) from exc
        today = timezone.localdate()
        if not season.start_date <= today <= season.end_date:
            raise CommandError(
                "Live feeds only support the current season; "
                "historical pagination is unverified"
            )
        client = self._client(options)
        try:
            summary = sync(season, client, budget=int(str(options["max_requests"])))
        except ValueError as exc:
            raise CommandError(str(exc)) from exc
        finally:
            client.close()
        self.stdout.write(json.dumps(summary, sort_keys=True))
        if summary["reauth_required"]:
            raise CommandError(
                "Sign in again and replace the session file; import progress is saved"
            )
        if summary["failed"]:
            raise CommandError(
                "Some feeds failed; inspect competition SyncResource.last_error "
                "and retry later"
            )

    @staticmethod
    def _client(options: dict[str, object]) -> SportlinkClient:
        """Load private credentials at the command boundary.

        Raises:
            CommandError: Credentials cannot be loaded safely.

        """
        token_file = options["token_file"]
        if token_file is not None and not isinstance(token_file, Path):
            raise CommandError("Token file must be a path")
        token = os.environ.get("SPORTLINK_ACCESS_TOKEN", "")
        if token_file:
            if token_file.stat().st_mode & 0o077:
                raise CommandError(
                    "Token file must not be readable by group or others (chmod 600)"
                )
            token = token_file.read_text().strip()
        session_file = options.get("session_file")
        if session_file is not None and not isinstance(session_file, Path):
            raise CommandError("Session file must be a path")
        if not session_file and (not token or any(char.isspace() for char in token)):
            raise CommandError(
                "Provide SPORTLINK_ACCESS_TOKEN or --token-file "
                "containing a valid access token"
            )
        try:
            client = competition_client(
                token, session_file, os.environ.get("SPORTLINK_USER_AGENT", "")
            )
        except (OSError, ValueError, TypeError) as exc:
            raise CommandError(
                "Unable to load a private valid OAuth session file"
            ) from exc
        return client
