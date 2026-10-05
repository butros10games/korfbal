"""Official classes and youth designations must not invent ages or strength."""

from dataclasses import asdict, replace
from io import StringIO
import json

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.utils import timezone
import pytest
from rest_framework import status
from rest_framework.test import APIClient

from apps.competition.domain.classification import (
    classify,
    designation,
    hierarchy,
    ladder_context,
    level,
)
from apps.competition.models import (
    CompetitionClass,
    CompetitionEdition,
    Pool,
    PoolEntry,
    Team,
)
from apps.competition.services import classification
from apps.competition.services.classification import (
    map_pool,
    plan_pool,
    relevel_classes,
    resolve_class,
)
from apps.competition.services.importer import Importer
from apps.competition.tests.test_importer import match_payload
from apps.schedule.models import Season


@pytest.mark.parametrize(
    ("name", "year", "kind", "age"),
    [
        ("Example J1", 2026, "j", None),
        ("Example J20", 2026, "j", None),
        ("Example U19-1", 2026, "u", "U19"),
        ("Example A1", 2024, "historical", "A"),
        ("Example A1", 2026, "unknown", None),
    ],
)
def test_designations(name: str, year: int, kind: str, age: str | None) -> None:
    """J numbers are local order, historical labels remain season-specific."""
    result = designation(name, year)
    assert result["kind"] == kind
    assert result["age_group"] == age
    assert result["average_age"] is None


def test_unknown_and_parallel_pool_codes() -> None:
    """Class mappings require complete context and never parse a pool number."""
    value, issues = classify("Hoofdklasse", "KORFBALL-VE-WK", 2026)
    assert value.code == "hoofdklasse"
    assert level(value, issues) is None
    assert classify("Hoofdklasse 06", "KORFBALL-VE-WK", 2026)[0].code == "unknown"
    assert classify("wissel", "", 2026)[0].code == "unknown"


def test_season_removed_class_and_context() -> None:
    """Youth second classes cannot be mapped into the new mixed A-category."""
    value, issues = classify(
        "gemengd U19 2e klasse", "KORFBALL-VE-WK", 2026, {"phase": "autumn"}
    )
    assert "class_removed_for_season" in issues
    assert level(value, issues) is None
    assert (
        "class_removed_for_season"
        not in classify("gemengd U19 2e klasse", "KORFBALL-VE-WK", 2025)[1]
    )


@pytest.mark.django_db
def test_mapping_rerun_and_source_change(season: Season) -> None:
    """Reviewed overrides survive imports but become conflicts when evidence changes."""
    pool = Pool.objects.create(
        season=season,
        external_id="P1",
        name="06",
        class_name="Hoofdklasse",
        sport="KORFBALL-VE-WK",
    )
    pool.mapping_override = {
        "values": {
            "gender": "mixed",
            "phase": "autumn",
            "age_group": "senior",
            "team_kind": "standard",
        },
        "reason": "Reviewed official worksheet",
        "source": plan_pool(pool)["evidence"],
    }
    pool.save()
    assert map_pool(pool)["status"] == "mapped"
    assert map_pool(pool)["changed"] is False
    assert CompetitionEdition.objects.count() == 1
    hoofdklasse_level = 2
    assert CompetitionClass.objects.get().level == hoofdklasse_level
    Importer(season, timezone.now()).pool({"PoolId": "P1", "ClassName": "1e klasse"})
    pool.refresh_from_db()
    assert pool.mapping_status == "conflict"
    assert pool.mapping_override["reason"] == "Reviewed official worksheet"
    assert pool.competition_class_id is None


@pytest.mark.django_db
def test_dry_run_and_api_filters(season: Season) -> None:
    """Read-only audit and authenticated paginated class filters share one source."""
    pool = Pool.objects.create(
        season=season,
        external_id="P1",
        name="06",
        class_name="Rood",
        sport="KORFBALL-VE-BK",
    )
    output = StringIO()
    call_command("map_competition", season=season.name, stdout=output)
    assert json.loads(output.getvalue())["pools"] == 1
    assert CompetitionClass.objects.count() == 0
    map_pool(pool)
    client = APIClient()
    assert client.get("/api/competition/pools/").status_code in {401, 403}
    client.force_authenticate(get_user_model().objects.create_user(username="mapping"))
    response = client.get(
        "/api/competition/pools/",
        {"category": "b", "season": str(season.pk), "page_size": 1},
    )
    assert response.status_code == status.HTTP_200_OK
    assert response.data["results"][0]["classification"]["context"]["colour"] == "red"
    assert client.get("/api/competition/pools/", {"category": "a"}).data["count"] == 0
    assert (
        client.get("/api/competition/pools/", {"gender": "invalid"}).status_code
        == status.HTTP_400_BAD_REQUEST
    )


@pytest.mark.parametrize(
    ("label", "code", "age", "gender", "playing_format"),
    [
        ("B4-geel dames nj", "youth_colour", "youth", "women", "four"),
        ("B-rood nj ", "youth_colour", "youth", "unknown", "eight"),
        ("Reserve 3e klasse nj", "class_3", "senior", "unknown", "eight"),
        ("U19 hoofdklasse dames", "hoofdklasse", "U19", "women", "eight"),
    ],
)
def test_observed_sportlink_labels(
    label: str, code: str, age: str, gender: str, playing_format: str
) -> None:
    """Observed source abbreviations retain phase and explicit dames context."""
    value, _ = classify(label, "KORFBALL-VE-WK", 2026)
    assert (value.code, value.age_group, value.gender, value.playing_format) == (
        code,
        age,
        gender,
        playing_format,
    )


def test_period_context_conflicts_and_level_eligibility() -> None:
    """A resolved period fills missing context; a label conflict remains explicit."""
    label = "gemengd senioren Hoofdklasse"
    value, issues = classify(
        label,
        "KORFBALL-VE-WK",
        2026,
        {"team_kind": "standard"},
        context={"phase": "autumn"},
    )
    assert value.phase == "autumn"
    assert "missing_phase" not in issues
    expected_level = 2
    assert level(value, issues, 2026) == expected_level
    no_phase, missing = classify(
        label, "KORFBALL-VE-WK", 2026, {"team_kind": "standard"}
    )
    assert "missing_phase" in missing
    assert level(no_phase, missing, 2026) == expected_level
    conflict, conflict_issues = classify(
        label + " nj",
        "KORFBALL-VE-WK",
        2026,
        {"team_kind": "standard"},
        context={"phase": "spring"},
    )
    assert "conflicting_period" in conflict_issues
    assert level(conflict, conflict_issues, 2026) is None


def test_verified_ladders_separate_lanes_and_preserve_historical_identity() -> None:
    """Reserve/standard lanes differ, and the current handbook never ranks A-F."""
    standard, standard_issues = classify(
        "gemengd senioren hoofdklasse",
        "KORFBALL-VE-WK",
        2026,
        {"team_kind": "standard"},
    )
    reserve, _ = classify("gemengd reserve hoofdklasse", "KORFBALL-VE-WK", 2026)
    standard_ladder, reserve_ladder = (
        hierarchy(standard, 2026),
        hierarchy(reserve, 2026),
    )
    assert standard_ladder is not None
    assert reserve_ladder is not None
    assert standard_ladder.ladder_id != reserve_ladder.ladder_id
    assert "2026" not in standard_ladder.ladder_id

    later_rule = replace(standard_ladder, first_edition=2027, last_edition=2027)
    assert later_rule.ladder_id == standard_ladder.ladder_id
    assert later_rule.hierarchy_revision == standard_ladder.hierarchy_revision
    assert (
        replace(standard_ladder, codes=standard_ladder.codes[1:]).hierarchy_revision
        != standard_ladder.hierarchy_revision
    )
    assert (
        ladder_context(standard, standard_issues, 2025)["level_reason"]
        == "ladder_unverified"
    )
    historic, issues = classify("gemengd A-jeugd hoofdklasse", "KORFBALL-VE-WK", 2018)
    assert (historic.code, historic.age_group) == ("hoofdklasse", "A")
    assert ladder_context(historic, issues, 2018)["level_reason"] == "ladder_unverified"
    colour, issues = classify("B4-geel nj", "KORFBALL-VE-WK", 2026)
    assert (
        ladder_context(colour, issues, 2026)["level_reason"] == "no_ladder_b_category"
    )


def test_womens_u17_current_handbook_does_not_allow_first_class() -> None:
    """The verified 2026-27 handbook lists women's U17 Hoofdklasse only."""
    value, issues = classify("dames U17 1e klasse", "KORFBALL-VE-WK", 2026)
    assert "class_not_in_hierarchy" in issues
    assert level(value, issues, 2026) is None
    value, issues = classify("dames U19 1e klasse", "KORFBALL-VE-WK", 2026)
    assert ladder_context(value, issues, 2026)["level_reason"] == "ranked"


@pytest.mark.django_db
def test_pool_period_is_classification_context(season: Season) -> None:
    """Stored period is used and does not erase explicit contradictory label tokens."""
    pool = Pool.objects.create(
        season=season,
        external_id="period",
        class_name="gemengd senioren Hoofdklasse",
        sport="KORFBALL-VE-WK",
        phase="autumn",
    )
    decision = plan_pool(pool)
    assert decision["classification"]["phase"] == "autumn"
    assert decision["evidence"]["period"] == {"source": "pool", "value": "autumn"}
    pool.class_name += " nj"
    pool.phase = "spring"
    assert plan_pool(pool)["status"] == "conflict"


@pytest.mark.django_db
def test_legacy_review_survives_additive_evidence_but_resolved_period_must_agree(
    season: Season,
) -> None:
    """Schema expansion preserves a review; contradictory period evidence does not."""
    pool = Pool.objects.create(
        season=season,
        external_id="legacy-review",
        class_name="Hoofdklasse",
        sport="KORFBALL-VE-WK",
        phase="autumn",
    )
    pool.mapping_override = {
        "values": {
            "phase": "autumn",
            "gender": "mixed",
            "age_group": "senior",
            "team_kind": "standard",
        },
        "reason": "Reviewed before evidence expansion",
        "source": {
            key: value
            for key, value in plan_pool(pool)["evidence"].items()
            if key not in {"period", "gender"}
        },
    }
    assert plan_pool(pool)["status"] == "mapped"
    pool.phase = "spring"
    assert "conflicting_period" in plan_pool(pool)["issues"]


@pytest.mark.django_db
def test_relevel_preserves_shared_class_key_and_reports_orphans(season: Season) -> None:
    """Existing levels update without mutating shared identity or deleting old rows."""
    value, issues = classify(
        "gemengd senioren hoofdklasse",
        "KORFBALL-VE-WK",
        2026,
        {"team_kind": "standard", "phase": "autumn"},
    )

    row = resolve_class(season, {"classification": asdict(value), "level": None})
    stale_level, expected_level = 99, 2
    CompetitionClass.objects.filter(pk=row.pk).update(level=stale_level)
    first = Pool.objects.create(
        season=season, external_id="shared1", competition_class=row
    )
    second = Pool.objects.create(
        season=season, external_id="shared2", competition_class=row
    )
    before_key = {
        field: getattr(row, field)
        for field in (
            "code",
            "category",
            "age_group",
            "team_kind",
            "colour",
            "playing_format",
            "edition_id",
        )
    }
    preview = relevel_classes(season=season)
    assert preview["decisions"][0]["before"] == stale_level
    assert preview["decisions"][0]["after"] == level(value, issues, 2026)
    row.refresh_from_db()
    assert row.level == stale_level
    relevel_classes(season=season, apply=True)
    row.refresh_from_db()
    assert row.level == expected_level
    assert before_key == {field: getattr(row, field) for field in before_key}
    assert set(
        Pool.objects.filter(pk__in=(first.pk, second.pk)).values_list(
            "competition_class_id", flat=True
        )
    ) == {row.pk}
    assert relevel_classes(season=season, apply=True)["changed"] == 0
    orphan = CompetitionClass.objects.create(
        edition=row.edition,
        code="class_1",
        category=row.category,
        age_group=row.age_group,
        team_kind=row.team_kind,
        colour=row.colour,
        playing_format=row.playing_format,
        level=99,
    )
    receipt = relevel_classes(season=season, apply=True)
    assert (
        next(item for item in receipt["decisions"] if item["class"] == orphan.pk)[
            "orphaned"
        ]
        is True
    )
    assert CompetitionClass.objects.filter(pk=orphan.pk).exists()


@pytest.mark.django_db
def test_source_gender_requires_calibration_and_whole_pool_agreement(
    season: Season, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Unmapped/missing/conflicting participants cannot supply gender for a pool."""
    payload = match_payload()
    payload["Pool"]["ClassName"] = "senioren Hoofdklasse"
    payload["HomeTeam"]["Gender"] = "unreviewed-value"
    payload["AwayTeam"]["Gender"] = "unreviewed-value"
    Importer(season, timezone.now()).apply(
        "club_results", "synthetic", {"MatchResult": [payload]}
    )
    pool = Pool.objects.get(external_id=str(payload["Pool"]["PoolId"]))
    decision = plan_pool(pool)
    assert decision["classification"]["gender"] == "unknown"
    assert decision["evidence"]["gender"]["reason"] == "source_gender_unmapped"
    monkeypatch.setattr(
        classification,
        "SOURCE_GENDERS",
        {"unreviewed-value": "mixed", "other-value": "women"},
    )
    assert plan_pool(pool)["classification"]["gender"] == "mixed"
    pool.class_name = "dames senioren Hoofdklasse"
    assert "conflicting_gender" in plan_pool(pool)["issues"]
    pool.class_name = "senioren Hoofdklasse"
    away = Team.objects.get(external_id=str(payload["AwayTeam"]["PublicTeamId"]))
    away.source_context["fields"]["Gender"]["value"] = "other-value"
    away.save(update_fields=("source_context",))
    decision = plan_pool(pool)
    assert decision["classification"]["gender"] == "unknown"
    assert decision["evidence"]["gender"]["reason"] == "source_gender_mixed"
    # A fixture participant remains relevant when it has no standing membership.
    PoolEntry.objects.filter(pool=pool, team=away).delete()
    assert plan_pool(pool)["evidence"]["gender"]["reason"] == "source_gender_mixed"
