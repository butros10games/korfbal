"""Reconcile existing history conservatively without changing application records."""

from copy import deepcopy
from datetime import timedelta
from io import StringIO
import json
from pathlib import Path
from typing import Any
from uuid import uuid4

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import CommandError
from django.utils import timezone
import pytest
from rest_framework import status
from rest_framework.test import APIClient

from apps.club.models.club import Club as LocalClub
from apps.competition.models import (
    Club,
    Match,
    Pool,
    PoolEntry,
    SyncLease,
    Team,
    TeamGroup,
)
from apps.competition.services.importer import Importer
from apps.competition.services.publishing import publish_catalogue
from apps.competition.services.reconciliation import (
    JointTeamIndex,
    joint_team_matches,
    reconcile,
)
from apps.competition.tests.test_importer import match_payload
from apps.game_tracker.models import MatchData
from apps.schedule.models import (
    Match as LocalMatch,
    Season,
    SeasonPool,
)
from apps.team.models.team import Team as LocalTeam


@pytest.fixture
def graph(season: Season) -> dict[str, Any]:
    """Create a source game and an already recorded local counterpart."""
    row = match_payload()
    row["HomeTeam"].update(
        TeamName="DTS Enkhuizen 1",
        Club={"ClubId": "DTS", "ClubName": "DTS Enkhuizen", "City": "Enkhuizen"},
    )
    row["AwayTeam"].update(
        TeamName="Example 2", Club={"ClubId": "EX", "ClubName": "Example"}
    )
    Importer(season, timezone.now()).apply(
        "club_results", "DTS", {"MatchResult": [row]}
    )
    home_club = LocalClub.objects.create(name="DTS")
    away_club = LocalClub.objects.create(name="Example")
    home = LocalTeam.objects.create(club=home_club, name="1")
    away = LocalTeam.objects.create(club=away_club, name="2")
    pool = SeasonPool.objects.create(season=season, name="A-1")
    pool.teams.add(home, away)
    source = Match.objects.get()
    for team in Team.objects.all():
        PoolEntry.objects.get_or_create(pool=source.pool, team=team)
    local = LocalMatch.objects.create(
        season=season,
        home_team=home,
        away_team=away,
        pool=pool,
        start_time=source.starts_at,
    )
    tracker, _ = MatchData.objects.update_or_create(
        match_link=local,
        defaults={"status": "finished", "home_score": 17, "away_score": 14},
    )
    return {
        "payload": row,
        "source": source,
        "local": local,
        "tracker": tracker,
        "club": home_club,
        "home": home,
        "away": away,
        "pool": pool,
        "overrides": {
            ("club", Club.objects.get(external_id="DTS").pk): str(home_club.pk)
        },
    }


@pytest.mark.django_db
def test_preview_then_idempotent_apply_preserves_recorded_history(
    graph: dict[str, Any], season: Season
) -> None:
    """DTS aliases unlock team, poule and match links without altering local data."""
    preview = reconcile(overrides=graph["overrides"])
    assert preview["counts"].get("explicit") == 1
    assert not Match.objects.filter(local_match__isnull=False).exists()
    assert not Club.objects.filter(local_club__isnull=False).exists()
    before = list(MatchData.objects.values())
    applied = reconcile(apply=True, overrides=graph["overrides"])
    assert applied["unlinked_local"] == {
        kind: [] for kind in ("club", "team", "pool", "match")
    }
    assert Match.objects.get().local_match == graph["local"]
    assert Pool.objects.get().local_pool == graph["pool"]
    assert TeamGroup.objects.get(name="DTS Enkhuizen 1").local_team == graph["home"]
    assert LocalClub.objects.get(pk=graph["club"].pk).name == "DTS"
    assert list(MatchData.objects.values()) == before
    Importer(season, timezone.now()).apply(
        "club_results", "DTS", {"MatchResult": [graph["payload"]]}
    )
    assert Match.objects.get().local_match == graph["local"]
    again = reconcile(apply=True)
    assert set(again["counts"]) == {"linked"}
    assert list(MatchData.objects.values()) == before


@pytest.mark.django_db
def test_duplicate_local_matches_are_reported_and_can_be_selected(
    graph: dict[str, Any],
) -> None:
    """Two recorded games with identical fixture metadata require explicit selection."""
    local = graph["local"]
    duplicate = LocalMatch.objects.create(
        season=local.season,
        home_team=local.home_team,
        away_team=local.away_team,
        start_time=local.start_time,
    )
    result = reconcile(apply=True, overrides=graph["overrides"])
    decision = next(row for row in result["decisions"] if row["kind"] == "match")
    assert decision["reason"] == "ambiguous_or_claimed"
    assert set(decision["candidates"]) == {str(local.pk), str(duplicate.pk)}
    assert Match.objects.get().local_match is None
    reconcile(apply=True, overrides={("match", graph["source"].pk): str(local.pk)})
    assert Match.objects.get().local_match == local
    assert LocalMatch.objects.filter(pk=duplicate.pk).exists()


@pytest.mark.django_db
def test_rescheduled_and_reversed_matches_are_not_guessed(
    graph: dict[str, Any],
) -> None:
    """Time differences need a reviewed mapping; reversing home/away is rejected."""
    local = graph["local"]
    local.start_time += timedelta(hours=1)
    local.save(update_fields=("start_time",))
    reconcile(apply=True, overrides=graph["overrides"])
    assert Match.objects.get().local_match is None
    reversed_match = LocalMatch.objects.create(
        season=local.season,
        home_team=local.away_team,
        away_team=local.home_team,
        start_time=local.start_time,
    )
    with pytest.raises(ValueError, match="Invalid match mapping"):
        reconcile(
            apply=True,
            overrides={("match", graph["source"].pk): str(reversed_match.pk)},
        )
    reconcile(apply=True, overrides={("match", graph["source"].pk): str(local.pk)})
    assert Match.objects.get().local_match == local


@pytest.mark.django_db
def test_duplicate_source_matches_cannot_claim_one_record(
    graph: dict[str, Any], season: Season
) -> None:
    """Source ambiguity never picks the first row or merges recorded matches."""
    row = deepcopy(graph["payload"])
    row["PublicMatchId"] = "another-source"
    Importer(season, timezone.now()).apply(
        "club_results", "DTS", {"MatchResult": [row]}
    )
    reconcile(apply=True, overrides=graph["overrides"])
    assert not Match.objects.filter(local_match__isnull=False).exists()


@pytest.mark.django_db
def test_invalid_mapping_rolls_back_all_links(graph: dict[str, Any]) -> None:
    """An invalid team parent rejects the entire batch including its club links."""
    group = TeamGroup.objects.get(name="DTS Enkhuizen 1")
    with pytest.raises(ValueError, match="Invalid team mapping"):
        reconcile(
            apply=True,
            overrides={**graph["overrides"], ("team", group.pk): str(graph["away"].pk)},
        )
    assert not Club.objects.filter(local_club__isnull=False).exists()


@pytest.mark.django_db
def test_unavailable_historical_matches_remain_visible_in_report(
    graph: dict[str, Any],
) -> None:
    """No provider counterpart is reported honestly instead of losing local history."""
    local = graph["local"]
    old_season = Season.objects.create(
        name="old",
        start_date=local.season.start_date - timedelta(days=365),
        end_date=local.season.end_date - timedelta(days=365),
    )
    historical = LocalMatch.objects.create(
        season=old_season,
        home_team=local.home_team,
        away_team=local.away_team,
        start_time=local.start_time - timedelta(days=365),
    )
    result = reconcile(overrides=graph["overrides"])
    assert [row["id"] for row in result["unlinked_local"]["match"]] == [
        str(historical.pk)
    ]


@pytest.mark.django_db
def test_command_and_existing_uuid_filters(
    graph: dict[str, Any], tmp_path: Path
) -> None:
    """Operators can preview/select aliases and clients can query by existing UUID."""
    path = tmp_path / "links.json"
    path.write_text(
        json.dumps([
            {"kind": kind, "source_id": source, "local_id": target}
            for (kind, source), target in graph["overrides"].items()
        ])
    )
    output = StringIO()
    call_command("reconcile_competition", links_file=path, stdout=output)
    assert json.loads(output.getvalue())["applied"] is False
    call_command(
        "reconcile_competition", links_file=path, apply=True, stdout=StringIO()
    )
    client = APIClient()
    client.force_authenticate(get_user_model().objects.create_user(username="linked"))
    for query in (
        f"local_club={graph['club'].pk}",
        f"local_team={graph['home'].pk}",
        f"local_match={graph['local'].pk}",
    ):
        response = client.get(f"/api/competition/matches/?{query}")
        assert response.data["count"] == 1
        assert response.data["results"][0]["local_match"] == graph["local"].pk
    assert (
        client.get("/api/competition/matches/?local_team=invalid").status_code
        == status.HTTP_400_BAD_REQUEST
    )
    path.write_text("{}")
    with pytest.raises(CommandError, match="JSON array"):
        call_command("reconcile_competition", links_file=path)


@pytest.mark.django_db
def test_active_import_blocks_apply_but_allows_read_only_preview(
    graph: dict[str, Any],
) -> None:
    """Do not link against a catalogue while its source batch is being imported."""
    SyncLease.objects.create(
        key="sportlink", owner=uuid4(), expires_at=timezone.now() + timedelta(minutes=1)
    )
    assert reconcile(overrides=graph["overrides"])["applied"] is False
    with pytest.raises(ValueError, match="An import is running"):
        reconcile(apply=True, overrides=graph["overrides"])
    assert not Club.objects.filter(local_club__isnull=False).exists()


@pytest.mark.django_db
def test_same_local_team_can_link_across_imported_seasons(
    graph: dict[str, Any], season: Season
) -> None:
    """Stable local teams retain distinct season-specific catalogue groups."""
    row = deepcopy(graph["payload"])
    old_season = Season.objects.create(
        name="earlier-import",
        start_date=season.start_date - timedelta(days=365),
        end_date=season.end_date - timedelta(days=365),
    )
    Importer(old_season, timezone.now()).apply(
        "club_teams", "DTS", {"ClubTeam": [row["HomeTeam"], row["AwayTeam"]]}
    )
    reconcile(apply=True, overrides=graph["overrides"])
    assert set(
        TeamGroup.objects.filter(local_team=graph["home"]).values_list(
            "season_id", flat=True
        )
    ) == {season.pk, old_season.pk}


@pytest.mark.django_db
def test_duplicate_source_club_names_require_selection(graph: dict[str, Any]) -> None:
    """Two KNKV clubs with the same label must not take an arbitrary local identity."""
    Club.objects.create(external_id="OTHER-EX", name="Example", city="Elsewhere")
    result = reconcile(apply=True, overrides=graph["overrides"])
    ambiguous = [
        row
        for row in result["decisions"]
        if row["kind"] == "club" and row["source_name"] == "Example"
    ]
    assert all(row["reason"] == "ambiguous_or_claimed" for row in ambiguous)
    assert not Club.objects.filter(name="Example", local_club__isnull=False).exists()
    assert Match.objects.get().local_match is None


@pytest.mark.django_db
@pytest.mark.parametrize("source_name", ["Example/Partner 2", "Partner/Example 2"])
def test_joint_team_links_without_reassigning_clubs(
    graph: dict[str, Any], source_name: str
) -> None:
    """A partner-registered team can belong to an existing local joint club."""
    joint = LocalClub.objects.create(name="Example/Partner")
    local = graph["away"]
    local.club = joint
    local.save(update_fields=("club",))
    source_group = TeamGroup.objects.get(name="Example 2")
    source_group.name = source_name
    source_group.save(update_fields=("name",))
    reconcile(apply=True, overrides=graph["overrides"])
    source_group.refresh_from_db()
    assert source_group.local_team == local
    assert Match.objects.get().local_match == graph["local"]
    assert source_group.club.name == "Example"
    local.refresh_from_db()
    assert local.club == joint
    assert set(reconcile(apply=True)["counts"]) == {"linked"}
    client = APIClient()
    client.force_authenticate(get_user_model().objects.create_user(username="joint"))
    for endpoint in ("team-groups", "matches"):
        response = client.get(f"/api/competition/{endpoint}/?local_club={joint.pk}")
        assert response.status_code == status.HTTP_200_OK
        assert response.data["count"] == 1


@pytest.mark.parametrize(
    ("source", "club", "local_club", "team", "expected"),
    [
        ("Helios/Stormvogels (U) 3", "Helios", "Helios/Stormvogels (U)", "3", True),
        ("Helios/Stormvogels (U) J3", "Helios", "Helios/Stormvogels (U)", "3", False),
        ("Helios/Stormvogels (U) 3", "Other", "Helios/Stormvogels (U)", "3", False),
        ("Helios/Stormvogels (U) 3", "Helios", "Helios/Stormvogels (L)", "3", False),
        ("Helios 3", "Helios", "Helios/Stormvogels (U)", "3", False),
    ],
)
def test_joint_team_boundaries(
    source: str, club: str, local_club: str, team: str, expected: bool
) -> None:
    """Shared labels cannot bypass team number, age or partner-club boundaries."""
    assert joint_team_matches(source, club, local_club, team) is expected
    index = JointTeamIndex()
    index.add("local", local_club, team)
    assert index.matches(source, club) == ({"local"} if expected else set())


@pytest.mark.parametrize(
    "source",
    [
        " Alpha / Beta Town  J3 ",
        "Beta Town/Alpha reserves 2",
        "Alpha/Beta Town 3",
        "Alpha 3",
        "Alpha//Beta Town 3",
        "Alpha/Beta Town Other 3",
    ],
)
@pytest.mark.parametrize("club", ["Alpha", "Beta Town", "Other"])
def test_joint_index_preserves_all_legacy_candidates(source: str, club: str) -> None:
    """Index lookup preserves ambiguous candidates and multiword designations."""
    teams = [
        ("Alpha/Beta Town", "J3"),
        ("Beta Town/Alpha", "J3"),
        ("Alpha/Beta Town", "reserves 2"),
        ("Alpha/Beta Town", "3"),
        ("Alpha/Beta Town Other", "3"),
        ("Alpha/", "3"),
        ("Alpha", "3"),
    ]
    index = JointTeamIndex()
    for identifier, (partners, name) in enumerate(teams):
        index.add(str(identifier), partners, name)
    expected = {
        str(identifier)
        for identifier, (partners, name) in enumerate(teams)
        if joint_team_matches(source, club, partners, name)
    }
    assert index.matches(source, club) == expected


@pytest.mark.django_db
def test_publication_applies_reviewed_overrides_in_one_pass(
    graph: dict[str, Any],
) -> None:
    """Publishing can reuse reviewed aliases without a separate reconciliation."""
    owner = uuid4()
    SyncLease.objects.create(
        key="sportlink", owner=owner, expires_at=timezone.now() + timedelta(minutes=1)
    )
    result = publish_catalogue(lease_owner=owner, overrides=graph["overrides"])
    assert result["links"]["explicit"] == 1
    assert not result["blocked"]
    assert Match.objects.get().local_match == graph["local"]
    assert set(LocalClub.objects.values_list("name", flat=True)) == {"DTS", "Example"}
    assert LocalMatch.objects.count() == 1
