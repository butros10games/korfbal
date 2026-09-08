"""Regression coverage for allocation baselines and exact competition replay."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from apps.competition.domain.elo import INITIAL_RATING, K_FACTOR, RatedResult, calculate
from apps.competition.models import (
    Allocation,
    AllocationSource,
    CompetitionEdition,
    Match,
    Pool,
)
from apps.competition.services.allocations import import_allocations
from apps.competition.services.importer import Importer
from apps.competition.services.rating_preview import PreviewParameters, preview_ratings
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


@pytest.fixture
def baseline(season: Season) -> AllocationSource:
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
