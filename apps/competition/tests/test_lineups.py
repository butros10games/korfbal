"""Synthetic verified v8 match selections, privacy and native roster isolation."""

from datetime import timedelta
from unittest.mock import Mock

from django.utils import timezone
import pytest
from rest_framework.test import APIClient

from apps.competition.adapters.outbound.sportlink import SportlinkClient
from apps.competition.models import Match, MatchMembership, SyncResource
from apps.competition.services.importer import Importer
from apps.competition.services.lineups import queue_lineups
from apps.competition.services.publishing import publish_catalogue
from apps.competition.services.season_repair import repair
from apps.competition.services.seasons import INDOOR
from apps.competition.tests.test_importer import match_payload
from apps.competition.tests.test_logos import BUCKET
from apps.competition.tests.test_rosters import person
from apps.game_tracker.models import MatchPlayer, StartingPlayerAssignment
from apps.player.models import Player
from apps.schedule.models import Season


def lineup() -> dict:
    """Names and identifiers are invented; flags follow the captured schema."""
    return {
        **match_payload(),
        "AllowsBasePlayers": True,
        "HomeFormationView": False,
        "AwayFormationView": False,
        "HomeTeamPerson": [
            {**person("P1"), "BasePlayer": True},
            {**person("P2"), "BasePlayer": False},
        ],
        "AwayTeamPerson": [{**person("P3"), "BasePlayer": True}],
    }


@pytest.mark.django_db
def test_lineups_reuse_people_without_roster_or_appearance(season: Season) -> None:
    """A match substitute belongs in selection, never regular roster or minutes."""
    importer = Importer(season, timezone.now())
    importer.match(match_payload(), result=True)
    publish_catalogue()
    importer.apply("team_roster", "T1", {"TeamPersonOverview": [person("P1")]})
    payload = lineup()
    payload["AwayTeamPerson"].append({
        **person("S1"),
        "BasePlayer": False,
        "TeamPersonFunction": {"RoleId": "COACHING_STAFF"},
    })
    for _ in range(2):
        importer.apply("match_lineup", "M1", payload)
    assert MatchMembership.objects.count() == len({"P1", "P2", "P3", "S1"})
    match = Match.objects.select_related(
        "home_team__local_team_data", "away_team__local_team_data"
    ).get()
    home = match.home_team.local_team_data
    assert list(home.players.values_list("knkv_person_id", flat=True)) == ["P1"]
    assert not MatchPlayer.objects.exists()
    assert not StartingPlayerAssignment.objects.exists()
    response = APIClient().get(
        f"/api/team/teams/{home.team_id}/overview/?season={season.pk}"
    )
    rows = {p["id_uuid"]: p for p in response.data["roster"]}
    sub = rows[str(Player.objects.get(knkv_person_id="P2").pk)]
    assert sub["role_labels"] == ["Wisselspeler (wedstrijd)"]
    assert sub["roster_role"] == "reserve"
    assert not response.data["stats"]["players"]
    away = match.away_team.local_team_data
    response = APIClient().get(
        f"/api/team/teams/{away.team_id}/overview/?season={season.pk}"
    )
    assert [p["id_uuid"] for p in response.data["staff"]] == [
        str(Player.objects.get(knkv_person_id="S1").pk)
    ]


@pytest.mark.django_db
def test_b_class_is_unknown_and_empty_clears_selection(season: Season) -> None:
    """B-class BasePlayer flags do not establish starters, even when all true."""
    importer = Importer(season, timezone.now())
    importer.match(match_payload(), result=True)
    payload = lineup()
    payload["AllowsBasePlayers"] = False
    importer.apply("match_lineup", "M1", payload)
    assert set(MatchMembership.objects.values_list("role", flat=True)) == {"selected"}
    payload.update(HomeTeamPerson=[], AwayTeamPerson=[])
    importer.apply("match_lineup", "M1", payload)
    assert not MatchMembership.objects.exists()


@pytest.mark.django_db
def test_private_match_person_erases_roster_and_selection(season: Season) -> None:
    """Newer privacy withdrawal clears both source observations atomically."""
    now = timezone.now()
    importer = Importer(season, now)
    importer.match(match_payload(), result=True)
    publish_catalogue()
    importer.apply("team_roster", "T1", {"TeamPersonOverview": [person("P1")]})
    importer.apply("match_lineup", "M1", lineup())
    private = lineup()
    private["HomeTeamPerson"][0]["PrivacyLevel"] = "PRIVATE"
    Importer(season, now + timedelta(minutes=1)).apply("match_lineup", "M1", private)
    player = Player.all_objects.get(knkv_person_id="P1")
    assert not player.name
    assert not player.knkv_memberships.exists()
    assert not MatchMembership.objects.filter(player=player).exists()
    importer.apply("match_lineup", "M1", lineup())
    assert not Player.objects.filter(pk=player.pk).exists()


@pytest.mark.django_db
@pytest.mark.parametrize(
    "change", ["wrong_team", "wrong_match", "missing_side", "bad_flag", "both_sides"]
)
def test_invalid_lineup_preserves_snapshot(season: Season, change: str) -> None:
    """Malformed successful responses never wipe an existing selection."""
    importer = Importer(season, timezone.now())
    importer.match(match_payload(), result=True)
    importer.apply("match_lineup", "M1", lineup())
    payload = lineup()
    if change == "wrong_team":
        payload["HomeTeam"]["PublicTeamId"] = "OTHER"
    elif change == "wrong_match":
        payload["PublicMatchId"] = "OTHER"
    elif change == "missing_side":
        del payload["AwayTeamPerson"]
    elif change == "bad_flag":
        payload["HomeTeamPerson"][0]["BasePlayer"] = None
    else:
        payload["AwayTeamPerson"] = payload["HomeTeamPerson"]
    previous = list(MatchMembership.objects.values())
    with pytest.raises((ValueError, TypeError)):
        importer.apply("match_lineup", "M1", payload)
    assert list(MatchMembership.objects.values()) == previous


@pytest.mark.django_db
def test_queue_and_verified_v8_request(season: Season) -> None:
    """Both sides share a request; enqueue never resets exhausted feed retries."""
    Importer(season, timezone.now()).match(match_payload(), result=True)
    assert queue_lineups(season) == 1
    resource = SyncResource.objects.get(kind="match_lineup")
    retry_cap = 6
    resource.failures = retry_cap
    resource.etag = '"old"'
    resource.save()
    assert queue_lineups(season) == 0
    resource.refresh_from_db()
    assert resource.failures == retry_cap
    client = SportlinkClient("synthetic", user_agent="synthetic")
    response = Mock(status_code=200, headers={})
    response.json.return_value = lineup()
    client.session.get = Mock(return_value=response)
    client.fetch(resource)
    kwargs = client.session.get.call_args.kwargs
    assert kwargs["params"] == {"PublicMatchId": "M1", "v": "8"}
    assert kwargs["headers"] == {"X-Navajo-Version": "8"}


@pytest.mark.django_db
def test_empty_newer_lineup_cannot_be_replaced_by_old_response(season: Season) -> None:
    """An empty selection still advances the observation checkpoint."""
    now = timezone.now()
    importer = Importer(season, now)
    importer.match(match_payload(), result=True)
    empty = lineup()
    empty.update(HomeTeamPerson=[], AwayTeamPerson=[])
    Importer(season, now + timedelta(minutes=1)).apply("match_lineup", "M1", empty)
    importer.apply("match_lineup", "M1", lineup())
    assert not MatchMembership.objects.exists()


@pytest.mark.django_db
def test_lineup_season_isolation_and_photo_reuse(season: Season) -> None:
    """Indoor selections follow the native match season and use the photo queue."""
    importer = Importer(season, timezone.now())
    payload = match_payload()
    payload["HomeTeam"]["SportId"] = INDOOR
    payload["AwayTeam"]["SportId"] = INDOOR
    importer.match(payload, result=True)
    publish_catalogue()
    repair(season, season.start_date.year)
    data = lineup()
    data["HomeTeamPerson"][0]["Photo"] = {"Bucket": BUCKET, "Hash": "A" * 32}
    importer.apply("match_lineup", "M1", data)
    fixture = Match.objects.select_related("home_team__local_team_data").get()
    team_data = fixture.home_team.local_team_data
    client = APIClient()
    url = f"/api/team/teams/{team_data.team_id}/overview/"
    assert not client.get(url, {"season": str(season.pk)}).data["roster"]
    indoor = client.get(url, {"season": str(team_data.season_id)}).data["roster"]
    assert len(indoor) == len(data["HomeTeamPerson"])
    player = Player.objects.get(knkv_person_id="P1")
    assert SyncResource.objects.filter(
        kind="player_photo", source_id=str(player.pk)
    ).exists()
    importer.apply(
        "team_roster", "T1", {"TeamPersonOverview": [person("P1", privacy="PRIVATE")]}
    )
    assert not MatchMembership.objects.filter(player=player).exists()
