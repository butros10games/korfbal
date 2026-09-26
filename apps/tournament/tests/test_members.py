"""Tournament collaboration grants reject duplicate and stale identities."""

from http import HTTPStatus

from django.contrib.auth import get_user_model
from django.test import Client
from django.utils import timezone
import pytest

from apps.tournament.models import Tournament, TournamentField, TournamentMember


pytestmark = pytest.mark.django_db


@pytest.mark.parametrize("method", ["post", "patch"])
def test_member_write_rejects_duplicate_user(client: Client, method: str) -> None:
    """Duplicate users produce field validation errors and preserve both grants."""
    owner = get_user_model().objects.create_user(username="membership-owner")
    member_user = get_user_model().objects.create_user(username="existing-member")
    other_user = get_user_model().objects.create_user(username="other-member")
    tournament = Tournament.objects.create(
        name="Memberships", slug="memberships", owner=owner, starts_at=timezone.now()
    )
    field = TournamentField.objects.create(tournament=tournament, label="Field 1")
    TournamentMember.objects.create(
        tournament=tournament,
        user=member_user,
        role=TournamentMember.Role.SCOREKEEPER,
        field=field,
    )
    other = TournamentMember.objects.create(
        tournament=tournament, user=other_user, role=TournamentMember.Role.MANAGER
    )
    before = list(tournament.member_roles.order_by("pk").values())
    client.force_login(owner)
    url = f"/api/tournaments/{tournament.pk}/members/"
    if method == "patch":
        url += f"{other.pk}/"

    response = getattr(client, method)(
        url,
        {"user": member_user.pk, "role": "manager", "field": None},
        content_type="application/json",
    )

    assert response.status_code == HTTPStatus.BAD_REQUEST
    assert "user" in response.json()
    assert list(tournament.member_roles.order_by("pk").values()) == before


def test_member_can_be_updated_and_granted_in_another_tournament(
    client: Client,
) -> None:
    """Uniqueness is tournament-scoped and allows editing the existing grant."""
    owner = get_user_model().objects.create_user(username="membership-owner")
    member_user = get_user_model().objects.create_user(username="existing-member")
    tournaments = [
        Tournament.objects.create(
            name=f"Tournament {index}",
            slug=f"tournament-{index}",
            owner=owner,
            starts_at=timezone.now(),
        )
        for index in range(2)
    ]
    member = TournamentMember.objects.create(
        tournament=tournaments[0],
        user=member_user,
        role=TournamentMember.Role.SCOREKEEPER,
    )
    client.force_login(owner)

    updated = client.patch(
        f"/api/tournaments/{tournaments[0].pk}/members/{member.pk}/",
        {"role": "manager"},
        content_type="application/json",
    )
    added = client.post(
        f"/api/tournaments/{tournaments[1].pk}/members/",
        {"user": member_user.pk, "role": "scorekeeper"},
        content_type="application/json",
    )

    assert updated.status_code == HTTPStatus.OK
    assert updated.json()["id"] == member.pk
    assert updated.json()["role"] == "manager"
    assert added.status_code == HTTPStatus.CREATED
    assert added.json()["user"] == member_user.pk
    assert (
        tournaments[0].member_roles.count() == tournaments[1].member_roles.count() == 1
    )
