"""Verify the MinIO media policy and optionally repair/probe it."""

from django.core.management.base import BaseCommand, CommandError, CommandParser

from apps.player.composition import check_media_privacy


class Command(BaseCommand):
    """Provide an explicit, repeatable media-privacy deployment check."""

    help = (
        "Check the MinIO media bucket; --repair removes public grants, "
        "--probe uses a synthetic object."
    )

    def add_arguments(self, parser: CommandParser) -> None:
        """Keep policy mutation and the synthetic write probe explicit."""
        parser.add_argument("--repair", action="store_true")
        parser.add_argument("--probe", action="store_true")

    def handle(self, *args: object, **options: object) -> None:
        """Fail deployment verification when privacy cannot be established.

        Raises:
            CommandError: The policy or probe failed.

        """
        try:
            check_media_privacy(
                repair=bool(options["repair"]), probe=bool(options["probe"])
            )
        except Exception as error:
            raise CommandError(
                "Media privacy verification failed; inspect storage policy and access."
            ) from error
        self.stdout.write(self.style.SUCCESS("Media bucket policy verified."))
        if options["probe"]:
            self.stdout.write(
                self.style.SUCCESS(
                    "Anonymous synthetic-object access denied; probe removed."
                )
            )
