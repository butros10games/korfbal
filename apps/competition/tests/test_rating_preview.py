"""Regression coverage for allocation baselines and exact competition replay."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
import json
from pathlib import Path
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.management import CommandError, call_command
import pytest
from rest_framework import status
from rest_framework.test import APIClient

from apps.competition.domain.elo import INITIAL_RATING, K_FACTOR, RatedResult, calculate
from apps.competition.models import (
    Allocation,
    AllocationSource,
    CompetitionEdition,
    Match,
    Pool,
    RatingConfiguration,
    Team,
)
from apps.competition.services.allocations import import_allocations
from apps.competition.services.importer import Importer
from apps.competition.services.published_ratings import (
    configure_ratings,
    disable_ratings,
)
from apps.competition.services.rating_preview import PreviewParameters, preview_ratings
from apps.competition.services.ratings import team_ratings
from apps.competition.tests.test_importer import match_payload
from apps.schedule.models import Season


START = datetime(2026, 9, 1, tzinfo=UTC)
END = datetime(2026, 9, 8, tzinfo=UTC)
TEAM_COUNT = 2
CLASS_LEVEL = 2
PARAMETERS = PreviewParameters(START, END, 20, 1.2)


def test_seeded_update_preserves_units_and_counts() -> None:
    """Raw KNKV zero is a valid baseline; upset updates are zero-sum."""
    match = RatedResult("one", END, 1, 2, 10, 5)
    scores = calculate(
        {1: "B", 2: "B"}, [match], initial={1: 0, 2: 20}, scale=20, k_factor=1.2
    )
    assert scores[1].value == pytest.approx(1.2 * 10 / 11)
    assert sum(row.value for row in scores.values()) == pytest.approx(20)
    assert scores[1].games == scores[2].games == 1
    assert scores == calculate(
        {1: "B", 2: "B"}, [match], initial={1: 0, 2: 20}, scale=20, k_factor=1.2
    )


@pytest.mark.parametrize(
    "parameters",
    [
        {"scale": 0},
        {"scale": float("nan")},
        {"k_factor": -1},
        {"initial": {1: float("inf")}},
        {"initial": {2: 50}},
    ],
)
def test_invalid_parameters_are_rejected(parameters: dict) -> None:
    """Never emit nonfinite JSON or ignore an out-of-population baseline."""
    with pytest.raises(ValueError, match="finite"):
        calculate({1: "B"}, [], **parameters)


def create_baseline(season: Season) -> AllocationSource:
    """Create an exact mapped poule with two independently seeded teams."""
    row = match_payload()
    row["HomeTeam"]["TeamName"] = "Example J1"
    row["AwayTeam"]["TeamName"] = "Other J2"
    Importer(season, END).apply("club_results", "CT1", {"MatchResult": [row]})
    match = Match.objects.get()
    pool = match.pool
    assert pool is not None
    pool.name = "Ge4-001"
    pool.class_name = ""
    pool.sport = "KORFBALL-VE-BK"
    pool.save()
    for team in (match.home_team, match.away_team):
        team.sport = "KORFBALL-VE-BK"
        team.save()
    match.starts_at = START + timedelta(days=1)
    match.home_score, match.away_score = 10, 5
    match.save()
    csv = (
        b";Poule/Team;Dg;Lftd;Punt;Plaats;;"
        b";Poule/Team;Dg;Lftd;Punt;Plaats;indeling dd;03-09-26\n"
        b"B-categorie geel (4-tallen);;;;;;;;;;;;;;\n"
        b";Ge4-001;;;;;;;;;;;;;\n"
        b"1;Example J1;Za;10,2;40;Teststad;;;;;;;;;\n"
        b"2;Other J2;Za;10,4;60;Teststad;;;;;;;;;\n"
    )
    import_allocations(csv, season, apply=True, label="Synthetic", gender="mixed")
    return AllocationSource.objects.get()


@pytest.fixture
def baseline(season: Season) -> AllocationSource:
    """Reuse the allocation builder across preview and match-prediction tests."""
    return create_baseline(season)


@pytest.mark.django_db
def test_replay_is_read_only_and_corrections_replace_results(
    season: Season,
    baseline: AllocationSource,
) -> None:
    """Include matches before publication using the explicit pre-season baseline."""
    first = preview_ratings(season, [baseline.pk], PARAMETERS)
    assert first["used_results"] == 1
    assert len(first["results"]) == TEAM_COUNT
    assert first["results"][0]["change"] > 0
    assert first == preview_ratings(season, [baseline.pk], PARAMETERS)
    Match.objects.update(home_score=0, away_score=10)
    second = preview_ratings(season, [baseline.pk], PARAMETERS)
    assert second["results"][0]["change"] < 0
    assert second["results"][0]["games"] == 1
    assert list(
        Allocation.objects.order_by("pk").values_list("knkv_points", flat=True)
    ) == [Decimal(40), Decimal(60)]


@pytest.mark.django_db
@pytest.mark.parametrize(
    "change", ["awarded", "outside_window", "other_pool", "missing_points", "conflict"]
)
def test_unusable_result_never_contaminates_baseline(
    season: Season,
    baseline: AllocationSource,
    change: str,
) -> None:
    """Guard temporal, allocation, source-quality and classification boundaries."""
    if change == "awarded":
        Match.objects.update(automatic_result=True)
    elif change == "outside_window":
        Match.objects.update(starts_at=START - timedelta(seconds=1))
    elif change == "other_pool":
        pool = Pool.objects.create(season=season, external_id="other", name="Ge4-002")
        Match.objects.update(pool=pool)
    elif change == "missing_points":
        Allocation.objects.filter(team_name="Example J1").update(knkv_points=None)
    else:
        Pool.objects.update(mapping_status="conflict")
    report = preview_ratings(season, [baseline.pk], PARAMETERS)
    assert report["used_results"] == 0
    assert all(row["change"] == 0 for row in report["results"])


@pytest.mark.django_db
def test_duplicate_snapshot_requires_explicit_selection(
    season: Season,
    baseline: AllocationSource,
) -> None:
    """Do not silently reseed after a new publication or choose by import order."""
    second = AllocationSource.objects.create(
        season=season, digest="second", label="second", published_on=END.date()
    )
    original = Allocation.objects.first()
    assert original is not None
    original.pk = None
    original.source = second
    original.save()
    with pytest.raises(ValueError, match="Multiple baselines"):
        preview_ratings(season, [baseline.pk, second.pk], PARAMETERS)


@pytest.mark.django_db
def test_a_class_uses_neutral_seed_without_knkv_points(
    season: Season,
    baseline: AllocationSource,
) -> None:
    """Official ordinal levels accompany A Elo but are not invented score gaps."""
    allocation = Allocation.objects.first()
    assert allocation is not None
    context = allocation.competition_class
    assert context is not None
    context.category, context.code, context.level = "a", "hoofdklasse", CLASS_LEVEL
    context.save()
    Allocation.objects.update(knkv_points=None)
    report = preview_ratings(season, [baseline.pk], PARAMETERS)
    assert report["used_results"] == 1
    assert report["results"][0]["baseline"] == INITIAL_RATING
    assert report["results"][0]["rating"] == pytest.approx(
        INITIAL_RATING + K_FACTOR / 2
    )
    assert report["results"][0]["class_level"] == CLASS_LEVEL


@pytest.mark.django_db
@pytest.mark.parametrize(
    "invalid", ["missing_source", "naive", "reversed", "outside_season"]
)
def test_invalid_preview_scope_is_rejected(
    season: Season,
    baseline: AllocationSource,
    invalid: str,
) -> None:
    """Source and temporal scope cannot silently fall back to the whole catalogue."""
    source_ids = [baseline.pk]
    parameters = PARAMETERS
    if invalid == "missing_source":
        source_ids.append(baseline.pk + 1)
    elif invalid == "naive":
        parameters = PreviewParameters(START.replace(tzinfo=None), END, 20, 1.2)
    elif invalid == "reversed":
        parameters = PreviewParameters(END, START, 20, 1.2)
    else:
        parameters = PreviewParameters(START.replace(year=2025), END, 20, 1.2)
    with pytest.raises(ValueError, match=r"Require|Select"):
        preview_ratings(season, source_ids, parameters)


@pytest.mark.django_db
def test_class_contexts_do_not_share_ratings(
    season: Season,
    baseline: AllocationSource,
) -> None:
    """A cross-gender fixture cannot connect otherwise identically named classes."""
    allocation = Allocation.objects.get(team_name="Other J2")
    context = allocation.competition_class
    assert context is not None
    edition = context.edition
    context.pk = None
    context.edition = CompetitionEdition.objects.create(
        season=season,
        discipline=edition.discipline,
        phase=edition.phase,
        gender="women",
    )
    context.save()
    allocation.competition_class = context
    allocation.save()
    entry = allocation.entry
    assert entry is not None
    pool = Pool.objects.create(
        season=season,
        external_id="women",
        name="Ge4-001",
        mapping_status="mapped",
        competition_class=context,
    )
    entry.pool = pool
    entry.save()
    report = preview_ratings(season, [baseline.pk], PARAMETERS)
    assert report["used_results"] == 0
    assert len({row["comparison_group"] for row in report["results"]}) == TEAM_COUNT


@pytest.mark.django_db
def test_publication_switches_existing_api_and_is_idempotent(
    season: Season,
    baseline: AllocationSource,
) -> None:
    """The existing endpoint serves seeded rows only after explicit activation."""
    assert not RatingConfiguration.objects.exists()
    report = configure_ratings(season, [baseline.pk], PARAMETERS, apply=False)
    assert report["changed"]
    assert not RatingConfiguration.objects.exists()
    assert team_ratings(season.pk)["model"] == "elo-v1"
    configure_ratings(season, [baseline.pk], PARAMETERS, apply=True)
    configuration = RatingConfiguration.objects.get()
    assert not configure_ratings(season, [baseline.pk], PARAMETERS, apply=True)[
        "changed"
    ]
    assert RatingConfiguration.objects.get().updated_at == configuration.updated_at
    result = team_ratings(season.pk)
    assert result["model"] == "knkv-seeded-elo-v1"
    row = next(row for row in result["results"] if row["external_id"] == "T1")
    assert row["original_knkv_points"] == "40.00"
    assert row["rating"] == pytest.approx(40 + 1.2 * 10 / 11, abs=0.0001)
    assert row["id"] == Match.objects.get().home_team_id
    assert result == team_ratings(season.pk)
    client = APIClient()
    client.force_authenticate(get_user_model().objects.create_user(username="seeded"))
    response = client.get(f"/api/competition/ratings/?season={season.pk}&page_size=1")
    assert response.status_code == status.HTTP_200_OK
    assert response.data["model"] == result["model"]
    assert response.data["results"] == result["results"][:1]
    assert response.data["metadata"]["sources"][0]["id"] == baseline.pk
    assert response.data["next"]
    club_response = client.get(
        f"/api/competition/ratings/?season={season.pk}&club={row['club_id']}"
    )
    assert club_response.data["results"] == [row]


@pytest.mark.django_db
@pytest.mark.parametrize(
    "change", ["correction", "awarded", "mapping", "baseline", "parameters", "rename"]
)
def test_published_cache_observes_changed_inputs(
    season: Season,
    baseline: AllocationSource,
    change: str,
) -> None:
    """No stale seeded scores after changes, even updates bypassing save timestamps."""
    configure_ratings(season, [baseline.pk], PARAMETERS, apply=True)
    first = team_ratings(season.pk)
    if change == "correction":
        Match.objects.update(home_score=0, away_score=10)
    elif change == "awarded":
        Match.objects.update(automatic_result=True)
    elif change == "mapping":
        Pool.objects.update(mapping_status="conflict")
    elif change == "baseline":
        Allocation.objects.filter(team_name="Example J1").update(knkv_points=0)
    elif change == "parameters":
        configure_ratings(
            season, [baseline.pk], PreviewParameters(START, END, 20, 2.4), apply=True
        )
    else:
        Team.objects.filter(external_id="T1").update(name="Renamed")
    second = team_ratings(season.pk)
    assert first["results"] != second["results"]
    assert all(row["games"] <= 1 for row in second["results"])
    if change == "mapping":
        assert second["results"] == []
        assert second["metadata"]["excluded"] == {"unresolved_context": TEAM_COUNT}
    if change == "awarded":
        assert all(row["change"] == 0 for row in second["results"])


@pytest.mark.django_db
def test_result_freshness_does_not_rebuild_seeded_cache(
    season: Season,
    baseline: AllocationSource,
) -> None:
    """Provider polling alone must not trigger a season recalculation."""
    configure_ratings(season, [baseline.pk], PARAMETERS, apply=True)
    first = team_ratings(season.pk)
    Match.objects.update(results_checked_at=END, result_observed_at=END)
    assert team_ratings(season.pk) == first


@pytest.mark.django_db
def test_new_results_and_elapsed_fixtures_rebuild_from_baseline(
    season: Season,
    baseline: AllocationSource,
) -> None:
    """A previously future final fixture becomes eligible when its start passes."""
    configure_ratings(season, [baseline.pk], PARAMETERS, apply=True)
    match = Match.objects.get()
    match.pk = None
    match.external_id = "new-result"
    match.starts_at = END + timedelta(hours=1)
    match.local_match = None
    match.save()
    with patch(
        "apps.competition.services.published_ratings.timezone.now", return_value=END
    ):
        first = team_ratings(season.pk)
    with patch(
        "apps.competition.services.published_ratings.timezone.now",
        return_value=END + timedelta(hours=2),
    ):
        second = team_ratings(season.pk)
    assert all(row["games"] == 1 for row in first["results"])
    assert all(row["games"] == TEAM_COUNT for row in second["results"])


@pytest.mark.django_db
def test_disable_restores_legacy_without_removing_baselines(
    season: Season,
    baseline: AllocationSource,
) -> None:
    """Activation is reversible, and failed reconfiguration leaves it intact."""
    configure_ratings(season, [baseline.pk], PARAMETERS, apply=True)
    with pytest.raises(ValueError, match="Select existing"):
        configure_ratings(season, [baseline.pk + 1], PARAMETERS, apply=True)
    assert RatingConfiguration.objects.get().active
    disable_ratings(season, apply=False)
    assert team_ratings(season.pk)["model"] == "knkv-seeded-elo-v1"
    disable_ratings(season, apply=True)
    assert team_ratings(season.pk)["model"] == "elo-v1"
    assert Allocation.objects.count() == TEAM_COUNT


@pytest.mark.django_db
def test_publication_command_and_output_failure_are_atomic(
    season: Season,
    baseline: AllocationSource,
    tmp_path: Path,
) -> None:
    """A failed report write must not leave a seemingly failed activation committed."""
    args = [
        "--season",
        season.name,
        "--source",
        str(baseline.pk),
        "--effective-at",
        START.isoformat(),
        "--b-scale",
        "20",
        "--b-k-factor",
        "1.2",
    ]
    with pytest.raises(CommandError, match="Require"):
        call_command("publish_allocation_ratings", "--season", season.name, "--apply")
    with pytest.raises(CommandError, match="No such file"):
        call_command(
            "publish_allocation_ratings",
            *args,
            "--apply",
            "--output",
            str(tmp_path / "missing" / "report.json"),
        )
    assert not RatingConfiguration.objects.exists()
    output = tmp_path / "report.json"
    call_command("publish_allocation_ratings", *args, "--output", str(output))
    assert not RatingConfiguration.objects.exists()
    call_command(
        "publish_allocation_ratings", *args, "--apply", "--output", str(output)
    )
    assert json.loads(output.read_text())["applied"]
    assert team_ratings(season.pk)["model"] == "knkv-seeded-elo-v1"
