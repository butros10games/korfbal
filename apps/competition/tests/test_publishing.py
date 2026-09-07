"""Imported data uses native admin, discovery, teams, seasons and match endpoints."""

from copy import deepcopy
from datetime import timedelta
from uuid import uuid4

from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction
from django.urls import reverse
from django.utils import timezone
import pytest
from rest_framework import status
from rest_framework.test import APIClient

from apps.club.models import Club as AppClub
from apps.competition.models import Club, Match, SyncLease, Team, TeamGroup
from apps.competition.services.importer import Importer
from apps.competition.services.publishing import publish_catalogue
from apps.competition.tests.test_importer import match_payload
from apps.game_tracker.models import MatchData, Shot
from apps.game_tracker.services.tracker_state import get_tracker_state
from apps.schedule.models import (
    Match as AppMatch,
    Season,
    SeasonPool,
)
from apps.team.models import (
    Team as AppTeam,
    TeamData,
)
from apps.team.tests.team_test_support import build_team_context


MATCH_SIDES = 2
SOURCE_VARIANTS = 3


@pytest.mark.django_db
def test_imported_records_use_native_admin_search_and_detail_pages(
    season: Season,
) -> None:
    """Expose imported identities through the existing application endpoints."""
    Importer(season, timezone.now()).apply(
        "club_results", "C", {"MatchResult": [match_payload()]}
    )
    first = publish_catalogue()
    assert first["counts"]["clubs_created"] == MATCH_SIDES
    assert first["counts"]["teams_created"] == MATCH_SIDES
    source = Match.objects.get()
    match = AppMatch.objects.select_related(
        "home_team__club", "away_team__club", "season"
    ).get()
    tracker = MatchData.objects.get(match_link=match)
    assert source.local_match_id == match.pk
    assert tracker.score_source == "knkv"
    assert tracker.status == "finished"
    assert (tracker.home_score, tracker.away_score) == (0, 10)
    assert not Shot.objects.exists()
    assert TeamData.objects.count() == MATCH_SIDES
    assert (
        TeamGroup.objects.filter(local_team_data__isnull=False).count() == MATCH_SIDES
    )
    assert SeasonPool.objects.get().teams.count() == MATCH_SIDES
    assert get_tracker_state(match, team=match.away_team)["score"] == {
        "for": 10,
        "against": 0,
    }
    client = APIClient()
    client.force_authenticate(
        get_user_model().objects.create_user(username="native-search")
    )
    clubs = client.get("/api/club/clubs/?search=Club").data["results"]
    assert {row["id_uuid"] for row in clubs} == {
        str(pk) for pk in AppClub.objects.values_list("pk", flat=True)
    }
    response = client.get(f"/api/club/clubs/{match.home_team.club_id}/overview/")
    assert response.status_code == status.HTTP_200_OK
    assert response.data["matches"]["recent"][0]["score"] == {"home": 0, "away": 10}
    response = client.get(f"/api/team/teams/{match.home_team_id}/overview/")
    assert response.status_code == status.HTTP_200_OK
    assert response.data["team"]["id_uuid"] == str(match.home_team_id)
    assert client.get(f"/api/matches/{match.pk}/summary/").data["score"] == {
        "home": 0,
        "away": 10,
    }
    publish_catalogue()
    assert (
        AppClub.objects.count(),
        AppTeam.objects.count(),
        AppMatch.objects.count(),
        TeamData.objects.count(),
    ) == (2, 2, 1, 2)
    client.force_login(
        get_user_model().objects.create_superuser(username="native-admin")
    )
    assert b"Club T1" in client.get(reverse("admin:club_club_changelist")).content


@pytest.mark.django_db
def test_global_team_shared_across_sports_and_season_rosters(season: Season) -> None:
    """Provider variants map to one global team and one roster for each season."""
    payload = match_payload()["HomeTeam"]
    payload["TeamName"] = "Club T1 J1"
    importer = Importer(season, timezone.now())
    importer.team(payload)
    indoor = {**payload, "PublicTeamId": "indoor", "SportId": "KORFBALL-ZA-WK"}
    importer.team(indoor)
    later = Season.objects.create(
        name="later",
        start_date=season.start_date + timedelta(days=365),
        end_date=season.end_date + timedelta(days=365),
    )
    Importer(later, timezone.now()).team(payload)
    publish_catalogue()
    assert AppTeam.objects.count() == 1
    assert AppTeam.objects.get().name == "J1"
    assert TeamData.objects.count() == MATCH_SIDES
    assert Team.objects.count() == SOURCE_VARIANTS
    assert set(TeamGroup.objects.values_list("local_team_id", flat=True)) == {
        AppTeam.objects.get().pk
    }
    with pytest.raises(IntegrityError), transaction.atomic():
        TeamData.objects.create(team=AppTeam.objects.get(), season=season)


@pytest.mark.django_db
def test_existing_global_identity_and_roster_are_preserved() -> None:
    """Link to an existing native team without replacing its members or settings."""
    context = build_team_context(suffix="publish")
    payload = match_payload()["HomeTeam"]
    payload["Club"] = {"ClubId": "C", "ClubName": context.club.name}
    payload["TeamName"] = f"{context.club.name} {context.team.name}"
    Importer(context.season, timezone.now()).team(payload)
    publish_catalogue()
    assert TeamGroup.objects.get().local_team_data_id == context.team_data.pk
    assert AppTeam.objects.count() == 1
    assert list(context.team_data.players.all()) == [context.player]
    assert list(context.team_data.coach.all()) == [context.coach]


@pytest.mark.django_db
def test_results_refresh_without_overwriting_tracker_activity(season: Season) -> None:
    """Retain native tracker authority after a command."""
    data = match_payload()
    Importer(season, timezone.now()).apply("club_results", "C", {"MatchResult": [data]})
    publish_catalogue()
    changed = deepcopy(data)
    changed["HomeResult"]["Score"] = 12
    Importer(season, timezone.now()).apply(
        "club_results", "C", {"MatchResult": [changed]}
    )
    publish_catalogue()
    tracker = MatchData.objects.get()
    assert tracker.home_score == changed["HomeResult"]["Score"]
    local_score = 15
    tracker.home_score = local_score
    tracker.live_revision = 1
    tracker.save(update_fields=("home_score", "live_revision"))
    changed["HomeResult"]["Score"] = 20
    Importer(season, timezone.now()).apply(
        "club_results", "C", {"MatchResult": [changed]}
    )
    publish_catalogue()
    tracker.refresh_from_db()
    assert tracker.home_score == local_score


@pytest.mark.django_db
def test_same_name_provider_clubs_require_review(season: Season) -> None:
    """Never combine clubs from different towns using a shared label."""
    del season
    Club.objects.create(external_id="A", name="Example", city="Town A")
    Club.objects.create(external_id="B", name="Example", city="Town B")
    report = publish_catalogue()
    assert not AppClub.objects.exists()
    assert len(report["blocked"]) == MATCH_SIDES


@pytest.mark.django_db
def test_publishing_respects_the_active_import_lease() -> None:
    """Do not publish a partially written catalogue owned by another worker."""
    SyncLease.objects.create(
        key="sportlink", owner=uuid4(), expires_at=timezone.now() + timedelta(minutes=1)
    )
    with pytest.raises(ValueError, match="running"):
        publish_catalogue()


@pytest.mark.django_db
def test_untouched_native_fixture_receives_official_result(season: Season) -> None:
    """An already scheduled match receives its result without changing its identity."""
    Importer(season, timezone.now()).apply(
        "club_results", "C", {"MatchResult": [match_payload()]}
    )
    source = Match.objects.select_related("home_team__club", "away_team__club").get()
    sides = []
    for team in (source.home_team, source.away_team):
        club = AppClub.objects.create(name=team.club.name)
        sides.append(AppTeam.objects.create(club=club, name=team.name))
    native = AppMatch.objects.create(
        season=season,
        home_team=sides[0],
        away_team=sides[1],
        start_time=source.starts_at,
    )
    publish_catalogue()
    source.refresh_from_db()
    assert source.local_match_id == native.pk
    assert not source.local_created
    native.refresh_from_db()
    assert native.tracker_data.score_source == "knkv"
    assert native.tracker_data.status == "finished"


@pytest.mark.django_db
def test_manual_score_edit_is_retained_without_a_tracker_revision(
    season: Season,
) -> None:
    """Admin changes cannot be silently overwritten by later provider observations."""
    data = match_payload()
    Importer(season, timezone.now()).apply("club_results", "C", {"MatchResult": [data]})
    publish_catalogue()
    local_score = 15
    MatchData.objects.update(home_score=local_score)
    data["HomeResult"]["Score"] = 20
    Importer(season, timezone.now()).apply("club_results", "C", {"MatchResult": [data]})
    report = publish_catalogue()
    assert MatchData.objects.get().home_score == local_score
    assert report["blocked"][0]["reason"] == "local_score_changed"


@pytest.mark.django_db
def test_pool_editor_retains_imported_sport_when_older_clients_omit_it(
    season: Season,
) -> None:
    """Keep the native pool editor compatible with newly imported sport identities."""
    Importer(season, timezone.now()).apply(
        "club_results", "C", {"MatchResult": [match_payload()]}
    )
    publish_catalogue()
    pool = SeasonPool.objects.get()
    client = APIClient()
    client.force_authenticate(
        get_user_model().objects.create_user(username="pool-editor", is_staff=True)
    )
    response = client.patch(
        f"/api/seasons/pools/{pool.pk}/",
        {
            "name": pool.name,
            "season_id": str(season.pk),
            "team_ids": [str(pk) for pk in pool.teams.values_list("pk", flat=True)],
        },
        format="json",
    )
    assert response.status_code == status.HTTP_200_OK
    pool.refresh_from_db()
    assert pool.sport == "KORFBALL-VE-WK"
