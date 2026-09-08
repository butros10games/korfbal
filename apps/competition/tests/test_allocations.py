"""Synthetic allocation spreadsheets cover encoding, two-column layout and linking."""

from decimal import Decimal

import pytest

from apps.competition.domain.allocations import parse_allocations
from apps.competition.models import (
    Allocation,
    AllocationSource,
    Club,
    Pool,
    PoolEntry,
    Team,
)
from apps.competition.services.allocations import import_allocations, pool_key, team_key
from apps.competition.services.classification import map_pool
from apps.schedule.models import Season


EXPECTED_ROWS = 2

CSV = (
    ";Poule/Team;Dg;Lftd;Punt;Plaats;;"
    ";Poule/Team;Dg;Lftd;Punt;Plaats;indeling dd;03-09-26\n"
    "B-categorie geel (4-tallen);;;;;;;;;;;;;;\n"
    ";Ge4-001;;;;;;;Ge4-002;;;;;;\n"
    "1;Voorbéeld J1;Za;10,2;0;Teststad;;1;Anders J8;Za;;;Anderstad;;\n"
).encode("cp1252")
A_CSV = (
    b";Poule/Team;Dg;Plaats;;;Poule/Team;Dg;Plaats;indeling dd;03-09-26\n"
    b"Dames reserve Topklasse;;;;;;;;;;\n"
    b";D-RTKA;;;;;D-RTKB;;;;\n"
    b"1;Voorbeeld 2;Zo;Teststad;;1;Anders 2;Zo;Anderstad;;\n"
)


def test_csv_preserves_ages_zero_and_missing_values() -> None:
    """Do not replace a genuine zero with missing data or lose the right column."""
    published, rows = parse_allocations(CSV)
    assert published.isoformat() == "2026-09-03"
    assert len(rows) == EXPECTED_ROWS
    assert rows[0].team_name == "Voorbéeld J1"
    assert rows[0].average_age == Decimal("10.2")
    assert rows[0].knkv_points == 0
    assert rows[1].pool_name == "Ge4-002"
    assert rows[1].average_age is None
    assert rows[1].knkv_points is None
    assert rows[0].classification["playing_format"] == "four"


def test_a_csv_has_no_fabricated_age_or_points() -> None:
    """Explicit dames/reserve headings classify independently of team numbers."""
    _, rows = parse_allocations(A_CSV)
    assert len(rows) == EXPECTED_ROWS
    assert rows[0].classification["gender"] == "women"
    assert rows[0].classification["team_kind"] == "reserve"
    assert rows[0].classification["code"] == "topklasse"
    assert rows[0].average_age is None
    assert rows[0].knkv_points is None


@pytest.mark.parametrize(
    "content",
    [
        CSV.replace(b"10,2", b"NaN"),
        CSV.replace(b"B-categorie geel", b"B-categorie violet"),
        CSV + CSV.splitlines()[-1] + b"\n",
    ],
)
def test_malformed_files_fail_before_import(content: bytes) -> None:
    """Reject unsupported or duplicate rows instead of silently skipping them."""
    with pytest.raises(ValueError, match=r"number|colour|Duplicate"):
        parse_allocations(content)


@pytest.mark.django_db
def test_staging_exact_links_and_idempotency(season: Season) -> None:
    """Link one unique season/pool/name/town identity and retain unmatched rows."""
    club = Club.objects.create(external_id="C1", name="Voorbéeld", city="Teststad")
    team = Team.objects.create(
        season=season,
        external_id="T1",
        club=club,
        name="Voorbéeld J1",
        sport="KORFBALL-VE-BK",
    )
    pool = Pool.objects.create(
        season=season, external_id="P1", name="Ge4-001", sport="KORFBALL-VE-BK"
    )
    entry = PoolEntry.objects.create(pool=pool, team=team)
    report = import_allocations(
        CSV, season, apply=False, label="Synthetic", gender="mixed"
    )
    assert report["links"] == {"matched": 1, "unmatched": 1}
    assert not AllocationSource.objects.exists()
    assert report["changed"] == EXPECTED_ROWS
    report = import_allocations(
        CSV, season, apply=True, label="Synthetic", gender="mixed"
    )
    assert report["changed"] == EXPECTED_ROWS
    assert Allocation.objects.get(entry=entry).average_age == Decimal("10.2")
    pool.refresh_from_db()
    assert pool.mapping_status == "mapped"
    assert pool.competition_class.edition.phase == "autumn"
    assert pool.competition_class.edition.gender == "mixed"
    assert pool.competition_class.level is None
    assert (
        import_allocations(CSV, season, apply=True, label="Synthetic", gender="mixed")[
            "changed"
        ]
        == 0
    )
    assert Allocation.objects.count() == EXPECTED_ROWS
    with pytest.raises(ValueError, match="gender"):
        import_allocations(CSV, season, apply=True, label="Synthetic", gender="women")
    assert Allocation.objects.count() == EXPECTED_ROWS


@pytest.mark.django_db
def test_heading_gender_conflict_is_atomic(season: Season) -> None:
    """A dames heading cannot be overridden by a mixed file scope."""
    with pytest.raises(ValueError, match="gender"):
        import_allocations(A_CSV, season, apply=True, label="Synthetic", gender="mixed")
    assert not AllocationSource.objects.exists()


@pytest.mark.django_db
def test_conflicting_provider_class_is_not_hidden_by_csv(season: Season) -> None:
    """A source correction requires review instead of silently reusing old context."""
    club = Club.objects.create(
        external_id="conflict", name="Voorbéeld", city="Teststad"
    )
    team = Team.objects.create(
        season=season,
        external_id="conflict",
        club=club,
        name="Voorbéeld J1",
        sport="KORFBALL-VE-BK",
    )
    pool = Pool.objects.create(
        season=season, external_id="conflict", name="Ge4-001", sport="KORFBALL-VE-BK"
    )
    PoolEntry.objects.create(pool=pool, team=team)
    import_allocations(CSV, season, apply=True, label="Synthetic", gender="mixed")
    pool.class_name = "Hoofdklasse"
    pool.save(update_fields=("class_name",))

    assert map_pool(pool)["status"] == "conflict"
    pool.refresh_from_db()
    assert pool.competition_class_id is None


def test_verified_pool_aliases_keep_classes_separate() -> None:
    """Code normalization handles worksheet/provider spelling, not strength order."""
    assert pool_key("M-001") == pool_key("MW-01")
    assert pool_key("MZ-001") == pool_key("MWZ-01")
    assert pool_key("D-HK-A") == pool_key("D-HKA")
    assert pool_key("S-001") != pool_key("S-002")
    assert pool_key("Ge4-001") == pool_key("Ge4-01")
    assert pool_key("Ge4-001") != pool_key("Ge-4001")
    assert team_key("t Capproen J1") == team_key("'t Capproen J1")
    assert team_key("Example J1") != team_key("Example J2")
