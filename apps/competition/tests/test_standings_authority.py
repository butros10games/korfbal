"""Official and generated standings stay separate, provisional and bounded."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
import json
from pathlib import Path

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connection, transaction
from django.test.utils import CaptureQueriesContext
import pytest
from rest_framework.test import APIClient

from apps.club.models import Club as NativeClub
from apps.competition.domain.standings_provenance import (
    COMPUTED,
    FINAL,
    FINAL_REVIEW,
    NONE,
    OFFICIAL,
    PARTIAL,
    PROVISIONAL,
    UNKNOWN,
    computed_provenance,
    content_digest,
    fixture_coverage,
    generated_standing,
    is_official_standing,
    table_digest,
    table_source,
    table_status,
    table_values,
    tied,
)
from apps.competition.models import (
    Club,
    HistoricalResource,
    Match,
    Pool,
    PoolEntry,
    Team,
    TeamGroup,
)
from apps.competition.services.computed_standings import (
    canonical_results,
    locked_entries,
    refresh_generated_standings,
    write_generated_table,
)
from apps.competition.services.importer import Importer
from apps.competition.services.standings_repair import Selection, apply, preview
from apps.schedule.models import Season
from apps.team.models import Team as NativeTeam


TODAY = date(2026, 10, 4)
WRITES = ("UPDATE", "INSERT", "DELETE")


def test_one_authority_decides_the_whole_table() -> None:
    """A filtered feed shows nothing; official rows always beat generated ones."""
    assert (
        table_source(official=True, generated=True, results_filtered=False) == OFFICIAL
    )
    assert (
        table_source(official=False, generated=True, results_filtered=False) == COMPUTED
    )
    assert table_source(official=True, generated=True, results_filtered=True) == NONE
    assert (
        table_source(official=False, generated=True, results_filtered=True) == COMPUTED
    )
    assert table_source(official=False, generated=False, results_filtered=False) == NONE
    assert is_official_standing({"Position": 1})
    assert not is_official_standing({})
    assert not is_official_standing({"Position": 1, "Computed": True})
    # The new column wins over a legacy copy that awaits clearing.
    assert generated_standing({"Position": 2, "Computed": True}, None) == {
        "Position": 2
    }
    assert generated_standing({"Position": 2, "Computed": True}, {"Position": 1}) == {
        "Position": 1
    }
    assert generated_standing({"Position": 2}, None) is None


def test_finality_needs_recorded_final_evidence() -> None:
    """Synchronized, closed or generated tables never become final by themselves."""
    final = final_provenance("a" * 32)
    assert table_status(OFFICIAL, {}) == UNKNOWN
    assert table_status(OFFICIAL, {"official": {"status": "final"}}) == UNKNOWN
    assert table_status(OFFICIAL, final) == FINAL
    assert (
        table_status(COMPUTED, {**final, "computed": {"status": "final"}})
        == PROVISIONAL
    )
    assert table_status(NONE, final) == UNKNOWN
    for evidence in (True, "published", ["published"], {"reviewed": "synthetic"}):
        assert (
            table_status(
                OFFICIAL, {"official": {"status": FINAL, "evidence": evidence}}
            )
            == UNKNOWN
        )
    assert table_status(OFFICIAL, {**final, "official_digest": "b" * 32}) == UNKNOWN
    assert fixture_coverage(COMPUTED, {"computed": {"coverage": "complete"}}) == UNKNOWN
    assert fixture_coverage(COMPUTED, {"computed": {"coverage": "partial"}}) == PARTIAL
    assert fixture_coverage(OFFICIAL, {}) == UNKNOWN
    assert fixture_coverage(NONE, {"computed": {"coverage": "partial"}}) == UNKNOWN
    with pytest.raises(ValueError, match="reason"):
        computed_provenance(
            reason="guess", results=1, partial=False, digest="", computed_at=""
        )


def final_provenance(digest: str) -> dict:
    """Bind synthetic review evidence to one specific complete official table."""
    return {
        "official_digest": digest,
        "official": {
            "status": FINAL,
            "evidence": {
                "kind": FINAL_REVIEW,
                "reference": "synthetic reviewed source",
                "table_digest": digest,
            },
        },
    }


def test_table_digest_tracks_content_and_authority_without_timestamps() -> None:
    """Polling and ordering are stable, but membership and source authority matter."""
    digest = content_digest([(1, {"Position": 1}), (2, {})])
    assert digest == content_digest([(2, {}), (1, {"Position": 1})])
    assert digest != content_digest([(1, {"Position": 1})])
    assert (
        table_digest(OFFICIAL, {"official_digest": digest}) == f"official:v1:{digest}"
    )
    assert (
        table_digest(COMPUTED, {"computed": {"digest": digest, "computed_at": "later"}})
        == f"computed:v1:{digest}"
    )
    assert table_digest(NONE, {"official_digest": digest}) is None


def test_deductions_are_shown_once_and_negative_totals_survive() -> None:
    """Official points already include the deduction; booleans stay unknown."""
    values = table_values({
        "Position": "1",
        "TotalPoints": -2,
        "PenaltyPoints": 4,
        "Won": True,
        "Lost": 1.5,
    })
    assert (values["position"], values["points"], values["penalty_points"]) == (
        1,
        -2,
        4,
    )
    assert (values["won"], values["lost"]) == (None, None)
    assert table_values(None)["penalty_points"] is None


def test_ties_follow_the_table_authority() -> None:
    """Official ties are shared positions; generated tables tie on equal points."""
    official = [
        {"Position": 1, "TotalPoints": 10},
        {"Position": 1, "TotalPoints": 10},
        {"Position": 3, "TotalPoints": 10},
    ]
    assert tied(OFFICIAL, official[0], official)
    assert not tied(OFFICIAL, official[2], official)
    generated = [{"Position": 1, "TotalPoints": 6}, {"Position": 2, "TotalPoints": 6}]
    assert tied(COMPUTED, generated[0], generated)
    assert not tied(OFFICIAL, generated[0], generated)


@pytest.fixture
def closed() -> Season:
    """Create a finished indoor season of edition 2024."""
    return Season.objects.create(
        name="Zaal 2024-2025",
        start_date=date(2024, 10, 1),
        end_date=date(2025, 3, 31),
        edition=2024,
        discipline="indoor",
        phase="indoor",
    )


def poule(season: Season, external_id: str = "P1", **fields: object) -> Pool:
    """Create an unfiltered source poule."""
    return Pool.objects.create(
        season=season,
        external_id=external_id,
        name="A",
        class_name="Hoofdklasse",
        results_filtered=fields.pop("results_filtered", False),
        **fields,
    )


def member(
    pool: Pool,
    name: str,
    *,
    standing: dict | None = None,
    computed: dict | None = None,
    local_team: NativeTeam | None = None,
) -> PoolEntry:
    """Create one club team's source record and poule membership."""
    club, _ = Club.objects.get_or_create(
        external_id=f"club-{name}", defaults={"name": f"Club {name}"}
    )
    group, _ = TeamGroup.objects.get_or_create(
        season=pool.season,
        club=club,
        normalized_name=name,
        defaults={"name": name, "local_team": local_team},
    )
    team = Team.objects.create(
        season=pool.season,
        club=club,
        external_id=f"{pool.external_id}-{name}",
        name=name,
        sport="KORFBALL-ZA-WK",
        group=group,
    )
    return PoolEntry.objects.create(
        pool=pool, team=team, standing=standing or {}, computed_standing=computed
    )


def fixture(
    pool: Pool,
    sides: tuple[PoolEntry, PoolEntry],
    score: tuple[int, int] | None,
    external_id: str,
    *,
    day: int = 1,
) -> Match:
    """Create one fixture; no score means it was not played."""
    home, away = sides
    return Match.objects.create(
        season=pool.season,
        external_id=external_id,
        pool=pool,
        home_team=home.team,
        away_team=away.team,
        starts_at=datetime(2024, 11, day, 14, tzinfo=UTC),
        status="FINAL" if score else "UPCOMING",
        home_score=score[0] if score else None,
        away_score=score[1] if score else None,
    )


def write(pool: Pool, reason: str = "closed_blank_fallback") -> str:
    """Write a generated table under the same locks as the services."""
    with transaction.atomic():
        locked = Pool.objects.select_for_update(no_key=True).get(pk=pool.pk)
        return write_generated_table(locked, locked_entries(locked), reason=reason)


def api() -> APIClient:
    """Return an authenticated catalogue client."""
    client = APIClient()
    client.force_authenticate(get_user_model().objects.create(username="tables"))
    return client


def standing_payload(entry: PoolEntry, **values: object) -> dict:
    """Build a source table row using only synthetic public identities."""
    team = entry.team
    return {
        "PublicTeamId": team.external_id,
        "TeamName": team.name,
        "SportId": team.sport,
        "Club": {"ClubId": team.club.external_id, "ClubName": team.club.name},
        **values,
    }


def result_payload(match: Match) -> dict:
    """Build one synthetic fixture observation for regeneration checks."""
    return {
        "PublicMatchId": match.external_id,
        "MatchDateTime": match.starts_at.isoformat(),
        "Status": match.status,
        "HomeTeam": standing_payload(
            PoolEntry.objects.get(pool=match.pool, team=match.home_team)
        ),
        "AwayTeam": standing_payload(
            PoolEntry.objects.get(pool=match.pool, team=match.away_team)
        ),
        "Pool": {
            "PoolId": match.pool.external_id,
            "PoolName": match.pool.name,
            "ClassName": match.pool.class_name,
        },
        "HomeResult": {"Score": match.home_score},
        "AwayResult": {"Score": match.away_score},
        "AutoResult": None,
    }


@pytest.mark.django_db
@pytest.mark.parametrize("change", ["values", "removed", "filtered", "member"])
def test_official_replacement_invalidates_only_changed_final_review(
    closed: Season, change: str
) -> None:
    """Equal polls retain review; changed values, membership or filtering clear it."""
    pool = poule(closed)
    home, away = member(pool, "H"), member(pool, "A")
    table = [standing_payload(home, Position=1), standing_payload(away, Position=2)]
    now = datetime(2026, 10, 4, tzinfo=UTC)
    envelope = {
        "MatchResult": [],
        "PoolStanding": {"PoolStandingTeam": table},
        "ResultsFiltered": False,
    }
    Importer(closed, now, discover=False).apply(
        "pool_results", pool.external_id, envelope
    )
    pool.refresh_from_db()
    provenance = final_provenance(pool.standings_provenance["official_digest"])
    provenance["computed"] = {"reason": "legacy_conversion", "digest": "b" * 32}
    pool.standings_provenance = provenance
    pool.save(update_fields=("standings_provenance",))
    Importer(closed, now + timedelta(seconds=1), discover=False).apply(
        "pool_results", pool.external_id, envelope
    )
    pool.refresh_from_db()
    assert pool.standings_provenance == provenance
    assert table_status(OFFICIAL, pool.standings_provenance) == FINAL
    if change == "values":
        table[0]["Position"] = 2
    elif change == "removed":
        table.pop()
    elif change == "filtered":
        envelope["ResultsFiltered"] = True
    else:
        extra = member(pool, "N")
        table.append(standing_payload(extra, Position=3))
    Importer(closed, now + timedelta(seconds=2), discover=False).apply(
        "pool_results", pool.external_id, envelope
    )
    pool.refresh_from_db()
    assert "official" not in pool.standings_provenance
    assert pool.standings_provenance["computed"] == provenance["computed"]


@pytest.mark.django_db
def test_generated_tables_are_visible_with_legacy_filtered_flag(closed: Season) -> None:
    """Generated authority is visible and filtered official tables stay hidden."""
    native = NativeTeam.objects.create(
        name="Synthetic", club=NativeClub.objects.create(name="Synthetic")
    )
    pool = poule(closed, results_filtered=True)
    home, away = member(pool, "H", local_team=native), member(pool, "A")
    match = fixture(pool, (home, away), (20, 10), "M1")
    assert write(pool) == "written"
    client = api()
    url = (
        f"/api/competition/pools/team-standings/?local_team={native.pk}"
        f"&season={closed.pk}"
    )
    row = client.get(url).data["results"][0]
    assert row["results_filtered"] is True
    assert row["table_source"] == COMPUTED
    assert row["table_digest"].startswith("computed:v1:")
    expected_teams = len((home, away))
    assert len(row["standings"]["results"]) == expected_teams
    assert (
        client.get(f"/api/competition/pools/{pool.pk}/standings/").data["count"]
        == expected_teams
    )
    before_digest = row["table_digest"]
    match.home_score = 5
    match.save(update_fields=("home_score",))
    refresh_generated_standings([pool.pk])
    assert client.get(url).data["results"][0]["table_digest"] != before_digest
    away.standing = {"Position": 1}
    away.save(update_fields=("standing",))
    row = client.get(url).data["results"][0]
    assert row["table_source"] == NONE
    assert row["table_digest"] is None
    assert row["standings"]["results"] == []
    assert client.get(f"/api/competition/pools/{pool.pk}/standings/").data["count"] == 0


@pytest.mark.django_db
def test_imported_correction_refreshes_only_touched_generated_table(
    closed: Season,
) -> None:
    """A score correction updates its table key; identical polls do not recompute it."""
    pool = poule(closed)
    home, away = member(pool, "H"), member(pool, "A")
    match = fixture(pool, (home, away), (20, 10), "M1")
    other = poule(closed, "OTHER")
    other_sides = (member(other, "X"), member(other, "Y"))
    fixture(other, other_sides, (10, 20), "M2")
    assert write(pool) == write(other) == "written"
    pool.refresh_from_db()
    other.refresh_from_db()
    before = pool.standings_provenance["computed"]
    untouched = other.standings_provenance
    row = result_payload(match)
    row["HomeResult"]["Score"] = 5
    now = datetime(2026, 10, 4, tzinfo=UTC)
    Importer(closed, now, discover=False).apply(
        "club_results", "club-H", {"MatchResult": [row]}
    )
    pool.refresh_from_db()
    after = pool.standings_provenance["computed"]
    assert after["digest"] != before["digest"]
    assert PoolEntry.objects.get(pk=away.pk).computed_standing["Position"] == 1
    Importer(closed, now + timedelta(seconds=1), discover=False).apply(
        "club_results", "club-H", {"MatchResult": [row]}
    )
    pool.refresh_from_db()
    other.refresh_from_db()
    assert pool.standings_provenance["computed"] == after
    assert other.standings_provenance == untouched


@pytest.mark.django_db
def test_fixture_move_refreshes_old_and_new_generated_tables(closed: Season) -> None:
    """Moving a scored fixture clears its old table and recalculates its new pool."""
    old, new = poule(closed, "OLD"), poule(closed, "NEW")
    old_sides = member(old, "H"), member(old, "A")
    new_sides = member(new, "H"), member(new, "A")
    moved = fixture(old, old_sides, (20, 10), "M1")
    fixture(new, new_sides, (10, 20), "M2", day=2)
    assert write(old) == write(new) == "written"
    new.refresh_from_db()
    before = new.standings_provenance[COMPUTED]["digest"]
    row = result_payload(moved)
    row["Pool"] = {
        "PoolId": new.external_id,
        "PoolName": new.name,
        "ClassName": new.class_name,
    }
    Importer(closed, datetime(2026, 10, 4, tzinfo=UTC), discover=False).apply(
        "club_results", "synthetic", {"MatchResult": [row]}
    )
    old.refresh_from_db()
    new.refresh_from_db()
    assert COMPUTED not in old.standings_provenance
    assert not PoolEntry.objects.filter(
        pool=old, computed_standing__isnull=False
    ).exists()
    assert new.standings_provenance[COMPUTED]["digest"] != before
    assert new.standings_provenance[COMPUTED]["results"] == len(("M1", "M2"))


@pytest.mark.django_db
def test_membership_discovery_invalidates_current_official_review(
    closed: Season,
) -> None:
    """Assignment members alter the content key before another table poll."""
    pool = poule(closed)
    home, away = (
        member(pool, "H", standing={"Position": 1}),
        member(pool, "A", standing={"Position": 2}),
    )
    digest = content_digest([
        (home.team_id, home.standing),
        (away.team_id, away.standing),
    ])
    pool.standings_provenance = final_provenance(digest)
    pool.save(update_fields=("standings_provenance",))
    new_member = member(pool, "N")
    new_payload = standing_payload(new_member)
    new_member.delete()
    row = {
        "PoolId": pool.external_id,
        "PoolName": pool.name,
        "ClassName": pool.class_name,
        "PoolAssignment": {"TeamInPoolAssignment": [new_payload]},
    }
    Importer(closed, datetime(2026, 10, 4, tzinfo=UTC), discover=False).apply(
        "team_pools", home.team.external_id, {"TeamPool": [row]}
    )
    pool.refresh_from_db()
    assert "official" not in pool.standings_provenance
    assert pool.standings_provenance["official_digest"] != digest


@pytest.mark.django_db
def test_unconverted_legacy_table_is_cleared_when_no_results_remain(
    closed: Season,
) -> None:
    """A changed pool cannot fall back to a stale legacy computed JSON row."""
    pool = poule(closed)
    row = member(pool, "H", standing={"Position": 1, "Computed": True})
    assert refresh_generated_standings([pool.pk]) == {"cleared": 1}
    row.refresh_from_db()
    assert row.standing == {}
    assert row.computed_standing is None


@pytest.mark.django_db
def test_generated_table_never_overwrites_official_rows(closed: Season) -> None:
    """An official table, even a partial one, blocks any generated rows."""
    pool = poule(closed)
    home = member(pool, "H", standing={"Position": 1, "TotalPoints": 2})
    away = member(pool, "A")
    fixture(pool, (home, away), (20, 10), "M1")
    assert write(pool) == "official_present"
    assert not PoolEntry.objects.filter(computed_standing__isnull=False).exists()
    assert Pool.objects.get().standings_provenance == {}


@pytest.mark.django_db
def test_canonical_finals_count_an_archive_twin_once(closed: Season) -> None:
    """The provider's record is the fixture; unplayed fixtures make it partial."""
    pool = poule(closed)
    home, away = member(pool, "H"), member(pool, "A")
    fixture(pool, (home, away), (20, 10), "M1")
    fixture(pool, (home, away), (20, 10), "archive:M1")
    fixture(pool, (away, home), None, "M2", day=8)
    inputs = canonical_results(pool.pk)
    assert len(inputs.results) == 1
    assert inputs.partial is True
    assert write(pool) == "written"
    provenance = Pool.objects.get().standings_provenance[COMPUTED]
    assert (provenance["coverage"], provenance["results"]) == (PARTIAL, 1)


@pytest.mark.django_db
def test_unchanged_reruns_write_nothing_and_only_generated_poules_refresh(
    closed: Season,
) -> None:
    """Touched-poule refreshes are idempotent and skip official or empty tables."""
    pool = poule(closed)
    home, away = member(pool, "H"), member(pool, "A")
    match = fixture(pool, (home, away), (20, 10), "M1")
    official = poule(closed, "P2")
    member(official, "X", standing={"Position": 1})
    assert write(pool) == "written"
    with CaptureQueriesContext(connection) as queries:
        assert refresh_generated_standings([pool.pk, official.pk]) == {"unchanged": 1}
    assert not [q for q in queries if q["sql"].lstrip().upper().startswith(WRITES)]
    Match.objects.filter(pk=match.pk).update(home_score=5)
    assert refresh_generated_standings([pool.pk]) == {"written": 1}
    assert PoolEntry.objects.get(pk=away.pk).computed_standing["Position"] == 1
    Match.objects.filter(pk=match.pk).delete()
    assert refresh_generated_standings([pool.pk]) == {"cleared": 1}
    assert not PoolEntry.objects.filter(computed_standing__isnull=False).exists()
    assert COMPUTED not in Pool.objects.get(pk=pool.pk).standings_provenance


@pytest.mark.django_db
def test_official_authority_holds_on_every_page(closed: Season) -> None:
    """A later page never switches to generated values, and deductions show once."""
    pool = poule(closed)
    member(pool, "A", standing={"Position": "1", "TotalPoints": 7, "PenaltyPoints": 2})
    member(pool, "B", standing={"Position": "2", "TotalPoints": 9})
    member(pool, "C", computed={"Position": 1, "TotalPoints": 12})
    client = api()
    first = client.get(f"/api/competition/pools/{pool.pk}/standings/?page_size=2").data
    assert [row["values"]["position"] for row in first["results"]] == [1, 2]
    assert (
        first["results"][0]["values"]["points"],
        first["results"][0]["values"]["penalty_points"],
    ) == (7, 2)
    later = client.get(
        f"/api/competition/pools/{pool.pk}/standings/?page_size=2&page=2"
    ).data["results"]
    assert [row["team"]["name"] for row in later] == ["C"]
    assert set(later[0]["values"].values()) == {None}


@pytest.mark.django_db
def test_team_standings_label_a_generated_table(closed: Season) -> None:
    """Generated values come from their own column, labelled provisional."""
    native = NativeTeam.objects.create(
        name="1", club=NativeClub.objects.create(name="N")
    )
    pool = poule(closed)
    home = member(pool, "H", local_team=native)
    away = member(pool, "A")
    fixture(pool, (home, away), (20, 10), "M1")
    fixture(pool, (away, home), None, "M2", day=8)
    assert write(pool) == "written"
    response = (
        api()
        .get(
            f"/api/competition/pools/team-standings/?local_team={native.pk}&season={closed.pk}"
        )
        .data["results"]
    )
    [table] = response
    assert {
        key: table[key]
        for key in (
            "table_source",
            "standings_computed",
            "table_status",
            "fixture_coverage",
        )
    } == {
        "table_source": COMPUTED,
        "standings_computed": True,
        "table_status": PROVISIONAL,
        "fixture_coverage": PARTIAL,
    }
    rows = [
        (row["team"]["name"], row["values"]["position"])
        for row in table["standings"]["results"]
    ]
    assert rows == [("H", 1), ("A", 2)]
    assert table["standings"]["results"][0]["values"]["penalty_points"] is None


@pytest.mark.django_db
def test_blank_closed_preview_is_read_only_and_apply_is_fingerprinted(
    closed: Season,
) -> None:
    """Only a fresh reviewed manifest writes, beside the empty official table."""
    pool = poule(closed)
    home, away = member(pool, "H"), member(pool, "A")
    fixture(pool, (home, away), (20, 10), "M1")
    selection = Selection(stage="blank-closed", editions=(2024,))
    with CaptureQueriesContext(connection) as queries:
        manifest = preview(selection, today=TODAY).as_payload()
    assert not [q for q in queries if q["sql"].lstrip().upper().startswith(WRITES)]
    assert manifest["counts"] == {"generate": 1}
    assert manifest["pools"][0]["official_checkpoints"] == []

    # A changed score after the review makes the plan stale.
    Match.objects.filter(external_id="M1").update(home_score=21)
    assert [row["outcome"] for row in apply(manifest).pools] == ["stale"]
    assert not PoolEntry.objects.filter(computed_standing__isnull=False).exists()

    manifest = preview(selection, today=TODAY).as_payload()
    assert [row["outcome"] for row in apply(manifest).pools] == ["written"]
    stored = Pool.objects.get()
    assert stored.standings_provenance[COMPUTED]["reason"] == "closed_blank_fallback"
    assert stored.standings_synced_at is None
    assert all(entry.standing == {} for entry in PoolEntry.objects.all())
    # Reruns find nothing to do, and the applied manifest cannot be replayed.
    assert preview(selection, today=TODAY).as_payload()["counts"] == {"unchanged": 1}
    assert [row["outcome"] for row in apply(manifest).pools] == ["stale"]


@pytest.mark.django_db
def test_official_recovery_comes_before_a_generated_table(closed: Season) -> None:
    """Retained or pending official tables and filtered feeds get no fallback."""
    retained, pending, filtered = (
        poule(closed, "R"),
        poule(closed, "Q"),
        poule(closed, "F", results_filtered=True),
    )
    for pool in (retained, pending, filtered):
        home, away = (
            member(pool, f"{pool.external_id}H"),
            member(pool, f"{pool.external_id}A"),
        )
        fixture(pool, (home, away), (20, 10), f"{pool.external_id}M")
    for pool, state, evidence in (
        (
            retained,
            "fetched",
            {"official_table": {"PoolStandingTeam": [{"PublicTeamId": "1"}]}},
        ),
        (pending, "pending", {}),
    ):
        HistoricalResource.objects.create(
            season=closed,
            provider="app",
            kind="edition_pool",
            source_id=pool.external_id,
            key=f"key-{pool.external_id}",
            start_date=closed.start_date,
            end_date=closed.end_date,
            state=state,
            evidence=evidence,
        )
    manifest = preview(
        Selection(
            stage="blank-closed", pool_ids=(retained.pk, pending.pk, filtered.pk)
        ),
        today=TODAY,
    ).as_payload()
    assert {row["external_id"]: row["outcome"] for row in manifest["pools"]} == {
        "R": "official_retained",
        "Q": "official_pending",
        "F": "filtered_feed",
    }
    assert apply(manifest).pools == []


@pytest.mark.django_db
def test_legacy_tables_convert_then_clear_without_losing_visibility(
    closed: Season, tmp_path: Path
) -> None:
    """Conversion keeps the legacy copy; clearing leaves the generated table."""
    pool = poule(closed)
    legacy = {
        "TotalMatches": 1,
        "Won": 1,
        "Draw": 0,
        "Lost": 0,
        "TotalPoints": 2,
        "GoalsFor": 20,
        "GoalsAgainst": 10,
        "GoalsDifference": 10,
        "Position": 1,
    }
    home = member(pool, "H", standing={**legacy, "Computed": True})
    away = member(
        pool, "A", standing={"Position": 2, "TotalPoints": 0, "Computed": True}
    )
    fixture(pool, (home, away), (20, 10), "archive:M1")
    path = tmp_path / "manifest.json"
    call_command(
        "repair_competition_standings",
        "--stage",
        "convert-legacy",
        "--pool",
        str(pool.pk),
        "--manifest",
        str(path),
    )
    assert json.loads(path.read_text())["counts"] == {"convert": 1}
    call_command(
        "repair_competition_standings",
        "--stage",
        "convert-legacy",
        "--apply",
        "--manifest",
        str(path),
    )
    home.refresh_from_db()
    assert home.computed_standing == legacy
    assert home.standing["Computed"] is True
    with pytest.raises(CommandError, match="another --stage"):
        call_command(
            "repair_competition_standings",
            "--stage",
            "clear-legacy",
            "--apply",
            "--manifest",
            str(path),
        )
    call_command(
        "repair_competition_standings",
        "--stage",
        "clear-legacy",
        "--pool",
        str(pool.pk),
        "--manifest",
        str(path),
    )
    call_command(
        "repair_competition_standings",
        "--stage",
        "clear-legacy",
        "--apply",
        "--manifest",
        str(path),
    )
    assert all(entry.standing == {} for entry in PoolEntry.objects.all())
    rows = api().get(f"/api/competition/pools/{pool.pk}/standings/").data["results"]
    assert [(row["team"]["name"], row["values"]["position"]) for row in rows] == [
        ("H", 1),
        ("A", 2),
    ]
    with pytest.raises(CommandError, match="requires"):
        call_command("repair_competition_standings", "--apply")
    with pytest.raises(CommandError, match="edition or poule"):
        call_command("repair_competition_standings")
