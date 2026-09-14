"""Public logos keep stable cache keys while private media stays inaccessible."""

from http import HTTPStatus
from io import BytesIO
from unittest.mock import patch
from urllib.parse import urlsplit

from django.test import Client
import pytest

from apps.club.api.serializers import ClubSerializer
from apps.club.models import Club


pytestmark = pytest.mark.django_db


def test_public_logo_url_is_stable_and_download_is_cacheable(client: Client) -> None:
    """Repeated API reads reuse bytes instead of minting a fresh download token."""
    club = Club.objects.create(name="Synthetic logo club", logo="club_pictures/one.png")
    url = club.get_club_logo()
    with patch("django.core.signing.time.time", return_value=4_000_000_000):
        assert club.get_club_logo() == url
        assert ClubSerializer(club).data["logo_url"] == url
    assert not urlsplit(url).query
    with patch(
        "apps.club.api.logo.media_storage.open", return_value=BytesIO(b"logo")
    ) as opened:
        response = client.get(urlsplit(url).path)
        assert response.status_code == HTTPStatus.OK
        assert b"".join(response.streaming_content) == b"logo"
        assert response["Cache-Control"] == "public, max-age=2592000, immutable"
        assert response["X-Content-Type-Options"] == "nosniff"
        assert response["Content-Security-Policy"].startswith("sandbox")
        opened.assert_called_once_with("club_pictures/one.png")


def test_replacement_logo_has_a_new_cache_key(client: Client) -> None:
    """An old version can never cache replacement bytes under the wrong URL."""
    club = Club.objects.create(name="Replacement", logo="club_pictures/one.png")
    old_url = club.get_club_logo()
    club.logo = "club_pictures/two.png"
    club.save(update_fields=["logo"])
    assert club.get_club_logo() != old_url
    with patch("apps.club.api.logo.media_storage.open") as opened:
        assert client.get(urlsplit(old_url).path).status_code == HTTPStatus.NOT_FOUND
        opened.assert_not_called()


@pytest.mark.parametrize(
    "key", ["", "profile_pictures/private.png", "club_pictures/../private.png"]
)
def test_public_logo_route_cannot_read_private_or_missing_files(
    client: Client, key: str
) -> None:
    """The public route accepts only the current club's dedicated logo namespace."""
    club = Club.objects.create(name="No public logo", logo=key)
    path = f"/api/club/clubs/{club.pk}/logo/{club.logo_version}/"
    with patch("apps.club.api.logo.media_storage.open") as opened:
        assert client.get(path).status_code == HTTPStatus.NOT_FOUND
        opened.assert_not_called()


def test_missing_stored_logo_returns_uncached_404(client: Client) -> None:
    """A removed object does not poison the immutable cache with an error."""
    club = Club.objects.create(name="Missing object", logo="club_pictures/gone.png")
    with patch("apps.club.api.logo.media_storage.open", side_effect=FileNotFoundError):
        response = client.get(urlsplit(club.get_club_logo()).path)
    assert response.status_code == HTTPStatus.NOT_FOUND
    assert "immutable" not in response.get("Cache-Control", "")
