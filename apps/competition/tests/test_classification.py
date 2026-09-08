"""Official classes and youth designations must not invent ages or strength."""

from io import StringIO
import json

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.utils import timezone
import pytest
from rest_framework import status
from rest_framework.test import APIClient

from apps.competition.domain.classification import classify, designation, level
from apps.competition.models import CompetitionClass, CompetitionEdition, Pool
from apps.competition.services.classification import map_pool, plan_pool
from apps.competition.services.importer import Importer
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
        ("Reserve 3e klasse nj", "class_3", "unknown", "unknown", "unknown"),
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
