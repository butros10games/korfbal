"""Observation ordering, atomic validation and deliberately bounded discovery."""

from datetime import timedelta
from io import StringIO
import json

from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
import pytest
from pytest_django.fixtures import Settings
from rest_framework.test import APIClient

from apps.competition.domain.rosters import RosterPayloadError
from apps.competition.models import (
    MatchMembership,
    RosterMembership,
    SyncResource,
    Team,
    TeamParticipation,
)
from apps.competition.services.importer import Importer
from apps.competition.services.rosters import (
    RosterQueueSelection,
    apply_roster_plan,
    plan_rosters,
)
from apps.competition.services.seasons import INDOOR, OUTDOOR
from apps.competition.tests.test_importer import match_payload, team_payload
from apps.competition.tests.test_lineups import lineup
from apps.competition.tests.test_rosters import person
from apps.competition.tests.test_season_bindings import setup_variants
from apps.player.models import Player
from apps.schedule.models import Season
from apps.team.models import TeamData


pytestmark = pytest.mark.django_db
DISCOVERY_CANDIDATES = 42
PILOT_SIZE = 20
FAILURE_CEILING = 6


@pytest.mark.parametrize("existing", [False, True])
def test_older_roster_keeps_membership_with_newer_visible_identity(
    season: Season, existing: bool
) -> None:
    """A different team's newer identity must not retire a present roster member."""
    now = timezone.now()
    importer = Importer(season, now)
    importer.team(team_payload("T1"))
    importer.team(team_payload("T2"))
    if existing:
        importer.apply("team_roster", "T1", {"TeamPersonOverview": [person()]})
    newer = {**person(), "FirstName": "Newer", "ShirtNumber": 19}
    Importer(season, now + timedelta(hours=2)).apply(
        "team_roster", "T2", {"TeamPersonOverview": [newer]}
    )
    Importer(season, now + timedelta(hours=1)).apply(
        "team_roster", "T1", {"TeamPersonOverview": [person()]}
    )
    player = Player.objects.get(knkv_person_id="P1")
    assert player.name == "Newer van Player"
    assert player.knkv_observed_at == now + timedelta(hours=2)
    membership = RosterMembership.objects.get(team__external_id="T1")
    assert membership.player_id == player.pk
    assert membership.ended_at is None
    assert membership.last_seen_at == now + timedelta(hours=1)
    assert membership.shirt_number == "7"


@pytest.mark.parametrize("archived", [False, True])
def test_older_roster_does_not_restore_withdrawn_or_archived_identity(
    season: Season, archived: bool
) -> None:
    """Newer privacy withdrawal and native archival retain their authority."""
    now = timezone.now()
    importer = Importer(season, now)
    importer.team(team_payload("T1"))
    importer.team(team_payload("T2"))
    importer.apply("team_roster", "T1", {"TeamPersonOverview": [person()]})
    if archived:
        Player.all_objects.update(archived_at=now + timedelta(hours=2))
    else:
        Importer(season, now + timedelta(hours=2)).apply(
            "team_roster", "T2", {"TeamPersonOverview": [person(privacy="PRIVATE")]}
        )
    Importer(season, now + timedelta(hours=1)).apply(
        "team_roster", "T1", {"TeamPersonOverview": [person()]}
    )
    assert not Player.objects.exists()
    assert not RosterMembership.objects.filter(ended_at=None).exists()
    player = Player.all_objects.get(knkv_person_id="P1")
    assert (
        player.archived_at is not None if archived else player.knkv_privacy == "PRIVATE"
    )


def test_membership_last_seen_stays_monotonic(season: Season) -> None:
    """An accepted older identity-independent roster cannot regress its interval."""
    now = timezone.now()
    importer = Importer(season, now)
    team = importer.team(team_payload("T1"))
    importer.apply("team_roster", "T1", {"TeamPersonOverview": [person()]})
    later = now + timedelta(hours=2)
    RosterMembership.objects.update(last_seen_at=later)
    Importer(season, now + timedelta(hours=1)).apply(
        "team_roster", "T1", {"TeamPersonOverview": [person()]}
    )
    assert RosterMembership.objects.get(team=team).last_seen_at == later


@pytest.mark.parametrize(
    ("change", "code"),
    [
        ({"PrivacyLevel": None}, "privacy"),
        ({"TeamPerson": "true"}, "membership"),
        ({"TeamPersonFunction": []}, "role"),
        ({"TeamPersonFunction": {"RoleId": None}}, "role"),
        ({"FirstName": False}, "name"),
        ({"ShirtNumber": {"private": "never retain"}}, "shirt"),
    ],
)
def test_malformed_person_rejects_whole_roster_with_safe_reason(
    season: Season, change: dict, code: str
) -> None:
    """Malformed rows cannot withdraw one member while retiring valid peers."""
    importer = Importer(season, timezone.now())
    importer.team(team_payload("T1"))
    importer.apply("team_roster", "T1", {"TeamPersonOverview": [person()]})
    previous = list(RosterMembership.objects.values())
    with pytest.raises(RosterPayloadError) as error:
        importer.apply(
            "team_roster",
            "T1",
            {
                "TeamPersonOverview": [
                    person(privacy="PRIVATE"),
                    {**person("P2"), **change},
                ]
            },
        )
    assert error.value.code == code
    assert str(error.value) == f"Invalid roster {code}"
    assert list(RosterMembership.objects.values()) == previous
    assert Player.objects.count() == 1


def test_malformed_second_lineup_side_rolls_back_privacy(season: Season) -> None:
    """Both sides validate before an otherwise valid home withdrawal is applied."""
    importer = Importer(season, timezone.now())
    importer.match(match_payload(), result=True)
    importer.apply("match_lineup", "M1", lineup())
    previous = list(MatchMembership.objects.values())
    payload = lineup()
    payload["HomeTeamPerson"][0]["PrivacyLevel"] = "PRIVATE"
    payload["AwayTeamPerson"][0]["TeamPersonFunction"] = None
    with pytest.raises(RosterPayloadError, match="Invalid roster role"):
        importer.apply("match_lineup", "M1", payload)
    assert list(MatchMembership.objects.values()) == previous
    assert Player.objects.filter(knkv_person_id="P1").exists()


def test_private_counts_follow_observation_period_and_team_scope(
    season: Season,
) -> None:
    """A spring snapshot follows participation rather than its autumn default."""
    now = timezone.now()
    today = timezone.localdate(now)
    importer, outdoor, indoor = setup_variants(season)
    outdoor.refresh_from_db()
    native_team = outdoor.local_team_data.team
    autumn = Season.objects.create(
        name="Synthetic autumn",
        start_date=today - timedelta(days=100),
        end_date=today - timedelta(days=30),
    )
    spring = Season.objects.create(
        name="Synthetic spring",
        start_date=today - timedelta(days=10),
        end_date=today + timedelta(days=30),
    )
    autumn_roster = TeamData.objects.create(team=native_team, season=autumn)
    spring_roster = TeamData.objects.create(team=native_team, season=spring)
    for source, count in ((outdoor, 3), (indoor, 2)):
        Team.objects.filter(pk=source.pk).update(local_team_data=autumn_roster)
        TeamParticipation.objects.create(
            team=source, phase="autumn", team_data=autumn_roster
        )
        TeamParticipation.objects.create(
            team=source, phase="spring", team_data=spring_roster
        )
        importer.apply(
            "team_roster",
            source.external_id,
            {"TeamPersonOverview": [person("PRIVATE", "PRIVATE")] * count},
        )

    def counts(requested: Season) -> dict:
        response = APIClient().get(
            f"/api/team/teams/{native_team.pk}/overview/",
            {"season": str(requested.pk)},
        )
        return response.data["private_roster"]

    assert counts(spring) == {
        "players": 3,
        "staff": 0,
        "is_estimate": True,
    }
    assert counts(autumn)["players"] == 0
    # Expiry remains the same eight-day privacy boundary used for visible people.
    Team.objects.filter(pk__in=[outdoor.pk, indoor.pk]).update(
        roster_observed_at=now - timedelta(days=8, seconds=1)
    )
    assert counts(spring)["players"] == 0


def test_missing_roster_discovery_is_bounded_no_write_and_idempotent(
    season: Season, settings: Settings
) -> None:
    """An explicit indoor pilot queues 20 of 42 without changing import switches."""
    settings.SPORTLINK_IMPORT_ROSTERS = False
    importer = Importer(season, timezone.now())
    for index in range(DISCOVERY_CANDIDATES):
        payload = team_payload(f"T{index:03}")
        payload["SportId"] = INDOOR
        importer.team(payload)
    other = team_payload("OUTDOOR")
    other["SportId"] = OUTDOOR
    importer.team(other)
    with CaptureQueriesContext(connection) as queries:
        plan = plan_rosters(season, selection=RosterQueueSelection(sport=INDOOR))
    assert not any(
        query["sql"].lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE"))
        for query in queries
    )
    assert not SyncResource.objects.filter(kind="team_roster").exists()
    assert plan.counts["unobserved_missing_feeds"] == DISCOVERY_CANDIDATES
    assert plan.missing == tuple(f"T{index:03}" for index in range(PILOT_SIZE))
    assert plan.next_cursor == "T019"
    assert apply_roster_plan(plan) == PILOT_SIZE
    assert apply_roster_plan(plan) == 0
    second = plan_rosters(
        season, selection=RosterQueueSelection(sport=INDOOR, after=plan.next_cursor)
    )
    assert len(second.missing) == PILOT_SIZE
    assert apply_roster_plan(second) == PILOT_SIZE
    assert second.next_cursor is not None
    last = plan_rosters(
        season, selection=RosterQueueSelection(sport=INDOOR, after=second.next_cursor)
    )
    assert len(last.missing) == DISCOVERY_CANDIDATES - PILOT_SIZE * 2
    assert last.next_cursor is None
    assert settings.SPORTLINK_IMPORT_ROSTERS is False
    assert not SyncResource.objects.filter(
        kind="team_roster", source_id="OUTDOOR"
    ).exists()


def test_command_defaults_to_preview_and_requires_explicit_apply(
    season: Season,
) -> None:
    """Existing season/refresh flags stay supported, while writes require --apply."""
    Importer(season, timezone.now()).team(team_payload("T1"))
    output = StringIO()
    call_command("queue_competition_rosters", season=season.name, stdout=output)
    report = json.loads(output.getvalue())
    assert report["dry_run"] is True
    assert report["queued"] == report["http_requests"] == 0
    assert report["selected_missing"] == ["T1"]
    assert not SyncResource.objects.filter(kind="team_roster").exists()
    call_command(
        "queue_competition_rosters", season=season.name, apply=True, stdout=StringIO()
    )
    assert SyncResource.objects.filter(kind="team_roster").count() == 1
    with pytest.raises(CommandError, match="Every selected roster source"):
        call_command(
            "queue_competition_rosters",
            season=season.name,
            team_id=["OTHER-SCOPE"],
            stdout=StringIO(),
        )


def test_stale_private_refresh_plan_preserves_new_failure_ceiling(
    season: Season,
) -> None:
    """Failures discovered after preview remain exhausted when the plan is applied."""
    importer = Importer(season, timezone.now())
    source = importer.team(team_payload("T1"))
    Team.objects.filter(pk=source.pk).update(
        private_roster_counts={"players": 1, "staff": 0}
    )
    feed = SyncResource.objects.create(
        season=season,
        kind="team_roster",
        source_id="T1",
        fetched_at=timezone.now(),
        next_sync_at=timezone.now(),
        etag="preserved",
    )
    plan = plan_rosters(season, refresh_private=True)
    assert plan.refresh == ("T1",)
    SyncResource.objects.filter(pk=feed.pk).update(failures=FAILURE_CEILING)
    assert apply_roster_plan(plan) == 0
    feed.refresh_from_db()
    assert feed.failures == FAILURE_CEILING
    assert feed.fetched_at is not None
    assert feed.etag == "preserved"
