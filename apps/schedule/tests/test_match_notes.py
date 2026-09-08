"""Team privacy, author ownership and revision contracts for match notes."""

from dataclasses import dataclass
from http import HTTPStatus

from django.test import Client
from django.utils import timezone
import pytest

from apps.player.models import PlayerClubMembership
from apps.schedule.models import Match, MatchNote
from apps.team.models import Team, TeamData
from apps.team.tests.team_test_support import (
    TeamTestContext,
    build_team_context,
    create_player,
    create_season,
)


@dataclass
class NotesContext:
    """Two teams share a club, but never their private notes."""

    home: TeamTestContext
    away: Team
    match: Match
    url: str


@pytest.fixture
def notes() -> NotesContext:
    """Build a fixture with season-specific roster membership."""
    home = build_team_context(suffix="notes")
    away = Team.objects.create(name="Other team", club=home.club)
    match = Match.objects.create(
        home_team=home.team,
        away_team=away,
        season=home.season,
        start_time=timezone.now(),
    )
    return NotesContext(home, away, match, f"/api/matches/{match.pk}/notes/")


UPDATED_REVISION = 2
NOTE_COUNT = 3

pytestmark = pytest.mark.django_db


@pytest.mark.parametrize("role", ["players", "coach", "staff"])
def test_roster_roles_can_share_notes(
    client: Client, notes: NotesContext, role: str
) -> None:
    """All roster roles can add notes and read one another's observations."""
    member = create_player(username=f"member_{role}")
    getattr(notes.home.team_data, role).add(member)
    client.force_login(member.user)
    url = f"{notes.url}?team={notes.home.team.pk}"
    response = client.post(
        url, {"text": "  Prepare the rebound  "}, content_type="application/json"
    )
    assert response.status_code == HTTPStatus.CREATED
    assert response.json()["text"] == "Prepare the rebound"
    assert response.json()["revision"] == 1
    assert response.json()["can_edit"] is True
    client.force_login(notes.home.player.user)
    response = client.get(url)
    assert response.status_code == HTTPStatus.OK
    assert response.json()["results"][0]["can_edit"] is False
    assert response["Cache-Control"] == "private, no-store"
    assert "Cookie" in response["Vary"]


@pytest.mark.parametrize(
    "outsider",
    ["anonymous", "staff", "same_club", "other_season", "opponent", "follower"],
)
def test_outsiders_cannot_read_or_add(
    client: Client, notes: NotesContext, outsider: str
) -> None:
    """Authentication and broader membership never replace selected-team access."""
    member = create_player(username="outsider")
    if outsider == "staff":
        member.user.is_staff = True
        member.user.is_superuser = True
        member.user.save()
    elif outsider == "follower":
        member.team_follow.add(notes.home.team)
        member.club_follow.add(notes.home.club)
    elif outsider == "same_club":
        PlayerClubMembership.objects.create(player=member, club=notes.home.club)
    elif outsider == "opponent":
        TeamData.objects.create(team=notes.away, season=notes.home.season).players.add(
            member
        )
    elif outsider == "other_season":
        TeamData.objects.create(
            team=notes.home.team, season=create_season("Other season")
        ).players.add(member)
    if outsider != "anonymous":
        client.force_login(member.user)
    url = f"{notes.url}?team={notes.home.team.pk}"
    for response in [
        client.get(url),
        client.post(url, {"text": "secret"}, content_type="application/json"),
    ]:
        assert response.status_code in {HTTPStatus.UNAUTHORIZED, HTTPStatus.FORBIDDEN}
        assert response["Content-Type"].startswith("application/json")
        assert response["Cache-Control"] == "private, no-store"
    assert not MatchNote.objects.exists()


def test_author_edits_are_revision_checked_and_team_scoped(
    client: Client, notes: NotesContext
) -> None:
    """A stale save/delete, teammate or wrong scope cannot change a note."""
    client.force_login(notes.home.player.user)
    url = f"{notes.url}?team={notes.home.team.pk}"
    created = client.post(
        url, {"text": "Original"}, content_type="application/json"
    ).json()
    detail = f"{notes.url}{created['id']}/?team={notes.home.team.pk}"
    response = client.patch(
        detail,
        {"text": "Edited", "expected_revision": 1},
        content_type="application/json",
    )
    assert response.status_code == HTTPStatus.OK
    assert response.json()["revision"] == UPDATED_REVISION
    for method in [client.patch, client.delete]:
        response = method(
            detail,
            {"text": "Stale", "expected_revision": 1},
            content_type="application/json",
        )
        assert response.status_code == HTTPStatus.CONFLICT
        assert response.json()["code"] == "revision_conflict"
    note = MatchNote.objects.get(pk=created["id"])
    assert note.text == "Edited"
    client.force_login(notes.home.coach.user)
    for method in [client.patch, client.delete]:
        assert (
            method(
                detail,
                {"text": "Hijacked", "expected_revision": 2},
                content_type="application/json",
            ).status_code
            == HTTPStatus.FORBIDDEN
        )
    client.force_login(notes.home.player.user)
    away_data = TeamData.objects.create(team=notes.away, season=notes.home.season)
    away_data.players.add(notes.home.player)
    assert client.get(f"{notes.url}?team={notes.away.pk}").json()["results"] == []
    wrong_team = f"{notes.url}{note.pk}/?team={notes.away.pk}"
    assert (
        client.delete(
            wrong_team, {"expected_revision": 2}, content_type="application/json"
        ).status_code
        == HTTPStatus.NOT_FOUND
    )
    other_match = Match.objects.create(
        home_team=notes.home.team,
        away_team=notes.away,
        season=notes.home.season,
        start_time=timezone.now(),
    )
    wrong_match = (
        f"/api/matches/{other_match.pk}/notes/{note.pk}/?team={notes.home.team.pk}"
    )
    assert (
        client.delete(
            wrong_match, {"expected_revision": 2}, content_type="application/json"
        ).status_code
        == HTTPStatus.NOT_FOUND
    )
    assert (
        client.delete(
            detail, {"expected_revision": 2}, content_type="application/json"
        ).status_code
        == HTTPStatus.NO_CONTENT
    )
    assert not MatchNote.objects.exists()


def test_removed_member_loses_access_even_to_own_notes(
    client: Client, notes: NotesContext
) -> None:
    """Previously authored notes confer no ongoing membership privilege."""
    note = MatchNote.objects.create(
        match=notes.match,
        team=notes.home.team,
        author=notes.home.player.user,
        text="Private",
    )
    client.force_login(notes.home.player.user)
    notes.home.team_data.players.remove(notes.home.player)
    assert (
        client.get(f"{notes.url}?team={notes.home.team.pk}").status_code
        == HTTPStatus.FORBIDDEN
    )
    assert (
        client.delete(
            f"{notes.url}{note.pk}/?team={notes.home.team.pk}",
            {"expected_revision": 1},
            content_type="application/json",
        ).status_code
        == HTTPStatus.FORBIDDEN
    )


@pytest.mark.parametrize("text", ["", "   ", "x" * 4001])
def test_invalid_text_rejected(client: Client, notes: NotesContext, text: str) -> None:
    """Reject empty and excessive note text."""
    client.force_login(notes.home.player.user)
    assert (
        client.post(
            f"{notes.url}?team={notes.home.team.pk}",
            {"text": text},
            content_type="application/json",
        ).status_code
        == HTTPStatus.BAD_REQUEST
    )


def test_scope_validation_and_pagination(client: Client, notes: NotesContext) -> None:
    """The selected team is required, and lists are bounded."""
    client.force_login(notes.home.player.user)
    assert client.get(notes.url).status_code == HTTPStatus.BAD_REQUEST
    assert client.get(f"{notes.url}?team=invalid").status_code == HTTPStatus.BAD_REQUEST
    for index in range(3):
        MatchNote.objects.create(
            match=notes.match,
            team=notes.home.team,
            author=notes.home.player.user,
            text=str(index),
        )
    response = client.get(f"{notes.url}?team={notes.home.team.pk}&page_size=2")
    assert response.json()["count"] == NOTE_COUNT
    assert len(response.json()["results"]) == UPDATED_REVISION
    assert response.json()["next"] is not None
