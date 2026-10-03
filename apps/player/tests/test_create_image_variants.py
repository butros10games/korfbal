"""The warm-up command creates logo copies and reports unusable originals."""

from io import BytesIO, StringIO
from pathlib import Path

from django.core.files.storage import default_storage
from django.core.management import call_command
from PIL import Image
import pytest
from pytest_django.fixtures import Settings

from apps.club.models import Club
from apps.player.media_paths import variant_key


pytestmark = pytest.mark.django_db


def test_creates_logo_copies_and_reports_skipped_originals(
    settings: Settings, tmp_path: Path
) -> None:
    """Readable logos get a copy; missing or unreadable ones are only reported."""
    settings.MEDIA_ROOT = tmp_path
    encoded = BytesIO()
    Image.new("RGB", (600, 600), "white").save(encoded, "PNG")
    default_storage.save("club_pictures/good.png", BytesIO(encoded.getvalue()))
    default_storage.save("club_pictures/odd.png", BytesIO(b"not an image"))
    for name in ("good", "odd", "gone"):
        Club.objects.create(name=name, logo=f"club_pictures/{name}.png")
    out, err = StringIO(), StringIO()

    call_command("create_image_variants", stdout=out, stderr=err)

    assert default_storage.exists(variant_key("club_pictures/good.png"))
    assert "1 copies ready, 2 skipped." in out.getvalue()
    assert "club_pictures/gone.png" in err.getvalue()
