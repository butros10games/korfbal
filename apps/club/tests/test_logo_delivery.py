"""Public logos keep stable cache keys while private media stays inaccessible."""

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

from apps.club.api.serializers import ClubSerializer
from apps.club.models import Club


pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def media_root(settings: Settings, tmp_path: Path) -> Path:
    """Store logos in a private temporary directory."""
    settings.MEDIA_ROOT = tmp_path
    return tmp_path


def _store_png(key: str, size: tuple[int, int] = (1024, 512)) -> None:
    encoded = BytesIO()
    Image.new("RGBA", size, (200, 30, 30, 128)).save(encoded, "PNG")
    default_storage.save(key, BytesIO(encoded.getvalue()))


def test_public_logo_url_is_stable_and_serves_a_small_cacheable_copy(
    client: Client,
) -> None:
    """Repeated API reads share one short URL that serves a 256-pixel WebP."""
    _store_png("club_pictures/one.png")
    club = Club.objects.create(name="Synthetic logo club", logo="club_pictures/one.png")
    url = club.get_club_logo()
    with patch("django.core.signing.time.time", return_value=4_000_000_000):
        assert club.get_club_logo() == url
        assert ClubSerializer(club).data["logo_url"] == url
    assert not urlsplit(url).query
    assert urlsplit(url).path.endswith(f"/logo/{club.logo_version[:16]}/w256/")

    response = client.get(urlsplit(url).path)

    assert response.status_code == HTTPStatus.OK
    assert response["Content-Type"] == "image/webp"
    assert response["Cache-Control"] == "public, max-age=2592000, immutable"
    assert response["X-Content-Type-Options"] == "nosniff"
    assert response["Content-Security-Policy"].startswith("sandbox")
    with Image.open(BytesIO(response.content)) as image:
        assert (image.format, image.size, image.mode) == ("WEBP", (256, 128), "RGBA")
    # The copy is stored once beside the original and reused afterwards.
    assert default_storage.exists("club_pictures/one.png.w256.webp")
    with patch("apps.club.api.logo.image_variant_storage.write") as write:
        assert client.get(urlsplit(url).path).content == response.content
        write.assert_not_called()


def test_full_digest_url_still_serves_the_original(client: Client) -> None:
    """URLs cached before the short version keep working."""
    _store_png("club_pictures/one.png")
    club = Club.objects.create(name="Old URL", logo="club_pictures/one.png")

    response = client.get(f"/api/club/clubs/{club.pk}/logo/{club.logo_version}/")

    assert response.status_code == HTTPStatus.OK
    assert (
        b"".join(response.streaming_content)
        == default_storage.open("club_pictures/one.png").read()
    )
    assert response["Cache-Control"] == "public, max-age=2592000, immutable"


def test_unreadable_logo_falls_back_to_the_original(client: Client) -> None:
    """A file Pillow cannot read is served unchanged rather than failing."""
    default_storage.save("club_pictures/odd.png", BytesIO(b"not an image"))
    club = Club.objects.create(name="Odd logo", logo="club_pictures/odd.png")

    response = client.get(urlsplit(club.get_club_logo()).path)

    assert response.status_code == HTTPStatus.OK
    assert b"".join(response.streaming_content) == b"not an image"
    assert not default_storage.exists("club_pictures/odd.png.w256.webp")


def test_replacement_logo_has_a_new_cache_key(client: Client) -> None:
    """An old version can never cache replacement bytes under the wrong URL."""
    _store_png("club_pictures/two.png")
    club = Club.objects.create(name="Replacement", logo="club_pictures/one.png")
    old_url = club.get_club_logo()
    club.logo = "club_pictures/two.png"
    club.save(update_fields=["logo"])
    assert club.get_club_logo() != old_url
    assert client.get(urlsplit(old_url).path).status_code == HTTPStatus.NOT_FOUND


@pytest.mark.parametrize(
    "key", ["", "profile_pictures/private.png", "club_pictures/../private.png"]
)
@pytest.mark.parametrize("suffix", ["", "w256/"])
def test_public_logo_route_cannot_read_private_or_missing_files(
    client: Client, key: str, suffix: str
) -> None:
    """The public routes accept only the current club's dedicated logo namespace."""
    _store_png("profile_pictures/private.png")
    club = Club.objects.create(name="No public logo", logo=key)
    path = f"/api/club/clubs/{club.pk}/logo/{club.logo_version[:16]}/{suffix}"
    if not suffix:
        path = f"/api/club/clubs/{club.pk}/logo/{club.logo_version}/"
    assert client.get(path).status_code == HTTPStatus.NOT_FOUND
    assert not default_storage.exists("profile_pictures/private.png.w256.webp")


def test_unknown_variant_is_not_found(client: Client) -> None:
    """Only the published size can be requested."""
    _store_png("club_pictures/one.png")
    club = Club.objects.create(name="Sizes", logo="club_pictures/one.png")
    path = f"/api/club/clubs/{club.pk}/logo/{club.logo_version[:16]}/w2048/"
    assert client.get(path).status_code == HTTPStatus.NOT_FOUND


def test_missing_stored_logo_returns_uncached_404(client: Client) -> None:
    """A removed object does not poison the immutable cache with an error."""
    club = Club.objects.create(name="Missing object", logo="club_pictures/gone.png")
    response = client.get(urlsplit(club.get_club_logo()).path)
    assert response.status_code == HTTPStatus.NOT_FOUND
    assert "immutable" not in response.get("Cache-Control", "")
