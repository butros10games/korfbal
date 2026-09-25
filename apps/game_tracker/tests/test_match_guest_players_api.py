"""Contracts for adding club-less guest players to a single match selection."""

from http import HTTPStatus

from django.http import HttpResponse
from django.test.client import Client
from django.utils import timezone
import pytest

from apps.game_tracker.models import MatchGuestPlayer, MatchPlayer
from apps.game_tracker.tests.tracker_test_helpers import (
    TrackerMatchContext,
    create_group_types,
    create_tracker_match,
    create_tracker_player,
    create_tracker_user,
    get_tracker_group,
    login_home_club_editor,
)
from apps.kwt_common.tests.api_test_support import assert_api_error
from apps.player.models import Player, PlayerClubMembership
from apps.schedule.models import Match


pytestmark = pytest.mark.django_db

DESIGNATION_URL = "/api/match/player_designation/"


def _guest_url(tracker: TrackerMatchContext, team_id: object | None = None) -> str:
    team = team_id or tracker.home_team.pk
    return f"/api/match/guest_player/{tracker.match.pk}/{team}/"


def _add_guest(
    client: Client, tracker: TrackerMatchContext, name: str, **overrides: object
) -> HttpResponse:
    return client.post(
        overrides.pop("url", None) or _guest_url(tracker),
        data={"name": name, "expected_revision": tracker.match_data.live_revision}
        | overrides,
        content_type="application/json",
    )


def test_editor_adds_guest_to_reserve_for_this_match_only(client: Client) -> None:
    """A guest is an account-less player placed in this match's reserve."""
    tracker = create_tracker_match(prefix="Guest add")
    create_group_types("Aanval", "Reserve")
    reserve = get_tracker_group(tracker, "Reserve")
    login_home_club_editor(client, tracker, "guest-add-editor")
    expected_revision = tracker.match_data.live_revision

    response = _add_guest(client, tracker, "  Invaller   de Vries ")

    assert response.status_code == HTTPStatus.CREATED
    body = response.json()
    assert body["live_revision"] == expected_revision + 1
    assert body["player"]["user"] == {"username": "Invaller de Vries"}
    assert body["player"]["is_guest"] is True
    guest = Player.objects.get(pk=body["player"]["id_uuid"])
    assert guest.user_id is None
    assert guest.knkv_person_id is None
    assert not PlayerClubMembership.objects.filter(player=guest).exists()
    assert list(reserve.players.all()) == [guest]
    assert MatchPlayer.objects.filter(
        match_data=tracker.match_data, team=tracker.home_team, player=guest
    ).exists()
    assert MatchGuestPlayer.objects.get(player=guest).team == tracker.home_team

    overview = client.get(
        f"/api/match/player_overview_data/{tracker.match.pk}/{tracker.home_team.pk}/"
    ).json()
    reserve_payload = next(
        group
        for group in overview["player_groups"]
        if group["starting_type"]["name"] == "Reserve"
    )
    assert reserve_payload["players"][0]["is_guest"] is True

    # The guest can move onto the court like any reserve player.
    attack = get_tracker_group(tracker, "Aanval")
    moved = client.post(
        DESIGNATION_URL,
        data={
            "new_group_id": str(attack.pk),
            "players": [{"id_uuid": str(guest.pk), "groupId": str(reserve.pk)}],
            "expected_revision": body["live_revision"],
        },
        content_type="application/json",
    )
    assert moved.status_code == HTTPStatus.OK
    assert list(attack.players.all()) == [guest]


def test_removed_guest_stays_selectable_only_in_its_own_match(client: Client) -> None:
    """Removing a guest keeps them re-addable here without leaking elsewhere."""
    tracker = create_tracker_match(prefix="Guest scope")
    create_group_types("Reserve")
    reserve = get_tracker_group(tracker, "Reserve")
    login_home_club_editor(client, tracker, "guest-scope-editor")
    guest_id = _add_guest(client, tracker, "Gastspeler Jansen").json()["player"][
        "id_uuid"
    ]
    tracker.match_data.refresh_from_db()
    removed = client.post(
        DESIGNATION_URL,
        data={
            "new_group_id": None,
            "players": [{"id_uuid": guest_id, "groupId": str(reserve.pk)}],
            "expected_revision": tracker.match_data.live_revision,
        },
        content_type="application/json",
    )
    assert removed.status_code == HTTPStatus.OK

    base = f"{tracker.match.pk}/{tracker.home_team.pk}/"
    available = client.get(f"/api/match/players_team/{base}").json()["players"]
    assert [(p["id_uuid"], p["is_guest"]) for p in available] == [(guest_id, True)]
    search = client.get(f"/api/match/player_search/{base}", {"search": "jansen"})
    assert [p["id_uuid"] for p in search.json()["players"]] == [guest_id]

    other_match = Match.objects.create(
        home_team=tracker.home_team,
        away_team=tracker.away_team,
        season=tracker.match.season,
        start_time=timezone.now(),
    )
    other_base = f"{other_match.pk}/{tracker.home_team.pk}/"
    assert client.get(f"/api/match/players_team/{other_base}").json() == {"players": []}
    other_search = client.get(
        f"/api/match/player_search/{other_base}", {"search": "jansen"}
    )
    assert other_search.json() == {"players": []}


def test_guest_add_rejects_outsiders_and_stale_revisions(client: Client) -> None:
    """Unauthorized or stale writes create no player and keep the revision."""
    tracker = create_tracker_match(prefix="Guest guard")
    create_group_types("Reserve")
    expected_revision = tracker.match_data.live_revision

    client.force_login(create_tracker_user(username="guest-outsider"))
    forbidden = _add_guest(client, tracker, "Niet Toegestaan")
    assert forbidden.status_code == HTTPStatus.FORBIDDEN

    login_home_club_editor(client, tracker, "guest-guard-editor")
    stale = _add_guest(
        client, tracker, "Te Laat", expected_revision=expected_revision + 5
    )
    assert stale.status_code == HTTPStatus.CONFLICT
    assert stale.json()["code"] == "revision_conflict"

    assert not Player.all_objects.filter(name__in=["Niet Toegestaan", "Te Laat"])
    assert not MatchGuestPlayer.objects.exists()
    tracker.match_data.refresh_from_db()
    assert tracker.match_data.live_revision == expected_revision


@pytest.mark.parametrize(
    ("overrides", "error"),
    [
        pytest.param({"name": " x "}, "at least 2", id="short-name"),
        pytest.param({"name": "x" * 81}, "at most 80", id="long-name"),
        pytest.param({"team": "away-other"}, "Invalid player group context", id="team"),
    ],
)
def test_guest_add_validates_name_and_team(
    client: Client, overrides: dict[str, str], error: str
) -> None:
    """Names are bounded and the team must play in this match."""
    tracker = create_tracker_match(prefix="Guest validation")
    create_group_types("Reserve")
    login_home_club_editor(client, tracker, "guest-validation-editor")
    url = None
    if overrides.pop("team", None):
        unrelated = create_tracker_match(prefix="Guest unrelated").home_team
        url = _guest_url(tracker, unrelated.pk)

    response = _add_guest(
        client, tracker, overrides.get("name", "Geldige Naam"), url=url
    )

    assert response.status_code == HTTPStatus.BAD_REQUEST
    assert error in response.json()["error"]
    assert not MatchGuestPlayer.objects.exists()


def test_guest_add_respects_full_reserve(client: Client) -> None:
    """A full reserve cannot receive a guest."""
    tracker = create_tracker_match(prefix="Guest full")
    create_group_types("Reserve")
    reserve = get_tracker_group(tracker, "Reserve")
    reserve.players.add(*[
        create_tracker_player(username=f"guest-full-{index}") for index in range(16)
    ])
    login_home_club_editor(client, tracker, "guest-full-editor")

    response = _add_guest(client, tracker, "Zeventiende Speler")

    assert response.status_code == HTTPStatus.BAD_REQUEST
    assert_api_error(response.json(), {"error": "Too many players selected"})
    assert not MatchGuestPlayer.objects.exists()
