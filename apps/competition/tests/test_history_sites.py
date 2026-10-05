"""Public result sites fill seasons the app no longer serves; the app still wins."""

from datetime import date
from io import StringIO
import json
from unittest.mock import Mock

from django.core.management import call_command
from django.utils import timezone
import pytest

from apps.competition.adapters.outbound.history import HistoryClient
from apps.competition.adapters.outbound.public_sites import PublicSiteClient
from apps.competition.application.ports import FetchResult, TransportError
from apps.competition.models import (
    Club,
    HistoricalResource,
    Match,
    Pool,
    PoolEntry,
    Team,
)
from apps.competition.services.computed_standings import (
    GOAL_DIFFERENCE_CLASSES,
    refresh_edition_standings,
    standings,
)
from apps.competition.services.history_editions import (
    current_edition,
    full_year_season,
    prepare_edition,
    recheck_edition,
    seed_edition,
)
from apps.competition.services.history_sites import (
    KORFBALNL,
    UITSLAGEN,
    seed_site,
    site_summary,
)
from apps.competition.services.importer import Importer
from apps.competition.services.publishing import pending_matches, publish_catalogue
from apps.competition.services.seasons import INDOOR, OUTDOOR
from apps.competition.services.site_repair import repair_site
from apps.competition.tasks import recheck_competition_history
from apps.competition.tests.fakes import RecordingScheduleChanges
from apps.competition.tests.test_history_editions import (
    row as app_row,
    run,
    standing,
    team as app_team,
)
from apps.game_tracker.models import MatchData
from apps.schedule.models import (
    Match as AppMatch,
    Season,
)


EDITION = 2024
INDOOR_SEASON = "Zaal seizoen 2024-2025"
KICKOFF = "2024-11-09T14:00:00.000Z"


@pytest.fixture(autouse=True)
def no_spacing_wait(monkeypatch: pytest.MonkeyPatch) -> None:
    """Exercise durable reservations without sleeping in regression tests."""
    monkeypatch.setattr("apps.competition.services.traffic.time.sleep", lambda _: None)


def catalogue(serie: str = "NAJAAR") -> dict:
    """Fabricate the former KNKV site's clubs, sports and poule classes."""
    return {
        "clubs": [
            {
                "_id": f"c{name}",
                "ref_id": f"C{name}",
                "name": f"Club {name}",
                "address": {"city": "Example"},
            }
            for name in ("T1", "T2")
        ],
        "sports": [
            {"_id": "outdoor", "ref_id": OUTDOOR},
            {"_id": "indoor", "ref_id": INDOOR},
        ],
        "poules": [
            {
                "ref_id": "7",
                "name": "A",
                "division": {"name": "Ereklasse"},
                "serie": serie,
            }
        ],
    }


def site_match(identifier: str, when: str = KICKOFF, **changes: object) -> dict:
    """Fabricate a played indoor match between T1 and T2 on the former KNKV site."""
    return {
        "_id": f"mongo{identifier}",
        "ref_id": identifier,
        "date": when,
        "sport": {"_id": "indoor"},
        "status": {"game": "uitgespeeld"},
        "teams": {
            "home": {"ref_id": "11", "name": "Example T1"},
            "away": {"ref_id": "12", "name": "Example T2"},
        },
        "clubs": {
            "home": {"_id": "cT1", "name": "Club T1"},
            "away": {"_id": "cT2", "name": "Club T2"},
        },
        "poule": {"ref_id": "7", "name": "A"},
        "stats": {"home": {"score": 20}, "away": {"score": 18}},
        **changes,
    }


def weeks(*matches: dict) -> FetchResult:
    """Wrap matches the way the site groups a club's results."""
    return FetchResult(200, {"weeks": [{"_id": 45, "matches": list(matches)}]})


def page_row(identifier: int, *, pool: str = "7", score: int | None = 20) -> dict:
    """Fabricate one korfbal-uitslagen.nl match with Sportlink identities."""
    side = {
        name: {
            "ref_id": name,
            "name": f"Example {name}",
            "club": {"ref_id": f"C{name}", "name": f"Club {name}"},
        }
        for name in ("T1", "T2")
    }
    return {
        "id": identifier,
        "date": "2024-11-09T14:00:00+00:00",
        "home_score": score,
        "away_score": 18 if score is not None else None,
        "pool": {
            "ref_id": pool,
            "name": "A",
            "division": {"name": "Ereklasse"},
            "phase": {"sport": {"ref_id": INDOOR}},
        },
        "home": side["T1"],
        "away": side["T2"],
    }


def import_club(*matches: dict) -> None:
    """Queue the edition and import one club's matches from the former KNKV site."""
    seed_site(KORFBALNL, EDITION)
    run([FetchResult(200, catalogue())])
    # One read per club: the same match is listed under both of its clubs.
    run([weeks(*matches), weeks(*matches)])


@pytest.mark.django_db
def test_former_knkv_site_fills_a_season_with_archive_matches() -> None:
    """Played matches land in the playing season with Sportlink's poule number."""
    import_club(
        site_match("1001"),
        site_match("1002", status={"game": "gepland"}),
        site_match("1003", clubs={"home": {"_id": "gone"}, "away": {"_id": "cT1"}}),
    )
    match = Match.objects.select_related("pool", "season", "home_team").get()
    assert match.external_id == "archive:knkv:1001"
    assert (match.status, match.home_score, match.away_score) == ("FINAL", 20, 18)
    assert match.season.name == INDOOR_SEASON
    assert (match.pool.external_id, match.pool.class_name) == ("7", "Ereklasse")
    assert match.home_team.external_id == "archive:knkv:11"
    summary = site_summary(EDITION)
    assert summary["matches"] == {INDOOR_SEASON: 1}
    assert summary["skipped"] == {"not_played": 2, "club_unknown": 2}
    assert summary["checkpoints"][KORFBALNL] == {
        "catalogue/fetched": 1,
        "club_matches/fetched": 2,
    }


AUTUMN, SPRING = "2024-09-14T13:00:00.000Z", "2025-04-12T13:00:00.000Z"
FULL_YEAR = "Veld seizoen 2024-2025"


def outdoor_match(identifier: str, when: str) -> dict:
    """Fabricate a played outdoor match in poule 7."""
    return site_match(identifier, when, sport={"_id": "outdoor"})


def app_match(season: Season, identifier: str, when: str) -> None:
    """Import one provider result of outdoor poule 7 into a season."""
    Importer(season, timezone.now(), discover=False).apply(
        "club_results", "", {"MatchResult": [app_row(identifier, when, pool=7)]}
    )


@pytest.mark.django_db
def test_full_year_outdoor_poule_stays_whole_in_its_own_season() -> None:
    """The site's regular outdoor series plays both halves as one competition."""
    seed_site(KORFBALNL, EDITION)
    run([FetchResult(200, catalogue("REGULIER"))])
    run([weeks(outdoor_match("1", AUTUMN), outdoor_match("2", SPRING)), weeks()])
    assert set(Match.objects.values_list("season__name", flat=True)) == {FULL_YEAR}
    assert Pool.objects.get().season.name == FULL_YEAR


@pytest.mark.django_db
def test_provider_poule_in_the_full_year_season_is_not_copied_into_a_half() -> None:
    """The provider's poule covers the whole edition, whichever season holds it."""
    prepare_edition(EDITION)
    app_match(full_year_season(EDITION), "M1", "2024-09-14T15:00:00+0200")
    import_club(outdoor_match("1", AUTUMN), outdoor_match("2", SPRING))
    assert list(Match.objects.values_list("external_id", flat=True)) == ["M1"]
    assert site_summary(EDITION)["skipped"] == {"app_has_poule": 4}


@pytest.mark.django_db
def test_reading_a_club_again_moves_a_half_copy_to_the_full_year_season() -> None:
    """Copies an earlier read placed by date are retired, fixtures included."""
    import_club(outdoor_match("1", AUTUMN))
    publish_catalogue(schedule_changes=RecordingScheduleChanges())
    assert Match.objects.get().season.name == "Voor seizoen 2024"
    HistoricalResource.objects.update(state="pending")
    run([FetchResult(200, catalogue("REGULIER"))])
    run([weeks(outdoor_match("1", AUTUMN)), weeks()])
    assert Match.objects.get().season.name == FULL_YEAR
    assert not AppMatch.objects.exists()
    assert not Pool.objects.filter(season__name="Voor seizoen 2024").exists()


@pytest.mark.django_db
def test_repair_removes_site_copies_of_provider_poules_and_reads_again() -> None:
    """Published duplicates go with their fixtures; the site is queued again."""
    import_club(outdoor_match("1", AUTUMN))
    publish_catalogue(schedule_changes=RecordingScheduleChanges())
    app_match(full_year_season(EDITION), "M1", "2024-09-14T15:00:00+0200")
    preview = repair_site(KORFBALNL, EDITION, apply=False)
    assert (preview["duplicates"], preview["poules"]) == (1, 1)
    assert Match.objects.count() == len({"M1", "archive:knkv:1"})

    result = repair_site(KORFBALNL, EDITION, apply=True)
    assert (result["removed"], result["kept_in_use"]) == (1, 0)
    assert list(Match.objects.values_list("external_id", flat=True)) == ["M1"]
    assert not AppMatch.objects.exists()
    assert not HistoricalResource.objects.exclude(state="pending").exists()


@pytest.mark.django_db
def test_catalogue_without_series_is_read_again_before_its_clubs() -> None:
    """An older catalogue cannot place full-year poules."""
    import_club(site_match("1001"))
    stored = HistoricalResource.objects.get(kind="catalogue")
    stored.evidence = {**stored.evidence, "version": 1}
    stored.save()
    seed_site(KORFBALNL, EDITION)
    assert HistoricalResource.objects.get(state="pending").kind == "catalogue"


@pytest.mark.django_db
def test_site_poule_gets_standings_computed_from_its_results() -> None:
    """A site has results but no table; the poule page still shows a ranking."""
    import_club(site_match("1001"), site_match("1002", "2024-11-16T14:00:00.000Z"))
    pool = Pool.objects.get()
    # Generated authority does not claim that an official feed was unfiltered.
    assert pool.results_filtered is True
    # A generated table is not an official synchronization.
    assert pool.standings_synced_at is None
    assert pool.standings_provenance["computed"] | {
        "digest": "",
        "computed_at": "",
    } == {
        "calculation": "computed-v2",
        "reason": "archive_results",
        "status": "provisional",
        "coverage": "unknown",
        "deductions": "unknown",
        "results": 2,
        "digest": "",
        "computed_at": "",
    }
    entries = {
        entry.team.name: entry for entry in PoolEntry.objects.select_related("team")
    }
    # The official standings stay empty; generated rows never enter them.
    assert all(entry.standing == {} for entry in entries.values())
    rows = {name: entry.computed_standing for name, entry in entries.items()}
    assert rows["Example T1"] == {
        "TotalMatches": 2,
        "Won": 2,
        "Draw": 0,
        "Lost": 0,
        "TotalPoints": 4,
        "GoalsFor": 40,
        "GoalsAgainst": 36,
        "GoalsDifference": 4,
        "Position": 1,
    }
    assert (rows["Example T2"]["Position"], rows["Example T2"]["Lost"]) == (2, 2)


@pytest.mark.django_db
def test_provider_poule_keeps_its_official_standings() -> None:
    """Only poules with nothing but site results get a computed table."""
    indoor = prepare_edition(EDITION).indoor
    Importer(indoor, timezone.now(), discover=False).apply(
        "club_results",
        "",
        {
            "MatchResult": [
                app_row("M1", "2024-11-09T15:00:00+0100", sport=INDOOR, pool=7)
            ]
        },
    )
    assert refresh_edition_standings(EDITION) == {"edition": EDITION, "poules": 0}
    assert Pool.objects.get().results_filtered is True


def result(home: str, away: str, home_score: int, away_score: int) -> tuple:
    """Fabricate one final result between two teams."""
    return (home, away, home_score, away_score)


def test_equal_points_are_decided_by_mutual_results_or_goal_difference() -> None:
    """Most competitions rank tied teams on their own matches; the youngest do not."""
    results = [result("A", "B", 20, 5), result("C", "A", 12, 11)]
    # A and C both have 2 points; C won their match, A has the better difference.
    assert [team for team, _ in standings(results, "ABC")] == ["C", "A", "B"]
    by_difference = [team for team, _ in standings(results, "ABC", mutual=False)]
    assert by_difference == ["A", "C", "B"]
    assert GOAL_DIFFERENCE_CLASSES.match("E-jeugd BK")
    assert GOAL_DIFFERENCE_CLASSES.match("Midweek dames nj")
    assert not GOAL_DIFFERENCE_CLASSES.match("Ereklasse")
    assert not GOAL_DIFFERENCE_CLASSES.match("")


def delisted_match(identifier: str = "1001") -> dict:
    """Fabricate a match of T1 against a club the site no longer lists."""
    return site_match(
        identifier,
        teams={
            "home": {"ref_id": "11", "name": "Example T1"},
            "away": {"ref_id": "99", "name": "Old Club 1"},
        },
        clubs={
            "home": {"_id": "cT1", "name": "Club T1"},
            "away": {"_id": "cOld", "name": "Old Club"},
        },
    )


@pytest.mark.django_db
def test_delisted_club_becomes_a_dissolved_club_with_its_own_read() -> None:
    """The site keeps a merged club's matches and name but not its code."""
    seed_site(KORFBALNL, EDITION)
    run([FetchResult(200, catalogue())])
    run([weeks(delisted_match()), weeks()])
    club = Match.objects.select_related("away_team__club").get().away_team.club
    assert (club.external_id, club.name, club.dissolved) == (
        "archive:knkv:cOld",
        "Old Club",
        True,
    )
    # Its matches against other delisted clubs appear in no listed club's read.
    assert HistoricalResource.objects.get(state="pending").source_id == "cOld"
    assert "club_unknown" not in site_summary(EDITION)["skipped"]


@pytest.mark.django_db
def test_delisted_club_is_the_catalogue_club_with_exactly_that_name() -> None:
    """A club the app history already knows keeps its Sportlink code."""
    Club.objects.create(external_id="NCX1", name="Old Club", dissolved=True)
    seed_site(KORFBALNL, EDITION)
    run([FetchResult(200, catalogue())])
    run([weeks(delisted_match()), weeks()])
    assert Match.objects.get().away_team.club.external_id == "NCX1"
    assert Club.objects.filter(name="Old Club").count() == 1


@pytest.mark.django_db
def test_team_without_a_sportlink_code_uses_the_site_team_number() -> None:
    """Teams the site added in spring 2018 carry scores but no team code."""
    import_club(
        site_match(
            "1001",
            teams={
                "home": {"ref_id": "11", "name": "Example T1"},
                "away": {"_id": "5a7fb971", "name": "Example T2"},
            },
        )
    )
    assert Match.objects.get().away_team.external_id == "archive:knkv:5a7fb971"
    assert "incomplete" not in site_summary(EDITION)["skipped"]
    stored = HistoricalResource.objects.filter(kind="club_matches").first()
    stored.evidence = {"skipped": {"incomplete": 2}}
    stored.save()
    assert seed_site(KORFBALNL, EDITION)["requeued"] == 1


@pytest.mark.django_db
def test_queueing_a_site_again_rereads_clubs_with_skipped_delisted_rows() -> None:
    """Reads made before delisted clubs were recognised are repeated once."""
    import_club(site_match("1001"))
    skipped = HistoricalResource.objects.filter(kind="club_matches").first()
    skipped.evidence = {"skipped": {"club_unknown": 3}}
    skipped.save()
    assert seed_site(KORFBALNL, EDITION)["requeued"] == 1
    assert HistoricalResource.objects.get(state="pending").pk == skipped.pk


@pytest.mark.django_db
def test_one_site_team_plays_outdoors_and_indoors() -> None:
    """The former KNKV site uses one team code for both disciplines."""
    import_club(
        site_match("1001", "2024-09-14T13:00:00.000Z", sport={"_id": "outdoor"}),
        site_match("1002"),
    )
    assert dict(Match.objects.values_list("external_id", "season__name")) == {
        "archive:knkv:1001": "Voor seizoen 2024",
        "archive:knkv:1002": INDOOR_SEASON,
    }


@pytest.mark.django_db
def test_site_team_is_named_after_the_catalogue_club() -> None:
    """A sponsor name from another year must not publish a second team."""
    Club.objects.create(external_id="CT1", name="Club T1/New Sponsor")
    import_club(
        site_match(
            "1001",
            teams={
                "home": {"ref_id": "11", "name": "Club T1 3"},
                "away": {"ref_id": "12", "name": "Club T2 3"},
            },
        )
    )
    assert Team.objects.get(external_id="archive:knkv:11").name == (
        "Club T1/New Sponsor 3"
    )


@pytest.mark.django_db
def test_a_site_waits_for_running_app_discovery() -> None:
    """The app is the preferred source; a site only fills what it left empty."""
    current = Season.objects.create(
        name="current", start_date=date(2026, 8, 1), end_date=date(2027, 6, 30)
    )
    Importer(current, timezone.now(), discover=False).team(app_team("T1"))
    seed_edition(EDITION)
    assert seed_site(KORFBALNL, EDITION) == {
        "edition": EDITION,
        "source": KORFBALNL,
        "queued": 0,
        "reason": "app_discovery_pending",
    }


@pytest.mark.django_db
def test_poules_the_app_delivered_are_not_filled_from_a_site() -> None:
    """A poule with provider matches keeps them; other poules are imported."""
    indoor = prepare_edition(EDITION).indoor
    Importer(indoor, timezone.now(), discover=False).apply(
        "club_results",
        "",
        {
            "MatchResult": [
                app_row("M1", "2024-11-09T15:00:00+0100", sport=INDOOR, pool=7)
            ]
        },
    )
    import_club(
        site_match("1001"), site_match("1002", poule={"ref_id": "8", "name": "B"})
    )
    assert set(Match.objects.values_list("external_id", flat=True)) == {
        "M1",
        "archive:knkv:1002",
    }
    assert site_summary(EDITION)["skipped"] == {"app_has_poule": 2}


@pytest.mark.django_db
def test_result_pages_follow_the_last_match_number(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A full page queues the next one; unplayed rows are never imported."""
    monkeypatch.setattr("apps.competition.services.history_sites.PAGE_SIZE", 2)
    seed_site(UITSLAGEN, EDITION)
    run([FetchResult(200, {"rows": [page_row(4), page_row(9, score=None)]})])
    run([FetchResult(200, {"rows": [page_row(12, pool="8")]})])
    assert sorted(
        HistoricalResource.objects.filter(provider=UITSLAGEN).values_list(
            "source_id", "state"
        )
    ) == [("0", "fetched"), ("9", "fetched")]
    assert set(Match.objects.values_list("external_id", flat=True)) == {
        "archive:ku:4",
        "archive:ku:12",
    }
    # Sportlink's own team IDs: the provider's record reuses the same team.
    assert set(Team.objects.values_list("external_id", flat=True)) == {"T1", "T2"}


@pytest.mark.django_db
def test_a_site_cannot_move_a_known_team_to_another_discipline() -> None:
    """Sportlink team IDs are per discipline; a mislabelled poule is skipped."""
    current = Season.objects.create(
        name="current", start_date=date(2026, 8, 1), end_date=date(2027, 6, 30)
    )
    Importer(current, timezone.now(), discover=False).team(app_team("T1", OUTDOOR))
    seed_site(UITSLAGEN, EDITION)
    run([FetchResult(200, {"rows": [page_row(4)]})])
    assert not Match.objects.exists()
    assert site_summary(EDITION)["skipped"] == {"sport_mismatch": 1}


@pytest.mark.django_db
def test_provider_record_replaces_its_published_archive_twin() -> None:
    """The restored provider result adopts the native fixture the archive made."""
    seed_site(UITSLAGEN, EDITION)
    run([FetchResult(200, {"rows": [page_row(4)]})])
    publish_catalogue(schedule_changes=RecordingScheduleChanges())
    native = AppMatch.objects.get()
    assert MatchData.objects.get(match_link=native).score_source == "archive"

    current = Season.objects.create(
        name="current", start_date=date(2026, 8, 1), end_date=date(2027, 6, 30)
    )
    Importer(current, timezone.now(), discover=False).team(app_team("T1", INDOOR))
    seed_edition(EDITION, scan=False)
    run([FetchResult(200, {"Pool": [{"PoolId": 7}]})])
    run([
        FetchResult(
            200,
            {
                "ResultsFiltered": False,
                "MatchResult": [
                    app_row("M1", "2024-11-09T15:00:00+0100", sport=INDOOR, pool=7)
                ],
                "PoolStanding": standing(1, "T1", "T2", sport=INDOOR),
            },
        )
    ])
    match = Match.objects.get(season__name=INDOOR_SEASON)
    assert (match.external_id, match.local_match_id) == ("M1", native.pk)

    publish_catalogue(schedule_changes=RecordingScheduleChanges())
    assert AppMatch.objects.count() == 1
    assert MatchData.objects.get(match_link=native).score_source == "knkv"
    assert not pending_matches().exists()


def two_empty_teams() -> None:
    """Scan an edition whose two probe teams the provider answers with nothing."""
    current = Season.objects.create(
        name="current", start_date=date(2026, 8, 1), end_date=date(2027, 6, 30)
    )
    importer = Importer(current, timezone.now(), discover=False)
    for name in ("T1", "T2"):
        importer.team(app_team(name, INDOOR))
    seed_edition(EDITION)
    run([FetchResult(200, {"Pool": []}), FetchResult(200, {"Pool": []})])


@pytest.mark.django_db
def test_recheck_reads_a_sample_and_a_hit_reopens_the_edition() -> None:
    """One restored checkpoint queues every other empty one again."""
    two_empty_teams()
    assert recheck_edition(EDITION, sample=1) == {"edition": EDITION, "rechecked": 1}
    assert list(
        HistoricalResource.objects.filter(state="pending").values_list(
            "source_id", "coverage"
        )
    ) == [("T1", "empty")]

    run([FetchResult(200, {"Pool": [{"PoolId": 10}]})])
    states = dict(
        HistoricalResource.objects.filter(kind="edition_team").values_list(
            "source_id", "state"
        )
    )
    assert states == {"T1": "fetched", "T2": "pending"}
    assert HistoricalResource.objects.filter(kind="edition_pool").exists()


@pytest.mark.django_db
def test_recheck_that_stays_empty_changes_nothing_else() -> None:
    """An edition the provider still withholds costs only the sample."""
    two_empty_teams()
    recheck_edition(EDITION, sample=1)
    run([FetchResult(200, {"Pool": []})])
    assert not HistoricalResource.objects.filter(state="pending").exists()
    # The sample rotates: the checkpoint read longest ago goes next.
    recheck_edition(EDITION, sample=1)
    assert HistoricalResource.objects.get(state="pending").source_id == "T2"


@pytest.mark.django_db
def test_recheck_queues_an_edition_that_was_never_read() -> None:
    """An unserved edition without checkpoints starts like a new import."""
    current = Season.objects.create(
        name="current", start_date=date(2026, 8, 1), end_date=date(2027, 6, 30)
    )
    Importer(current, timezone.now(), discover=False).team(app_team("T1"))
    assert recheck_edition(EDITION) == {"edition": EDITION, "rechecked": 1}
    assert recheck_edition(EDITION)["reason"] == "discovery_pending"


@pytest.mark.django_db
def test_weekly_task_rechecks_only_finished_configured_editions(
    settings: object,
) -> None:
    """The running edition is live work, never a historical recheck."""
    settings.SPORTLINK_SYNC_ENABLED = True
    settings.SPORTLINK_HISTORY_RECHECK_EDITIONS = [EDITION, current_edition()]
    result = recheck_competition_history()
    assert [row["edition"] for row in result["editions"]] == [EDITION]
    settings.SPORTLINK_SYNC_ENABLED = False
    assert recheck_competition_history() == {"status": "disabled"}


@pytest.mark.django_db
def test_command_queues_a_site_and_reports_it() -> None:
    """Operators choose the site explicitly and read its progress per edition."""
    out = StringIO()
    call_command(
        "import_competition_history",
        "site",
        "--source",
        UITSLAGEN,
        "--edition",
        str(EDITION),
        stdout=out,
    )
    assert json.loads(out.getvalue()) == [
        {"edition": EDITION, "source": UITSLAGEN, "queued": 1, "requeued": 0}
    ]
    status = StringIO()
    call_command(
        "import_competition_history",
        "status",
        "--edition",
        str(EDITION),
        stdout=status,
    )
    assert json.loads(status.getvalue())[0]["sites"]["checkpoints"] == {
        UITSLAGEN: {"match_page/pending": 1}
    }


def reply(*, text: str = "", body: object = None, status: int = 200) -> Mock:
    """Fabricate one HTTP response of a result site."""
    return Mock(status_code=status, text=text, json=lambda: body, headers={})


def site_resource(provider: str, kind: str, source_id: str) -> HistoricalResource:
    """Build an unsaved 2024-2025 checkpoint for wire-contract tests."""
    return HistoricalResource(
        season=Season(start_date=date(2024, 10, 1), end_date=date(2025, 6, 30)),
        provider=provider,
        kind=kind,
        source_id=source_id,
        start_date=date(2024, 7, 1),
        end_date=date(2025, 6, 30),
    )


def test_former_knkv_site_key_is_read_once_and_requests_are_counted() -> None:
    """The site's published key authorises reads; every attempt passes the gate."""
    client, gate = PublicSiteClient(), Mock()
    client.session.get = Mock(
        side_effect=[
            reply(text="api: 'x', token: 'site.key-1',"),
            reply(body=[{"_id": 45, "matches": []}]),
            reply(body=[]),
        ]
    )
    resource = site_resource(KORFBALNL, "club_matches", "club1")
    assert client.fetch(resource, gate).data == {"weeks": [{"_id": 45, "matches": []}]}
    client.fetch(resource, gate)
    calls = client.session.get.call_args_list
    assert [call.args[0] for call in calls] == [
        "https://competitie.korfbal.nl/configs/environment.js",
        "https://api.korfbal.nl/v1/matches/club/club1",
        "https://api.korfbal.nl/v1/matches/club/club1",
    ]
    assert calls[1].kwargs["headers"] == {"Authorization": "Bearer site.key-1"}
    assert calls[1].kwargs["params"]["start"] == "2024-07-01"
    assert calls[1].kwargs["params"]["end"] == "2025-06-30"
    assert gate.before_request.call_count == len(calls)
    assert all(call.kwargs["allow_redirects"] is False for call in calls)


def test_a_single_row_collection_is_read_as_one_row() -> None:
    """The former KNKV site returns a one-poule series as a bare object."""
    client, gate = PublicSiteClient(), Mock()
    client.korfbalnl_token = "key"
    client.session.get = Mock(
        side_effect=[
            reply(body=[{"_id": "s1", "year": 2024, "serie": "NAJAAR"}]),
            reply(body={"_id": "p1", "ref_id": "7", "name": "A"}),
            reply(body=[]),
            reply(body=[]),
        ]
    )
    data = client.fetch(site_resource(KORFBALNL, "catalogue", "2024"), gate).data
    assert data["poules"] == [
        {"_id": "p1", "ref_id": "7", "name": "A", "serie": "NAJAAR"}
    ]
    client.session.get = Mock(return_value=reply(body={"statusCode": 404}))
    with pytest.raises(TransportError):
        client.fetch(site_resource(KORFBALNL, "club_matches", "club1"), gate)


def test_result_page_is_bounded_by_the_edition_and_the_cursor() -> None:
    """A page reads only the edition's matches after the checkpoint's number."""
    client, gate = PublicSiteClient(), Mock()
    client.session.get = Mock(
        side_effect=[
            reply(text='<script src="/assets/index-Ab1_c.js"></script>'),
            reply(text='"https://abc123.supabase.co","eyJh.eyJi.c-d_e"'),
            reply(body=[{"id": 41}]),
        ]
    )
    result = client.fetch(site_resource(UITSLAGEN, "match_page", "40"), gate)
    assert result.data == {"rows": [{"id": 41}]}
    page = client.session.get.call_args_list[2]
    assert page.args[0] == "https://abc123.supabase.co/rest/v1/matches"
    assert ("id", "gt.40") in page.kwargs["params"]
    assert ("date", "gte.2024-07-01") in page.kwargs["params"]
    assert ("date", "lt.2025-07-01") in page.kwargs["params"]
    assert page.kwargs["headers"]["apikey"] == "eyJh.eyJi.c-d_e"
    assert gate.before_request.call_count == len(client.session.get.call_args_list)


def test_site_http_failure_is_reported_as_its_status() -> None:
    """A rate limit reaches the worker's cooldown handling without a body."""
    client = HistoryClient()
    client.sites = PublicSiteClient()
    client.sites.korfbalnl_token = "key"
    client.sites.session.get = Mock(return_value=reply(status=429))
    result = client.fetch(site_resource(KORFBALNL, "club_matches", "club1"), Mock())
    assert (result.status, result.data) == (429, None)
    client.close()
