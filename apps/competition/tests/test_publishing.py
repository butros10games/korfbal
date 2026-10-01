"""Imported data uses native admin, discovery, teams, seasons and match endpoints."""

from copy import deepcopy
from datetime import timedelta
from io import StringIO
import time
from unittest.mock import Mock, patch
from uuid import uuid4

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.db import IntegrityError, connection, transaction
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone
import pytest
from rest_framework import status
from rest_framework.test import APIClient

from apps.club.models import Club as AppClub
from apps.competition.models import Club, Match, SyncLease, Team, TeamGroup
from apps.competition.services.importer import Importer
from apps.competition.services.publishing import (
    MatchBounds,
    Publisher,
    pending_matches,
    publish_catalogue,
)
from apps.competition.tests.fakes import RecordingScheduleChanges
from apps.competition.tests.test_importer import match_payload
from apps.game_tracker.models import MatchData, Shot
from apps.game_tracker.services.tracker_state import get_tracker_state
from apps.player.services.goal_song_manifest import build_goal_song_manifest
from apps.schedule.models import (
    Match as AppMatch,
    Season,
    SeasonPool,
)
from apps.team.models import (
    Team as AppTeam,
    TeamData,
)
from apps.team.tests.team_test_support import build_team_context


MATCH_SIDES = 2
SOURCE_VARIANTS = 3
CLEAN_PUBLICATION_QUERIES = 2


@pytest.mark.django_db
def test_imported_records_use_native_admin_search_and_detail_pages(
    season: Season,
) -> None:
    """Expose imported identities through the existing application endpoints."""
    Importer(season, timezone.now()).apply(
        "club_results", "C", {"MatchResult": [match_payload()]}
    )
    first = publish_catalogue(schedule_changes=RecordingScheduleChanges())
    assert first["counts"]["clubs_created"] == MATCH_SIDES
    assert first["counts"]["teams_created"] == MATCH_SIDES
    source = Match.objects.get()
    match = AppMatch.objects.select_related(
        "home_team__club", "away_team__club", "season"
    ).get()
    tracker = MatchData.objects.get(match_link=match)
    assert source.local_match_id == match.pk
    assert tracker.score_source == "knkv"
    assert tracker.status == "finished"
    assert (tracker.home_score, tracker.away_score) == (0, 10)
    assert not Shot.objects.exists()
    assert TeamData.objects.count() == MATCH_SIDES
    assert (
        TeamGroup.objects.filter(local_team_data__isnull=False).count() == MATCH_SIDES
    )
    assert SeasonPool.objects.get().teams.count() == MATCH_SIDES
    assert get_tracker_state(
        match, team=match.away_team, goal_audio=build_goal_song_manifest
    )["score"] == {
        "for": 10,
        "against": 0,
    }
    client = APIClient()
    client.force_authenticate(
        get_user_model().objects.create_user(username="native-search")
    )
    clubs = client.get("/api/club/clubs/?search=Club").data["results"]
    assert {row["id_uuid"] for row in clubs} == {
        str(pk) for pk in AppClub.objects.values_list("pk", flat=True)
    }
    response = client.get(f"/api/club/clubs/{match.home_team.club_id}/overview/")
    assert response.status_code == status.HTTP_200_OK
    assert response.data["matches"]["recent"][0]["score"] == {"home": 0, "away": 10}
    response = client.get(f"/api/team/teams/{match.home_team_id}/overview/")
    assert response.status_code == status.HTTP_200_OK
    assert response.data["team"]["id_uuid"] == str(match.home_team_id)
    assert client.get(f"/api/matches/{match.pk}/summary/").data["score"] == {
        "home": 0,
        "away": 10,
    }
    publish_catalogue(schedule_changes=RecordingScheduleChanges())
    assert (
        AppClub.objects.count(),
        AppTeam.objects.count(),
        AppMatch.objects.count(),
        TeamData.objects.count(),
    ) == (2, 2, 1, 2)
    user = get_user_model().objects.create_superuser(username="native-admin")
    client.force_login(user)
    session = client.session
    session["bg_auth_mfa_verified"] = user.get_session_auth_hash()
    session.save()
    assert b"Club T1" in client.get(reverse("admin:club_club_changelist")).content


@pytest.mark.django_db
def test_global_team_shared_across_sports_and_season_rosters(season: Season) -> None:
    """Provider variants map to one global team and one roster for each season."""
    payload = match_payload()["HomeTeam"]
    payload["TeamName"] = "Club T1 J1"
    importer = Importer(season, timezone.now())
    importer.team(payload)
    indoor = {**payload, "PublicTeamId": "indoor", "SportId": "KORFBALL-ZA-WK"}
    importer.team(indoor)
    later = Season.objects.create(
        name="later",
        start_date=season.start_date + timedelta(days=365),
        end_date=season.end_date + timedelta(days=365),
    )
    Importer(later, timezone.now()).team(payload)
    publish_catalogue(schedule_changes=RecordingScheduleChanges())
    assert AppTeam.objects.count() == 1
    assert AppTeam.objects.get().name == "J1"
    assert TeamData.objects.count() == MATCH_SIDES
    assert Team.objects.count() == SOURCE_VARIANTS
    assert set(TeamGroup.objects.values_list("local_team_id", flat=True)) == {
        AppTeam.objects.get().pk
    }
    with pytest.raises(IntegrityError), transaction.atomic():
        TeamData.objects.create(team=AppTeam.objects.get(), season=season)


@pytest.mark.django_db
def test_existing_global_identity_and_roster_are_preserved() -> None:
    """Link to an existing native team without replacing its members or settings."""
    context = build_team_context(suffix="publish")
    payload = match_payload()["HomeTeam"]
    payload["Club"] = {"ClubId": "C", "ClubName": context.club.name}
    payload["TeamName"] = f"{context.club.name} {context.team.name}"
    Importer(context.season, timezone.now()).team(payload)
    publish_catalogue(schedule_changes=RecordingScheduleChanges())
    assert TeamGroup.objects.get().local_team_data_id == context.team_data.pk
    assert AppTeam.objects.count() == 1
    assert list(context.team_data.players.all()) == [context.player]
    assert list(context.team_data.coach.all()) == [context.coach]


@pytest.mark.django_db
def test_results_refresh_without_overwriting_tracker_activity(season: Season) -> None:
    """Retain native tracker authority after a command."""
    data = match_payload()
    Importer(season, timezone.now()).apply("club_results", "C", {"MatchResult": [data]})
    publish_catalogue(schedule_changes=RecordingScheduleChanges())
    changed = deepcopy(data)
    changed["HomeResult"]["Score"] = 12
    Importer(season, timezone.now()).apply(
        "club_results", "C", {"MatchResult": [changed]}
    )
    publish_catalogue(schedule_changes=RecordingScheduleChanges())
    tracker = MatchData.objects.get()
    assert tracker.home_score == changed["HomeResult"]["Score"]
    local_score = 15
    tracker.home_score = local_score
    tracker.live_revision = 1
    tracker.save(update_fields=("home_score", "live_revision"))
    changed["HomeResult"]["Score"] = 20
    Importer(season, timezone.now()).apply(
        "club_results", "C", {"MatchResult": [changed]}
    )
    publish_catalogue(schedule_changes=RecordingScheduleChanges())
    tracker.refresh_from_db()
    assert tracker.home_score == local_score


@pytest.mark.django_db
def test_same_name_provider_clubs_require_review(season: Season) -> None:
    """Never combine clubs from different towns using a shared label."""
    del season
    Club.objects.create(external_id="A", name="Example", city="Town A")
    Club.objects.create(external_id="B", name="Example", city="Town B")
    report = publish_catalogue(schedule_changes=RecordingScheduleChanges())
    assert not AppClub.objects.exists()
    assert len(report["blocked"]) == MATCH_SIDES


@pytest.mark.django_db
def test_publishing_respects_the_active_import_lease() -> None:
    """Do not publish a partially written catalogue owned by another worker."""
    SyncLease.objects.create(
        key="sportlink", owner=uuid4(), expires_at=timezone.now() + timedelta(minutes=1)
    )
    with pytest.raises(ValueError, match="running"):
        publish_catalogue(schedule_changes=RecordingScheduleChanges())


@pytest.mark.django_db
def test_untouched_native_fixture_receives_official_result(season: Season) -> None:
    """An already scheduled match receives its result without changing its identity."""
    Importer(season, timezone.now()).apply(
        "club_results", "C", {"MatchResult": [match_payload()]}
    )
    source = Match.objects.select_related("home_team__club", "away_team__club").get()
    sides = []
    for team in (source.home_team, source.away_team):
        club = AppClub.objects.create(name=team.club.name)
        sides.append(AppTeam.objects.create(club=club, name=team.name))
    native = AppMatch.objects.create(
        season=season,
        home_team=sides[0],
        away_team=sides[1],
        start_time=source.starts_at,
    )
    publish_catalogue(schedule_changes=RecordingScheduleChanges())
    source.refresh_from_db()
    assert source.local_match_id == native.pk
    assert not source.local_created
    native.refresh_from_db()
    assert native.tracker_data.score_source == "knkv"
    assert native.tracker_data.status == "finished"


@pytest.mark.django_db
def test_manual_score_edit_is_retained_without_a_tracker_revision(
    season: Season,
) -> None:
    """Admin changes cannot be silently overwritten by later provider observations."""
    data = match_payload()
    Importer(season, timezone.now()).apply("club_results", "C", {"MatchResult": [data]})
    publish_catalogue(schedule_changes=RecordingScheduleChanges())
    local_score = 15
    MatchData.objects.update(home_score=local_score)
    data["HomeResult"]["Score"] = 20
    Importer(season, timezone.now()).apply("club_results", "C", {"MatchResult": [data]})
    report = publish_catalogue(schedule_changes=RecordingScheduleChanges())
    assert MatchData.objects.get().home_score == local_score
    assert report["blocked"][0]["reason"] == "local_score_changed"


@pytest.mark.django_db
def test_pool_editor_retains_imported_sport_when_older_clients_omit_it(
    season: Season,
) -> None:
    """Keep the native pool editor compatible with newly imported sport identities."""
    Importer(season, timezone.now()).apply(
        "club_results", "C", {"MatchResult": [match_payload()]}
    )
    publish_catalogue(schedule_changes=RecordingScheduleChanges())
    pool = SeasonPool.objects.get()
    client = APIClient()
    client.force_authenticate(
        get_user_model().objects.create_user(username="pool-editor", is_staff=True)
    )
    response = client.patch(
        f"/api/seasons/pools/{pool.pk}/",
        {
            "name": pool.name,
            "season_id": str(season.pk),
            "team_ids": [str(pk) for pk in pool.teams.values_list("pk", flat=True)],
        },
        format="json",
    )
    assert response.status_code == status.HTTP_200_OK
    pool.refresh_from_db()
    assert pool.sport == "KORFBALL-VE-WK"


@pytest.mark.django_db
@pytest.mark.parametrize("result", [False, True])
def test_unchanged_import_does_not_republish_but_fixture_changes_do(
    season: Season,
    result: bool,
) -> None:
    """Stable polling leaves native rows untouched; rescheduling still publishes."""
    now = timezone.now()
    row = match_payload()
    kind = "club_results" if result else "club_program"
    payload = (
        {"MatchResult": [row]} if result else {"ProgramItemMatchClub": [{"Match": row}]}
    )
    Importer(season, now).apply(kind, "CT1", payload)
    publish_catalogue(schedule_changes=RecordingScheduleChanges())
    original = Match.objects.get()
    Importer(season, now + timedelta(seconds=1)).apply(kind, "CT1", payload)
    with CaptureQueriesContext(connection) as queries:
        publication = publish_catalogue(schedule_changes=RecordingScheduleChanges())
    writes = [
        query["sql"]
        for query in queries
        if query["sql"].split()[0] in {"INSERT", "UPDATE", "DELETE"}
    ]
    assert writes == []
    assert publication["counts"].get("matches_updated", 0) == 0
    assert Match.objects.get().published_at == original.published_at
    row["MatchDateTime"] = "2026-09-06T13:30:00+0200"
    Importer(season, now + timedelta(seconds=2)).apply(kind, "CT1", payload)
    publication = publish_catalogue(schedule_changes=RecordingScheduleChanges())
    changed = Match.objects.get()
    assert publication["counts"]["matches_updated"] == 1
    assert changed.updated_at > original.updated_at
    assert AppMatch.objects.get().start_time == changed.starts_at
    assert changed.revisions.count() == int(result)
    if result:
        row["MatchDateTime"] = "2026-09-07T13:30:00+0200"
        row["HomeResult"] = {"Score": 99}
        Importer(season, now).apply(kind, "CT1", payload)
        stale = Match.objects.get()
        assert stale.starts_at == changed.starts_at
        assert stale.home_score == changed.home_score
        assert stale.result_observed_at == changed.result_observed_at
        assert stale.updated_at == changed.updated_at


@pytest.mark.django_db
def test_clean_publication_skips_native_catalogue_reads(season: Season) -> None:
    """Already-published teams and matches require only their two pending queries."""
    Importer(season, timezone.now()).apply(
        "club_results", "C", {"MatchResult": [match_payload()]}
    )
    publish_catalogue(schedule_changes=RecordingScheduleChanges())
    publisher = Publisher(RecordingScheduleChanges())
    with CaptureQueriesContext(connection) as queries:
        publisher.teams()
        publisher.matches()
    assert len(queries) == CLEAN_PUBLICATION_QUERIES
    assert not publisher.counts
    assert not publisher.blocked


@pytest.mark.django_db
def test_linked_score_correction_skips_fixture_candidate_scan(season: Season) -> None:
    """Updating a linked score cannot require reading every native fixture."""
    row = match_payload()
    observed = timezone.now()
    Importer(season, observed).apply("club_results", "C", {"MatchResult": [row]})
    publish_catalogue(schedule_changes=RecordingScheduleChanges())
    row["HomeResult"]["Score"] = 12
    Importer(season, observed + timedelta(seconds=1)).apply(
        "club_results", "C", {"MatchResult": [row]}
    )
    with CaptureQueriesContext(connection) as queries:
        Publisher(RecordingScheduleChanges()).matches()
    assert MatchData.objects.get().home_score == row["HomeResult"]["Score"]
    assert not any(
        f'FROM "{AppMatch._meta.db_table}"' in query["sql"] for query in queries
    )


@pytest.mark.django_db
@pytest.mark.parametrize("duplicate_native", [False, True])
def test_pending_fixture_lookup_preserves_ambiguity_guards(
    season: Season, duplicate_native: bool
) -> None:
    """A new source cannot claim an owned fixture or choose duplicate candidates."""
    payload = match_payload()
    Importer(season, timezone.now()).apply(
        "club_results", "C", {"MatchResult": [payload]}
    )
    publish_catalogue(schedule_changes=RecordingScheduleChanges())
    native = AppMatch.objects.get()
    if duplicate_native:
        AppMatch.objects.create(
            season=season,
            home_team_id=native.home_team_id,
            away_team_id=native.away_team_id,
            start_time=native.start_time,
        )
    payload["PublicMatchId"] = "second-source"
    Importer(season, timezone.now()).apply(
        "club_results", "C", {"MatchResult": [payload]}
    )
    source = Match.objects.get(external_id="second-source")
    publisher = Publisher(RecordingScheduleChanges())
    publisher.matches()
    source.refresh_from_db()
    assert source.local_match_id is None
    assert publisher.counts == {}
    assert publisher.blocked == [
        {"kind": "match", "source_id": source.pk, "reason": "ambiguous_fixture"}
    ]


@pytest.mark.django_db
def test_pending_fixture_loads_only_relevant_native_candidates(season: Season) -> None:
    """Resolving one source should not materialize unrelated native fixtures."""
    Importer(season, timezone.now()).apply(
        "club_results", "C", {"MatchResult": [match_payload()]}
    )
    publish_catalogue(schedule_changes=RecordingScheduleChanges())
    native = AppMatch.objects.get()
    for days in range(1, 11):
        AppMatch.objects.create(
            season=season,
            home_team_id=native.home_team_id,
            away_team_id=native.away_team_id,
            start_time=native.start_time + timedelta(days=days),
        )
    source = Match.objects.get()
    Match.objects.filter(pk=source.pk).update(local_match=None, published_at=None)
    publisher = Publisher(RecordingScheduleChanges())
    # Preserve the classmethod descriptor for Django's signature inspection.
    load = Mock(wraps=AppMatch.from_db.__func__)
    with patch.object(AppMatch, "from_db", classmethod(load)):
        publisher.matches()
    assert load.call_count == 1
    source.refresh_from_db()
    assert source.local_match_id == native.pk
    assert not publisher.blocked
    assert publisher.counts["matches_updated"] == 1


@pytest.mark.django_db
def test_published_poule_gains_new_members_without_rescanning_others(
    season: Season,
) -> None:
    """Incremental poule publication still adds members seen after publication."""
    Importer(season, timezone.now()).apply(
        "club_results", "C", {"MatchResult": [match_payload()]}
    )
    publish_catalogue(schedule_changes=RecordingScheduleChanges())
    row = deepcopy(match_payload())
    row["PublicMatchId"] = "M2"
    row["AwayTeam"] = {
        **row["AwayTeam"],
        "PublicTeamId": "T3",
        "TeamName": "Example T3",
        "Club": {"ClubId": "CT3", "ClubName": "Club T3", "City": "Example"},
    }
    Importer(season, timezone.now()).apply("club_results", "C", {"MatchResult": [row]})
    publish_catalogue(schedule_changes=RecordingScheduleChanges())
    assert SeasonPool.objects.get().teams.count() == MATCH_SIDES + 1
    with CaptureQueriesContext(connection) as queries:
        Publisher(RecordingScheduleChanges()).pools()
    # Nothing is pending: one query finds no poule to publish.
    assert len(queries.captured_queries) == 1


def import_two_matches(season: Season) -> None:
    """Import two unpublished source fixtures."""
    second = deepcopy(match_payload())
    second["PublicMatchId"] = "M2"
    second["MatchDateTime"] = "2026-09-12T13:30:00+0200"
    Importer(season, timezone.now()).apply(
        "club_results", "C", {"MatchResult": [match_payload(), second]}
    )


@pytest.mark.django_db
def test_bounded_publication_leaves_the_rest_pending(season: Season) -> None:
    """History publishes its backlog in chunks, oldest first."""
    import_two_matches(season)
    publish_catalogue(
        schedule_changes=RecordingScheduleChanges(), bounds=MatchBounds(limit=1)
    )
    assert list(pending_matches().values_list("external_id", flat=True)) == ["M2"]
    publish_catalogue(
        schedule_changes=RecordingScheduleChanges(),
        bounds=MatchBounds(deadline=time.monotonic() - 1),
    )
    assert pending_matches().count() == 1
    publish_catalogue(schedule_changes=RecordingScheduleChanges())
    assert not pending_matches().exists()


@pytest.mark.django_db
def test_season_scoped_publication_skips_other_seasons(season: Season) -> None:
    """The live sync publishes only its own season, never the history backlog."""
    import_two_matches(season)
    other = Season.objects.create(
        name="other", start_date=season.start_date, end_date=season.end_date
    )
    publish_catalogue(
        schedule_changes=RecordingScheduleChanges(),
        bounds=MatchBounds(seasons={other.pk}),
    )
    assert pending_matches().count() == MATCH_SIDES


@pytest.mark.django_db
def test_dissolved_club_gets_its_own_native_club(season: Season) -> None:
    """A dissolved club never merges into a current club with the same name."""
    current = AppClub.objects.create(name="Keizer Karel")
    row = match_payload()
    row["HomeTeam"]["Club"]["ClubName"] = ""
    row["HomeTeam"].update(TeamName="Keizer Karel 2", TeamCode="2")
    Importer(season, timezone.now(), discover=False).apply(
        "club_results", "", {"MatchResult": [row]}
    )
    publish_catalogue(schedule_changes=RecordingScheduleChanges())
    source = Club.objects.select_related("local_club").get(dissolved=True)
    assert source.local_club_id != current.pk
    assert (source.local_club.name, source.local_club.dissolved) == (
        "Keizer Karel (opgeheven)",
        True,
    )
    assert not AppClub.objects.get(pk=current.pk).dissolved


def import_twin(season: Season, status: str, match_id: str) -> None:
    """Import one record of a fixture that KNKV lists twice."""
    row = match_payload()
    row.update(PublicMatchId=match_id, Status=status)
    if status != "FINAL":
        row.update(HomeResult=None, AwayResult=None)
    Importer(season, timezone.now()).apply("club_results", "C", {"MatchResult": [row]})


@pytest.mark.django_db
def test_final_record_takes_over_a_fixture_from_its_suspended_twin(
    season: Season,
) -> None:
    """A replayed match's result replaces the suspended original on the fixture."""
    import_twin(season, "SUSPENDED", "suspended")
    publish_catalogue(schedule_changes=RecordingScheduleChanges())
    native = AppMatch.objects.get()
    import_twin(season, "FINAL", "final")
    result = publish_catalogue(schedule_changes=RecordingScheduleChanges())
    assert result["counts"]["matches_superseded"] == 1
    assert not result["blocked"]
    final = Match.objects.get(external_id="final")
    suspended = Match.objects.get(external_id="suspended")
    assert (final.local_match_id, final.local_created) == (native.pk, True)
    assert (suspended.local_match_id, suspended.local_created) == (None, False)
    native.tracker_data.refresh_from_db()
    assert native.tracker_data.status == "finished"
    assert (native.tracker_data.home_score, native.tracker_data.away_score) == (0, 10)
    assert not pending_matches().exists()


@pytest.mark.django_db
def test_suspended_twin_of_a_published_final_stays_unpublished(
    season: Season,
) -> None:
    """The superseded record neither steals the fixture nor waits as pending."""
    import_twin(season, "FINAL", "final")
    publish_catalogue(schedule_changes=RecordingScheduleChanges())
    import_twin(season, "SUSPENDED", "suspended")
    assert not pending_matches().exists()
    result = publish_catalogue(schedule_changes=RecordingScheduleChanges())
    assert not result["blocked"]
    assert Match.objects.get(external_id="final").local_match_id is not None
    assert AppMatch.objects.count() == 1


MANUAL_HOME_SCORE = 5


def manual_fixture(season: Season, offset: timedelta) -> AppMatch:
    """Enter the imported fixture by hand, kickoff stored ``offset`` away."""
    source = Match.objects.select_related("home_team__club", "away_team__club").get()
    sides = []
    for team in (source.home_team, source.away_team):
        club, _ = AppClub.objects.get_or_create(name=team.club.name)
        sides.append(AppTeam.objects.get_or_create(club=club, name=team.name)[0])
    native = AppMatch.objects.create(
        season=season,
        home_team=sides[0],
        away_team=sides[1],
        start_time=source.starts_at + offset,
    )
    MatchData.objects.filter(match_link=native).update(
        status="finished",
        home_score=MANUAL_HOME_SCORE,
        away_score=4,
        score_source="tracker",
    )
    return native


@pytest.mark.django_db
def test_manual_fixture_an_hour_off_is_linked_not_duplicated(season: Season) -> None:
    """Dutch local time saved as UTC still identifies the same fixture."""
    Importer(season, timezone.now()).apply(
        "club_results", "C", {"MatchResult": [match_payload()]}
    )
    native = manual_fixture(season, timedelta(hours=1))
    result = publish_catalogue(schedule_changes=RecordingScheduleChanges())
    assert result["counts"]["matches_clock_twin"] == 1
    assert Match.objects.get().local_match_id == native.pk
    assert AppMatch.objects.count() == 1
    tracker = MatchData.objects.get(match_link=native)
    assert (tracker.score_source, tracker.home_score) == ("tracker", MANUAL_HOME_SCORE)


@pytest.mark.django_db
def test_other_offsets_still_create_their_own_fixture(season: Season) -> None:
    """Only whole clock offsets of one or two hours identify a fixture."""
    Importer(season, timezone.now()).apply(
        "club_results", "C", {"MatchResult": [match_payload()]}
    )
    manual_fixture(season, timedelta(hours=3))
    publish_catalogue(schedule_changes=RecordingScheduleChanges())
    assert AppMatch.objects.count() == len(["manual", "imported"])


@pytest.mark.django_db
def test_repair_merges_an_imported_copy_into_its_manual_fixture(
    season: Season,
) -> None:
    """Copies created before clock matching are merged once, keeping the tracker."""
    Importer(season, timezone.now()).apply(
        "club_results", "C", {"MatchResult": [match_payload()]}
    )
    publish_catalogue(schedule_changes=RecordingScheduleChanges())
    copy = Match.objects.get().local_match
    native = manual_fixture(season, -timedelta(hours=2))
    out = StringIO()
    call_command("repair_clock_twins", stdout=out)
    assert '"source": "M1"' in out.getvalue()
    assert AppMatch.objects.filter(pk=copy.pk).exists()
    call_command("repair_clock_twins", "--apply", stdout=StringIO())
    source = Match.objects.get()
    assert (source.local_match_id, source.local_created) == (native.pk, False)
    assert not AppMatch.objects.filter(pk=copy.pk).exists()
    publish_catalogue(schedule_changes=RecordingScheduleChanges())
    assert AppMatch.objects.count() == 1
    assert MatchData.objects.get(match_link=native).home_score == MANUAL_HOME_SCORE
