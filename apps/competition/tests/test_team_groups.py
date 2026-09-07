"""Unified application teams retain separate competition identities."""

from copy import deepcopy
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.utils import timezone
import pytest
from rest_framework import status
from rest_framework.test import APIClient

from apps.competition.models import Match, Team, TeamGroup
from apps.competition.services.importer import Importer
from apps.competition.tests.test_importer import match_payload, team_payload
from apps.schedule.models import Season


@pytest.mark.django_db
def test_group_variants_and_query_matches(season: Season) -> None:
    """Combine equal club/team names while retaining sport-specific matches and IDs."""
    outdoor = match_payload()
    indoor = deepcopy(outdoor)
    indoor["PublicMatchId"] = "INDOOR-MATCH"
    for side in ("HomeTeam", "AwayTeam"):
        indoor[side]["PublicTeamId"] += "-ZA"
        indoor[side]["SportId"] = "KORFBALL-ZA-WK"
        indoor[side]["TeamName"] = "  " + indoor[side]["TeamName"].upper() + "  "
    importer = Importer(season, timezone.now())
    importer.apply("club_results", "CT1", {"MatchResult": [outdoor, indoor]})
    assert Team.objects.count() == len({"T1", "T2", "T3", "T4"})
    assert TeamGroup.objects.count() == len({"indoor", "outdoor"})
    group = Team.objects.get(external_id="T1").group
    assert group is not None
    client = APIClient()
    client.force_authenticate(get_user_model().objects.create_user(username="groups"))
    response = client.get(f"/api/competition/team-groups/{group.pk}/")
    assert len(response.data["variants"]) == len({"indoor", "outdoor"})
    matches = client.get(f"/api/competition/matches/?team_group={group.pk}")
    assert matches.data["count"] == len({"indoor", "outdoor"})
    assert (
        client.get("/api/competition/matches/?team_group=bad").status_code
        == status.HTTP_400_BAD_REQUEST
    )
    assert Match.objects.count() == len({"indoor", "outdoor"})
    importer.apply("club_results", "CT1", {"MatchResult": [outdoor]})
    assert Team.objects.get(external_id="T1").group == group


@pytest.mark.django_db
def test_distinct_numbers_clubs_and_seasons_stay_separate(season: Season) -> None:
    """Exact matching never guesses across a different club, number or season."""
    first = team_payload("T1")
    other_club = team_payload("T2")
    other_club["TeamName"] = first["TeamName"]
    other_number = deepcopy(first)
    other_number.update(PublicTeamId="T3", TeamName="Example T2")
    Importer(season, timezone.now()).apply(
        "club_teams", "CT1", {"ClubTeam": [first, other_club, other_number]}
    )
    next_season = Season.objects.create(
        name="later",
        start_date=season.start_date + timedelta(days=365),
        end_date=season.end_date + timedelta(days=365),
    )
    Importer(next_season, timezone.now()).apply(
        "club_teams", "CT1", {"ClubTeam": [first]}
    )
    assert TeamGroup.objects.count() == len({"T1", "T2", "T3", "T4"})
