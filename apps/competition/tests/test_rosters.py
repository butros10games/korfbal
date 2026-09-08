"""Privacy, membership intervals, discovery and local roster reads."""

from datetime import timedelta
from http import HTTPStatus
from unittest.mock import Mock, patch

from django.contrib.auth import get_user_model
from django.utils import timezone
import pytest
from rest_framework.test import APIClient

from apps.club.models import Club as LocalClub
from apps.competition.adapters.outbound.sportlink import SportlinkClient
from apps.competition.models import RosterMembership, SyncResource
from apps.competition.services.importer import Importer
from apps.competition.services.rosters import queue_rosters
from apps.competition.tests.test_importer import team_payload
from apps.player.models import Player
from apps.schedule.models import Season
from apps.team.models import TeamData
from apps.team.models.team import Team as LocalTeam


def person(identifier: str = "P1", privacy: str = "NORMAL") -> dict:
    """Fabricated person; unexpected personal fields must never be retained."""
    return {
        "PersonId": identifier,
        "PrivacyLevel": privacy,
        "TeamPerson": True,
        "TeamPersonFunction": {"RoleId": "PLAYER_DEFAULT"},
        "FirstName": "Example",
        "Infix": "van",
        "LastName": "Player",
        "Gender": "IGNORED",
        "Photo": {"Hash": "ignored"},
        "ShirtNumber": 7,
    }


@pytest.mark.django_db
def test_membership_intervals_and_private_erasure(season: Season) -> None:
    """Absence retires, return rejoins, and private identity history is erased."""
    now = timezone.now()
    importer = Importer(season, now)
    importer.team(team_payload("T1"))
    importer.team(team_payload("T2"))
    payload = {"TeamPersonOverview": [person()]}
    for source in ("T1", "T2", "T1"):
        importer.apply("team_roster", source, payload)
    assert Player.objects.count() == 1
    expected_teams = 2
    assert RosterMembership.objects.count() == expected_teams
    importer.apply("team_roster", "T1", {"TeamPersonOverview": []})
    assert RosterMembership.objects.filter(ended_at=None).count() == 1
    Importer(season, now + timedelta(hours=1)).apply("team_roster", "T1", payload)
    expected_intervals = 3
    assert RosterMembership.objects.count() == expected_intervals
    Importer(season, now + timedelta(hours=2)).apply(
        "team_roster", "T2", {"TeamPersonOverview": [person(privacy="PRIVATE")]}
    )
    assert not Player.objects.exists()
    assert not RosterMembership.objects.exists()


@pytest.mark.django_db
@pytest.mark.parametrize("privacy", ["NORMAL", "LIMITED", "OPEN"])
def test_only_visible_actual_team_players(season: Season, privacy: str) -> None:
    """Staff, guests and hidden/unknown privacy entries do not become players."""
    importer = Importer(season, timezone.now())
    importer.team(team_payload("T1"))
    rows = [person(privacy=privacy), person("P2", "PRIVATE"), person("P3", "UNKNOWN")]
    staff = person("P4")
    staff["TeamPersonFunction"] = {"RoleId": "COACHING_STAFF"}
    guest = person("P5")
    guest["TeamPerson"] = False
    importer.apply("team_roster", "T1", {"TeamPersonOverview": [*rows, staff, guest]})
    assert list(Player.objects.values_list("knkv_person_id", flat=True)) == ["P1"]
    assert Player.objects.get().display_name == "Example van Player"
    assert not get_user_model().objects.exists()


@pytest.mark.django_db
def test_malformed_response_does_not_retire_roster(season: Season) -> None:
    """Malformed envelopes cannot partially apply privacy edits or retirement."""
    importer = Importer(season, timezone.now())
    importer.team(team_payload("T1"))
    importer.apply("team_roster", "T1", {"TeamPersonOverview": [person()]})
    for payload in (
        {},
        {"TeamPersonOverview": None},
        {"TeamPersonOverview": [person(privacy="PRIVATE"), {}]},
    ):
        with pytest.raises(ValueError, match="Invalid roster"):
            importer.apply("team_roster", "T1", payload)
        assert Player.objects.count() == 1
        assert RosterMembership.objects.get().ended_at is None


@pytest.mark.django_db
def test_queue_is_explicit_idempotent_and_preserves_checkpoints(season: Season) -> None:
    """Queue one collection per source team, with no per-player request fanout."""
    importer = Importer(season, timezone.now())
    importer.team(team_payload("T1"))
    assert not SyncResource.objects.filter(kind="team_roster").exists()
    assert queue_rosters(season) == 1
    feed = SyncResource.objects.get(kind="team_roster")
    feed.failures = 2
    feed.fetched_at = timezone.now()
    feed.save()
    assert queue_rosters(season) == 0
    feed.refresh_from_db()
    expected_failures = 2
    assert feed.failures == expected_failures
    assert feed.fetched_at is not None


@pytest.mark.django_db
def test_native_profile_without_account_and_privacy_expiry(season: Season) -> None:
    """Ordinary player endpoints serve visible imports and hide withdrawn identities."""
    importer = Importer(season, timezone.now())
    importer.team(team_payload("T1"))
    importer.apply("team_roster", "T1", {"TeamPersonOverview": [person()]})
    player = Player.objects.get()
    url = f"/api/player/players/{player.pk}/"
    client = APIClient()
    response = client.get(url)
    assert response.status_code == HTTPStatus.OK
    assert response.data["display_name"] == "Example van Player"
    assert response.data["user"] is None
    assert "knkv_person_id" not in response.data
    Player.all_objects.filter(pk=player.pk).update(
        knkv_observed_at=timezone.now() - timedelta(days=9)
    )
    assert client.get(url).status_code == HTTPStatus.NOT_FOUND
    assert Player.all_objects.get(pk=player.pk).display_name == "Afgeschermd"


@pytest.mark.django_db
def test_combined_team_roster_and_expiry(season: Season) -> None:
    """Indoor and outdoor IDs share one player row on the native team page."""
    now = timezone.now()
    importer = Importer(season, now)
    first = importer.team(team_payload("T1"))
    second = importer.team(team_payload("T2"))
    local = LocalTeam.objects.create(
        name="Example 1", club=LocalClub.objects.create(name="Example")
    )
    group = first.group
    group.local_team = local
    group.local_team_data = TeamData.objects.create(team=local, season=season)
    group.save()
    second.group = group
    second.save()
    for source in ("T1", "T2"):
        importer.apply("team_roster", source, {"TeamPersonOverview": [person()]})
    assert group.local_team_data.players.count() == 1
    client = APIClient()
    response = client.get(
        f"/api/team/teams/{local.pk}/overview/?season={season.pk}&include_stats=false"
    )
    assert response.status_code == HTTPStatus.OK
    assert response.data["roster"][0]["id_uuid"] == str(Player.objects.get().pk)
    assert response.data["roster"][0]["display_name"] == "Example van Player"
    assert "imported_roster" not in response.data
    Player.all_objects.update(knkv_observed_at=now - timedelta(days=9))
    assert group.local_team_data.players.count() == 0


@pytest.mark.django_db
def test_roster_transport_uses_verified_version_and_fresh_body(season: Season) -> None:
    """Refresh one complete version-zero roster without person-by-person calls."""
    client = SportlinkClient("synthetic", user_agent="synthetic")
    resource = SyncResource(
        season=season, kind="team_roster", source_id="T1", etag='"old"'
    )
    response = Mock(status_code=HTTPStatus.OK, headers={})
    response.json.return_value = {"TeamPersonOverview": []}
    with patch.object(client.session, "get", return_value=response) as get:
        client.fetch(resource)
    assert get.call_count == 1
    assert get.call_args.args[0].endswith("/team/TeamPersons")
    assert get.call_args.kwargs["params"] == {"v": "0", "PublicTeamId": "T1"}
    assert get.call_args.kwargs["headers"] == {"X-Navajo-Version": "0"}
    client.close()


@pytest.mark.django_db
def test_native_roster_retirement_keeps_other_variant(season: Season) -> None:
    """Removing one source variant must not remove a player still in another."""
    importer = Importer(season, timezone.now())
    first = importer.team(team_payload("T1"))
    second = importer.team(team_payload("T2"))
    local = LocalTeam.objects.create(
        name="Example", club=LocalClub.objects.create(name="Example")
    )
    group = first.group
    group.local_team = local
    group.local_team_data = TeamData.objects.create(team=local, season=season)
    group.save()
    second.group = group
    second.save()
    for source in ("T1", "T2"):
        importer.apply("team_roster", source, {"TeamPersonOverview": [person()]})
    importer.apply("team_roster", "T1", {"TeamPersonOverview": []})
    assert group.local_team_data.players.count() == 1
    importer.apply("team_roster", "T2", {"TeamPersonOverview": []})
    assert group.local_team_data.players.count() == 0
