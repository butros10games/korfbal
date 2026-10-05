"""Allowlisted provider context preserves richness without changing identities."""

from copy import deepcopy
from datetime import timedelta

from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
import pytest

from apps.club.models import Club as NativeClub
from apps.competition.domain.source_context import (
    ContextKind,
    context_value,
    merge_duplicate_context,
    merge_source_context,
)
from apps.competition.models import Club, Pool, Team
from apps.competition.services.importer import Importer
from apps.competition.tests.test_importer import team_payload
from apps.schedule.models import Season
from apps.team.models import Team as NativeTeam


def test_context_retains_rich_fields_and_rejects_older_overwrite() -> None:
    """No provider Gender enum or native classification identity is invented."""
    now = timezone.now()
    data = {
        "Gender": "Observed gender",
        "TeamCode": "J1",
        "Class": [
            {
                "ClassId": "provider-class",
                "ClassName": "Public class",
                "Extra": "ignored",
            }
        ],
        "SportDescription": "Veld Week",
        "SportTag": "VE",
        "SortOrder": 0,
        "LocalTeam": False,
        "BirthDate": "private-unused",
    }
    before = deepcopy(data)
    context = merge_source_context("team", {}, data, now, "club_teams")
    thin = merge_source_context(
        "team",
        context,
        {"Gender": "", "Class": []},
        now + timedelta(days=1),
        "club_results",
    )
    older = merge_source_context(
        "team", thin, {"Gender": "older gender"}, now - timedelta(days=1), "historical"
    )
    assert older == context
    assert context_value(context, "Gender") == "Observed gender"
    assert context_value(context, "Class") == [
        {"id": "provider-class", "name": "Public class"}
    ]
    assert context_value(context, "LocalTeam") is False
    assert context_value(context, "SortOrder") == 0
    assert "BirthDate" not in context["fields"]
    assert data == before
    repeated = merge_source_context(
        "team", context, data, now + timedelta(days=1), "club_teams"
    )
    assert context_value(repeated, "Gender") == context_value(context, "Gender")
    assert (
        repeated["fields"]["Gender"]["observed_at"]
        == (now + timedelta(days=1)).isoformat()
    )


def test_equal_newer_context_fences_delayed_changed_value() -> None:
    """The observation watermark advances even when the observed value is equal."""
    now = timezone.now()
    initial = merge_source_context("team", {}, {"Gender": "A"}, now, "club_teams")
    newer = merge_source_context(
        "team", initial, {"Gender": "A"}, now + timedelta(seconds=2), "club_results"
    )
    delayed = merge_source_context(
        "team", newer, {"Gender": "B"}, now + timedelta(seconds=1), "club_results"
    )
    assert delayed == newer
    assert context_value(delayed, "Gender") == "A"
    assert (
        delayed["fields"]["Gender"]["observed_at"]
        == (now + timedelta(seconds=2)).isoformat()
    )


@pytest.mark.parametrize(
    ("kind", "field", "value"),
    [
        ("team", "Gender", 1),
        ("team", "LocalTeam", 1),
        ("team", "SortOrder", True),
        ("team", "SortOrder", float("inf")),
        ("team", "Class", [{"ClassId": 1}]),
        ("pool", "CompetitionKind", {}),
        ("match", "RoundNr", True),
        ("match", "ExternalMatchId", "123"),
    ],
)
def test_context_rejects_unverified_types(
    kind: ContextKind, field: str, value: object
) -> None:
    """Malformed context cannot become silently accepted source evidence."""
    with pytest.raises(ValueError, match="Invalid source context"):
        merge_source_context(kind, {}, {field: value}, timezone.now(), "summary")


def test_duplicate_context_enrichment_is_order_independent_and_copied() -> None:
    """Fixture deduplication cannot discard the richer copy's context."""
    thin = {"PublicTeamId": "T1", "Class": [{"ClassId": "C1", "ClassName": None}]}
    rich = {
        "PublicTeamId": "T1",
        "Gender": "raw",
        "Class": [{"ClassId": "C1", "ClassName": "Class"}],
    }
    before = deepcopy(thin)
    assert merge_duplicate_context("team", thin, rich) == merge_duplicate_context(
        "team", rich, thin
    )
    assert thin == before
    with pytest.raises(ValueError, match="Conflicting duplicate"):
        merge_duplicate_context("team", rich, {"Gender": "different"})


@pytest.mark.django_db
def test_repeated_team_and_pool_cache_enrich_source_context(season: Season) -> None:
    """The first abbreviated row does not freeze the cached source context."""
    now = timezone.now()
    importer = Importer(season, now)
    first = team_payload("T1")
    row = importer.team(first)
    repeated = importer.team({
        **first,
        "Gender": "raw",
        "SortOrder": 0,
        "LocalTeam": False,
    })
    assert repeated.pk == row.pk
    assert context_value(Team.objects.get(pk=row.pk).source_context, "Gender") == "raw"
    pool = importer.pool({"PoolId": "P1", "PoolName": "Pool"})
    repeated_pool = importer.pool({
        "PoolId": "P1",
        "CompetitionKind": "raw-kind",
        "SortOrder": 0,
    })
    assert repeated_pool.pk == pool.pk
    assert context_value(Pool.objects.get(pk=pool.pk).source_context, "SortOrder") == 0
    with CaptureQueriesContext(connection) as queries:
        importer.team({**first, "Gender": "raw", "SortOrder": 0, "LocalTeam": False})
    assert not [query for query in queries if query["sql"].split()[0] == "UPDATE"]


@pytest.mark.django_db
def test_group_club_conflict_rejects_before_catalogue_mutation(season: Season) -> None:
    """A source identity cannot silently move a reviewed seasonal group."""
    first = team_payload("T1")
    importer = Importer(season, timezone.now())
    team = importer.team(first)
    altered = deepcopy(first)
    altered["Club"] = {"ClubId": "other", "ClubName": "Other"}
    with pytest.raises(ValueError, match="club identity"):
        importer.team(altered)
    assert not Club.objects.filter(external_id="other").exists()
    team.refresh_from_db()
    assert team.club.external_id == "CT1"


@pytest.mark.django_db
def test_joint_native_registration_does_not_trigger_source_group_guard(
    season: Season,
) -> None:
    """The guard compares source clubs, leaving native joint ownership intact."""
    payload = team_payload("T1")
    source = Importer(season, timezone.now()).team(payload)
    assert source.group is not None
    joint_club = NativeClub.objects.create(name="Joint registration")
    native = NativeTeam.objects.create(club=joint_club, name="Joint 1")
    source.group.local_team = native
    source.group.save(update_fields=("local_team",))
    assert (
        Importer(season, timezone.now()).team(payload).group.local_team_id == native.pk
    )


@pytest.mark.django_db
def test_current_club_teams_cannot_write_historical_variants(season: Season) -> None:
    """ClubTeams has no proven historical edition selector."""
    with pytest.raises(ValueError, match="Current ClubTeams"):
        Importer(season, timezone.now(), discover=False).apply(
            "club_teams", "CT1", {"ClubTeam": [team_payload("T1")]}
        )
    assert not Team.objects.exists()
