"""Resized copies never outlive their original and stay within decoding limits."""

from http import HTTPStatus
from io import BytesIO
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlsplit

from django.core.files.storage import default_storage
from django.test import Client
from PIL import Image
import pytest
from pytest_django.fixtures import Settings

from apps.club.models import Club
from apps.player.adapters.outbound.private_storage import PrivateMediaStorage
from apps.player.adapters.outbound.storage import DjangoImageVariantStorage
from apps.player.media_paths import delete_with_variant, variant_key, variant_url
from apps.player.services import image_variants
from apps.player.services.image_variants import VariantBusyError, image_variant


pytestmark = pytest.mark.django_db
KEY = "profile_pictures/owner/avatar.png"


@pytest.fixture(autouse=True)
def media_root(settings: Settings, tmp_path: Path) -> Path:
    """Store pictures in a private temporary directory."""
    settings.MEDIA_ROOT = tmp_path
    return tmp_path


def _store(key: str = KEY, size: tuple[int, int] = (800, 600)) -> None:
    encoded = BytesIO()
    Image.new("RGB", size, "teal").save(encoded, "PNG")
    default_storage.save(key, BytesIO(encoded.getvalue()))


def test_deletion_during_resizing_leaves_no_copy() -> None:
    """A copy written after the original was withdrawn is removed again."""
    _store()
    storage = DjangoImageVariantStorage()
    resize = image_variants._resized

    def withdrawn_meanwhile(original: bytes) -> bytes | None:
        resized = resize(original)
        delete_with_variant(default_storage, KEY)
        return resized

    with (
        patch.object(image_variants, "_resized", withdrawn_meanwhile),
        pytest.raises(FileNotFoundError),
    ):
        image_variant(storage, KEY)

    assert not default_storage.exists(variant_key(KEY))
    # The reservation was released for the next attempt.
    assert storage.claim(variant_key(KEY))
    storage.release(variant_key(KEY))


def test_copy_is_not_served_without_its_original() -> None:
    """A leftover copy cannot reveal a deleted picture."""
    default_storage.save(variant_key(KEY), BytesIO(b"leftover"))
    with pytest.raises(FileNotFoundError):
        image_variant(DjangoImageVariantStorage(), KEY)


@pytest.mark.parametrize(
    ("limit", "value"),
    [
        ("MAX_SOURCE_BYTES", 100),
        ("MAX_SOURCE_PIXELS", 800 * 600 - 1),
    ],
)
def test_sources_beyond_the_decoding_limits_are_not_resized(
    limit: str, value: int
) -> None:
    """Large files and dimensions are rejected before decoding."""
    _store()
    with patch.object(image_variants, limit, value):
        assert image_variant(DjangoImageVariantStorage(), KEY) is None
    assert not default_storage.exists(variant_key(KEY))


def test_decompression_bomb_warning_is_rejected() -> None:
    """Pillow's bomb warning counts as a failure, not as a slow success."""
    _store()
    with patch.object(Image, "MAX_IMAGE_PIXELS", 800 * 600 - 1):
        assert image_variant(DjangoImageVariantStorage(), KEY) is None


def test_concurrent_first_requests_resize_once(client: Client) -> None:
    """While one request creates a copy, others serve the original briefly."""
    _store()
    _store("club_pictures/logo.png")
    storage = DjangoImageVariantStorage()
    club = Club.objects.create(name="Busy", logo="club_pictures/logo.png")
    assert storage.claim(variant_key(KEY))
    assert storage.claim(variant_key("club_pictures/logo.png"))
    with pytest.raises(VariantBusyError):
        image_variant(storage, KEY)

    logo = client.get(urlsplit(club.get_club_logo()).path)
    assert logo.status_code == HTTPStatus.OK
    assert logo["Cache-Control"] == "public, max-age=60"
    assert logo.get("Content-Type") != "image/webp"

    signed = PrivateMediaStorage(
        access_key="synthetic", secret_key="synthetic", bucket_name="test"
    ).url(KEY)
    url = urlsplit(variant_url(signed))
    picture = client.get(f"{url.path}?{url.query}")
    assert picture.status_code == HTTPStatus.OK
    assert picture["Cache-Control"] == "private, max-age=60"
    assert not default_storage.exists(variant_key(KEY))
    storage.release(variant_key(KEY))
    storage.release(variant_key("club_pictures/logo.png"))
