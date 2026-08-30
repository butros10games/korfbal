"""Regression tests for club-admin membership workflows."""

from __future__ import annotations

from datetime import timedelta
from http import HTTPStatus

from django.contrib.auth.models import User
from django.test.client import Client
from django.utils import timezone
import pytest

from apps.club.models import Club
from apps.player.models import Player, PlayerClubMembership


SEARCH_RESULT_LIMIT = 20


def _create_user_and_player(username: str) -> tuple[User, Player]:
    user = User.objects.create_user(username=username)
    return user, Player.objects.get(user=user)


@pytest.mark.django_db
def test_admin_actions_reject_anonymous_and_other_club_admins(client: Client) -> None:
    """An admin role must be scoped to the club in the request URL."""
    target_club = Club.objects.create(name="Target Club")
    other_club = Club.objects.create(name="Other Club")
    other_admin, other_admin_player = _create_user_and_player("other-admin")
    other_club.admin.add(other_admin_player)
    _, member = _create_user_and_player("protected-member")
    membership = PlayerClubMembership.objects.create(
        club=target_club,
        player=member,
    )

    add_url = f"/api/club/clubs/{target_club.id_uuid}/memberships/"
    remove_url = f"{add_url}{member.id_uuid}/"

    anonymous_response = client.post(
        add_url,
        data={"username": "protected-member"},
        content_type="application/json",
    )
    assert anonymous_response.status_code in {
        HTTPStatus.UNAUTHORIZED,
        HTTPStatus.FORBIDDEN,
    }

    client.force_login(other_admin)
    add_response = client.post(
        add_url,
        data={"username": "protected-member"},
        content_type="application/json",
    )
    remove_response = client.delete(remove_url)

    assert add_response.status_code == HTTPStatus.FORBIDDEN
    assert remove_response.status_code == HTTPStatus.FORBIDDEN
    membership.refresh_from_db()
    assert membership.end_date is None


@pytest.mark.django_db
def test_add_membership_requires_an_identifier(client: Client) -> None:
    """Empty membership requests should fail before changing membership history."""
    club = Club.objects.create(name="Identifier Club")
    admin, admin_player = _create_user_and_player("identifier-admin")
    club.admin.add(admin_player)
    client.force_login(admin)

    response = client.post(
        f"/api/club/clubs/{club.id_uuid}/memberships/",
        data={},
        content_type="application/json",
    )

    assert response.status_code == HTTPStatus.BAD_REQUEST
    assert response.json()["non_field_errors"] == [
        "Provide one of: player_id, user_id, username."
    ]
    assert not PlayerClubMembership.objects.filter(club=club).exists()


@pytest.mark.django_db
def test_add_membership_by_user_id_recreates_a_missing_player(client: Client) -> None:
    """Legacy users without a Player row should still be addable by user ID."""
    club = Club.objects.create(name="Legacy User Club")
    admin, admin_player = _create_user_and_player("legacy-admin")
    club.admin.add(admin_player)
    legacy_user, legacy_player = _create_user_and_player("legacy-user")
    legacy_player.delete()
    assert not Player.objects.filter(user=legacy_user).exists()
    client.force_login(admin)

    response = client.post(
        f"/api/club/clubs/{club.id_uuid}/memberships/",
        data={"user_id": legacy_user.pk},
        content_type="application/json",
    )

    assert response.status_code == HTTPStatus.CREATED
    membership = PlayerClubMembership.objects.get(club=club)
    assert membership.player.user_id == legacy_user.pk
    assert response.json()["player"]["username"] == "legacy-user"


@pytest.mark.django_db
def test_closed_member_can_rejoin_without_losing_history(client: Client) -> None:
    """Rejoining creates a new open period and preserves the closed period."""
    club = Club.objects.create(name="Rejoin Club")
    admin, admin_player = _create_user_and_player("rejoin-admin")
    club.admin.add(admin_player)
    _, member = _create_user_and_player("returning-member")
    today = timezone.localdate()
    old_membership = PlayerClubMembership.objects.create(
        club=club,
        player=member,
        start_date=today - timedelta(days=60),
        end_date=today - timedelta(days=30),
    )
    client.force_login(admin)

    response = client.post(
        f"/api/club/clubs/{club.id_uuid}/memberships/",
        data={"player_id": str(member.id_uuid), "start_date": today.isoformat()},
        content_type="application/json",
    )

    assert response.status_code == HTTPStatus.CREATED
    memberships = list(
        PlayerClubMembership.objects.filter(club=club, player=member).order_by(
            "start_date"
        )
    )
    assert memberships == [
        old_membership,
        PlayerClubMembership.objects.get(pk=response.json()["id_uuid"]),
    ]
    assert memberships[0].end_date == today - timedelta(days=30)
    assert memberships[1].start_date == today
    assert memberships[1].end_date is None


@pytest.mark.django_db
def test_settings_returns_only_current_members_sorted_by_username(
    client: Client,
) -> None:
    """The settings roster excludes expired and not-yet-started membership periods."""
    club = Club.objects.create(name="Settings Roster Club")
    admin, admin_player = _create_user_and_player("settings-admin")
    club.admin.add(admin_player)
    today = timezone.localdate()

    for username, start_offset, end_offset in (
        ("z-current", -10, None),
        ("a-current", -20, 0),
        ("expired", -20, -1),
        ("future", 1, None),
    ):
        _, player = _create_user_and_player(username)
        PlayerClubMembership.objects.create(
            club=club,
            player=player,
            start_date=today + timedelta(days=start_offset),
            end_date=(
                today + timedelta(days=end_offset) if end_offset is not None else None
            ),
        )

    client.force_login(admin)
    response = client.get(f"/api/club/clubs/{club.id_uuid}/settings/")

    assert response.status_code == HTTPStatus.OK
    assert [
        membership["player"]["username"] for membership in response.json()["members"]
    ] == ["a-current", "z-current"]


@pytest.mark.django_db
def test_user_search_is_trimmed_ordered_and_capped(client: Client) -> None:
    """Admin user search should stay deterministic and bounded for broad terms."""
    club = Club.objects.create(name="Bounded Search Club")
    admin, admin_player = _create_user_and_player("search-admin")
    club.admin.add(admin_player)
    for index in range(25, -1, -1):
        User.objects.create_user(username=f"candidate-{index:02d}")
    client.force_login(admin)

    response = client.get(
        f"/api/club/clubs/{club.id_uuid}/settings/user-search/",
        {"search": "  CANDIDATE-  "},
    )

    assert response.status_code == HTTPStatus.OK
    results = response.json()["results"]
    assert len(results) == SEARCH_RESULT_LIMIT
    assert [row["username"] for row in results] == [
        f"candidate-{index:02d}" for index in range(SEARCH_RESULT_LIMIT)
    ]
