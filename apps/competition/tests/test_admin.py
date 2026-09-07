"""Staff can inspect imported identities without changing provider-owned records."""

from django.contrib.auth import get_user_model
from django.test import Client
from django.urls import reverse
import pytest
from rest_framework import status

from apps.club.models import Club as LocalClub
from apps.competition.models import Club


@pytest.mark.django_db
def test_catalogue_admin_and_existing_club_link() -> None:
    """Render source records in their own admin and link existing clubs to them."""
    user = get_user_model().objects.create_superuser(username="catalogue-admin")
    client = Client()
    client.force_login(user)
    local = LocalClub.objects.create(name="DTS")
    club = Club.objects.create(
        external_id="DTS-E", name="DTS (E)", city="Enkhuizen", local_club=local
    )
    detail_url = reverse("admin:competition_club_change", args=(club.pk,))
    listing = client.get(
        reverse("admin:competition_club_changelist"), {"q": "Enkhuizen"}
    )
    assert listing.status_code == status.HTTP_200_OK
    assert b"DTS (E)" in listing.content
    existing = client.get(reverse("admin:club_club_changelist"))
    assert detail_url.encode() in existing.content
    assert client.get(detail_url).status_code == status.HTTP_200_OK
    assert (
        client.post(detail_url, {"name": "changed"}).status_code
        == status.HTTP_403_FORBIDDEN
    )
    assert (
        client.post(reverse("admin:competition_club_add"), {}).status_code
        == status.HTTP_403_FORBIDDEN
    )
    assert (
        client.post(
            reverse("admin:competition_club_delete", args=(club.pk,)), {"post": "yes"}
        ).status_code
        == status.HTTP_403_FORBIDDEN
    )
    club.refresh_from_db()
    assert club.name == "DTS (E)"


@pytest.mark.django_db
@pytest.mark.parametrize(
    "model",
    [
        "club",
        "teamgroup",
        "team",
        "pool",
        "match",
        "syncresource",
        "resultrevision",
        "trafficstate",
        "synclease",
    ],
)
def test_catalogue_admin_lists_are_registered(model: str) -> None:
    """Every catalogue and import progress model has a working admin list."""
    client = Client()
    client.force_login(
        get_user_model().objects.create_superuser(username="catalogue-admin")
    )
    assert (
        client.get(reverse(f"admin:competition_{model}_changelist")).status_code
        == status.HTTP_200_OK
    )
