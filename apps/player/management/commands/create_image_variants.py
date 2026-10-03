"""Create the small copies of club logos and profile pictures ahead of requests."""

from django.core.management.base import BaseCommand, CommandParser

from apps.club.models import Club
from apps.player.composition import image_variant_storage
from apps.player.media_paths import PROFILE_PICTURE_PREFIX
from apps.player.models import Player
from apps.player.services.image_variants import VariantBusyError, image_variant


class Command(BaseCommand):
    """Copies are otherwise made by the first request, inside a web worker."""

    help = (
        "Create missing 256-pixel WebP copies of club logos and, with --pictures, "
        "of profile pictures."
    )

    def add_arguments(self, parser: CommandParser) -> None:
        """Logos are public and on every schedule; pictures are opt-in."""
        parser.add_argument("--pictures", action="store_true")

    def handle(self, *args: object, **options: object) -> None:
        """Create each copy once; report originals that are missing or unreadable."""
        keys = list(
            Club.objects
            .filter(logo__startswith="club_pictures/")
            .values_list("logo", flat=True)
            .distinct()
        )
        if options["pictures"]:
            keys += Player.all_objects.filter(
                profile_picture__startswith=PROFILE_PICTURE_PREFIX
            ).values_list("profile_picture", flat=True)
        created = skipped = 0
        for key in keys:
            try:
                resized = image_variant(image_variant_storage, key)
            except (FileNotFoundError, VariantBusyError):
                resized = None
            if resized is None:
                skipped += 1
                self.stderr.write(f"Skipped {key}: missing, busy or not an image.")
            else:
                created += 1
        self.stdout.write(
            self.style.SUCCESS(f"{created} copies ready, {skipped} skipped.")
        )
