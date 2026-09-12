"""Synthetic DWF contracts, account isolation and recoverable publication."""

from copy import deepcopy
from datetime import timedelta
from unittest.mock import Mock, patch

from django.contrib.auth.models import User
from django.db import connection, transaction
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
import pytest
from rest_framework import status
from rest_framework.test import APIClient

from apps.competition.application.match_forms import MatchFormError, MatchFormOptions
from apps.competition.models import (
    CompetitionClass,
    CompetitionEdition,
    Match,
    MatchFormAccess,
    MatchFormSync,
    Pool,
    SyncLease,
)
from apps.competition.services.importer import Importer
from apps.competition.services.match_form_payloads import (
    merge_substitutions,
    player_rows,
    publish_players,
)
from apps.competition.services.match_form_worker import (
    discover,
    drain,
)
from apps.competition.services.match_forms import enqueue, execute, import_is_due
from apps.competition.services.publishing import publish_catalogue
from apps.competition.tasks import discover_match_forms, sync_match_forms
from apps.competition.tests.test_importer import match_payload
from apps.competition.tests.test_rosters import person
from apps.game_tracker.composition import apply_tracker_command
from apps.game_tracker.models import (
    GroupType,
    MatchData,
    MatchPart,
    MatchPlayer,
    Pause,
    PlayerChange,
    PlayerGroup,
)
from apps.game_tracker.realtime.contracts import LiveResource
from apps.game_tracker.services.live_updates import record_match_change
from apps.game_tracker.services.match_mutations import MatchRevisionConflictError
from apps.game_tracker.services.player_groups import ensure_player_groups_for_match_data
from apps.game_tracker.tests.tracker_test_helpers import create_match_part
from apps.player.models import Player
from apps.schedule.models import Season


def form(home: bool = True) -> dict:
    """Provide a synthetic editable player form."""
    return {
        "PublicMatchId": "M1",
        "IsHome": home,
        "Permissions": {"TeamEditAllowed": True},
        "InputForm": {
            "TeamLocked": False,
            "CaptainApproved": False,
            "OverrideWarnings": False,
        },
        "MatchFormTeamPersons": {
            "MatchFormTeamPerson": [
                {**person("P1"), "OnMatchForm": True, "BasePlayer": True},
                {**person("P2"), "OnMatchForm": True, "BasePlayer": True},
            ]
        },
    }


def events_form(events: list[dict]) -> dict:
    """Preserve scores alongside synthetic match events."""
    return {
        "PublicMatchId": "M1",
        "Permissions": {"MatchEventEditHomeTeamAllowed": True},
        "InputForm": {"HomeScore": 22, "AwayScore": 18},
        "MatchFormMatchEvents": {"MatchEvent": events},
    }


def event(client_id: str = "own", team: str = "T1") -> dict:
    """Build an identified substitution."""
    return {
        "ClientEventId": client_id,
        "PublicTeamId": team,
        "TypeOfEvent": 10,
        "PersonId": "P1",
        "OtherPersonId": "P2",
        "OffsetTime": "35",
        "PeriodId": 2,
    }


def test_publish_submits_to_official_without_granting_official_approval() -> None:
    """Submit the team without falsely granting approval on behalf of the official."""
    original = form()
    original["MatchFormTeamPersons"]["MatchFormTeamPerson"][1]["Captain"] = True
    staff = {
        **person("STAFF"),
        "TeamPersonFunction": {"RoleId": "COACHING_STAFF"},
        "OnMatchForm": True,
    }
    original["MatchFormTeamPersons"]["MatchFormTeamPerson"].append(staff)
    updated = publish_players(
        original, True, {"P1": False}, allows_base=False, captain_id="P1"
    )
    assert updated["MatchFormTeamPersons"]["MatchFormTeamPerson"][-1] == staff
    assert updated["InputForm"] == {
        "TeamLocked": False,
        "CaptainApproved": True,
        "OverrideWarnings": False,
    }
    assert (
        updated["MatchFormTeamPersons"]["MatchFormTeamPerson"][0]["BasePlayer"] is True
    )
    assert (
        updated["MatchFormTeamPersons"]["MatchFormTeamPerson"][1]["OnMatchForm"]
        is False
    )
    assert original["InputForm"]["TeamLocked"] is False
    assert [
        row["PersonId"]
        for row in updated["MatchFormTeamPersons"]["MatchFormTeamPerson"]
        if row.get("Captain")
    ] == ["P1"]


@pytest.mark.parametrize("bad", [None, {}, "players"])
def test_invalid_collection_cannot_clear_upstream(bad: object) -> None:
    """Invalid collection cannot clear upstream."""
    payload = form()
    payload["MatchFormTeamPersons"]["MatchFormTeamPerson"] = bad
    with pytest.raises(MatchFormError, match="invalid_response"):
        publish_players(payload, True, {"P1": True}, allows_base=True, captain_id="P1")


def test_merge_is_idempotent_preserves_opponent_and_updates_only_owned_event() -> None:
    """Merge is idempotent preserves opponent and updates only owned event."""
    opponent = {**event("opponent", "T2"), "EventId": 80}
    existing = {**event(), "EventId": 90, "ServerField": "retain"}
    original = events_form([opponent, existing])
    desired = [{**event(), "OffsetTime": "36"}]
    updated = merge_substitutions(
        original, home=True, team_id="T1", desired=desired, owned_ids=["own"]
    )
    assert updated["InputForm"] == original["InputForm"]
    assert updated["MatchFormMatchEvents"]["MatchEvent"] == [
        opponent,
        {**existing, "OffsetTime": "36"},
    ]
    assert (
        merge_substitutions(
            updated, home=True, team_id="T1", desired=desired, owned_ids=["own"]
        )
        == updated
    )
    cleared = merge_substitutions(
        updated, home=True, team_id="T1", desired=[], owned_ids=["own"]
    )
    assert cleared["MatchFormMatchEvents"]["MatchEvent"] == [opponent]


def test_manual_duplicate_and_opponent_id_collision_stop_publication() -> None:
    """Manual duplicate and opponent id collision stop publication."""
    with pytest.raises(MatchFormError, match="manual_substitution_conflict"):
        merge_substitutions(
            events_form([event("manual")]),
            home=True,
            team_id="T1",
            desired=[event()],
            owned_ids=[],
        )
    with pytest.raises(MatchFormError, match="knkv_changed"):
        merge_substitutions(
            events_form([event("own", "T2")]),
            home=True,
            team_id="T1",
            desired=[event()],
            owned_ids=[],
        )


@pytest.fixture
def scope(
    season: Season, django_user_model: type[User]
) -> tuple[Match, MatchData, MatchFormAccess]:
    """Publish a synthetic fixture and bind its local account."""
    importer = Importer(season, timezone.now())
    importer.match(match_payload(), result=False)
    publish_catalogue()
    source = Match.objects.select_related("local_match__home_team").get(
        external_id="M1"
    )
    tracker, _ = MatchData.objects.get_or_create(match_link=source.local_match)
    tracker.status = "upcoming"
    tracker.save()
    for index, name in enumerate(["Aanval", "Verdediging", "Reserve"]):
        GroupType.objects.get_or_create(name=name, defaults={"order": index})
    ensure_player_groups_for_match_data(tracker)
    user = django_user_model.objects.create_user(
        username="synthetic-owner", is_staff=True
    )
    access = MatchFormAccess.objects.create(
        user=user, team=source.local_match.home_team
    )
    tracker.refresh_from_db()
    return source, tracker, access


@pytest.mark.django_db
@pytest.mark.parametrize("read_only", [False, True])
def test_import_adds_reserves_once_preserves_manual_divisions_and_revision(
    scope: tuple[Match, MatchData, MatchFormAccess],
    read_only: bool,
) -> None:
    """Import adds reserves once preserves manual divisions and revision."""
    source, tracker, access = scope
    assigned = Player.objects.create(
        knkv_person_id="P1",
        name="Synthetic player",
        knkv_privacy="OPEN",
        knkv_observed_at=timezone.now(),
    )
    attack = PlayerGroup.objects.get(
        match_data=tracker, team=access.team, starting_type__name="Aanval"
    )
    attack.players.add(assigned)
    tracker.refresh_from_db()
    job = enqueue(access, source.local_match_id, "import", tracker.live_revision)
    provider = Mock()
    provider.read.return_value = form()
    provider.read.return_value["Permissions"] = {
        "TeamEditAllowed": not read_only,
        "TeamViewAllowed": True,
    }
    provider.read.return_value["MatchFormTeamPersons"]["MatchFormTeamPerson"][0][
        "Captain"
    ] = True
    execute(job, provider, Mock())
    job.refresh_from_db()
    assert job.captain_player_id == assigned.pk
    assert (
        MatchPlayer.objects.get(
            match_data=tracker, team=access.team, is_captain=True
        ).player_id
        == assigned.pk
    )
    assert list(attack.players.values_list("knkv_person_id", flat=True)) == ["P1"]
    reserve = PlayerGroup.objects.get(
        match_data=tracker, team=access.team, starting_type__name="Reserve"
    )
    assert list(reserve.players.values_list("knkv_person_id", flat=True)) == ["P2"]
    tracker.refresh_from_db()
    revision = tracker.live_revision
    job.expected_revision = revision
    execute(job, provider, Mock())
    tracker.refresh_from_db()
    assert tracker.live_revision == revision
    assert job.player_count == len({"P1", "P2"})
    provider.replace.assert_not_called()


@pytest.mark.django_db
def test_delayed_import_rejects_changed_revision_and_started_match(
    scope: tuple[Match, MatchData, MatchFormAccess],
) -> None:
    """Delayed import rejects changed revision and started match."""
    source, tracker, access = scope
    job = enqueue(access, source.local_match_id, "import", tracker.live_revision)
    provider = Mock()
    provider.read.return_value = form()
    MatchData.objects.filter(pk=tracker.pk).update(
        live_revision=tracker.live_revision + 1
    )

    with pytest.raises(MatchRevisionConflictError):
        execute(job, provider, Mock())
    assert not Player.objects.filter(knkv_person_id="P1").exists()
    tracker.refresh_from_db()
    job.expected_revision = tracker.live_revision
    MatchData.objects.filter(pk=tracker.pk).update(status="active")
    with pytest.raises(MatchFormError, match="match_started"):
        execute(job, provider, Mock())


@pytest.mark.django_db
def test_api_does_not_lend_owner_credentials_even_to_another_staff_user(
    scope: tuple[Match, MatchData, MatchFormAccess], django_user_model: type[User]
) -> None:
    """Api does not lend owner credentials even to another staff user."""
    source, tracker, access = scope
    api = APIClient()
    url = f"/api/competition/match-forms/{source.local_match_id}/{access.team_id}/"
    assert api.get(url).status_code in {401, 403}
    other = django_user_model.objects.create_user(username="other", is_staff=True)
    api.force_authenticate(other)
    assert api.get(url).data == {"connected": False}
    assert (
        api.post(
            url, {"action": "publish", "expected_revision": tracker.live_revision}
        ).status_code
        == status.HTTP_403_FORBIDDEN
    )
    api.force_authenticate(access.user)
    response = api.post(
        url, {"action": "import", "expected_revision": tracker.live_revision}
    )
    assert response.status_code == status.HTTP_202_ACCEPTED
    assert response["Cache-Control"] == "no-store, private"
    assert (
        api.post(
            url, {"action": "import", "expected_revision": tracker.live_revision}
        ).status_code
        == status.HTTP_202_ACCEPTED
    )
    assert MatchFormSync.objects.count() == 1


@pytest.mark.django_db
def test_worker_waits_for_global_lease_and_retains_failed_jobs(
    scope: tuple[Match, MatchData, MatchFormAccess],
) -> None:
    """Worker waits for global lease and retains failed jobs."""
    source, tracker, access = scope
    job = enqueue(access, source.local_match_id, "import", tracker.live_revision)
    SyncLease.objects.update_or_create(
        key="sportlink", defaults={"expires_at": timezone.now() + timedelta(minutes=1)}
    )
    factory = Mock()
    assert drain(factory, Mock()) == "busy"
    factory.assert_not_called()
    SyncLease.objects.update(expires_at=timezone.now() - timedelta(seconds=1))
    factory.return_value.read.side_effect = MatchFormError("connection_failed")
    assert drain(factory, Mock()) == "pending"
    job.refresh_from_db()
    assert job.error_code == "connection_failed"
    assert job.attempts == 1


@pytest.mark.django_db
def test_publish_checks_provider_permissions_before_write(
    scope: tuple[Match, MatchData, MatchFormAccess],
) -> None:
    """Publish checks provider permissions before write."""
    source, tracker, access = scope
    player = Player.objects.create(
        knkv_person_id="P1",
        name="Synthetic player",
        knkv_privacy="OPEN",
        knkv_observed_at=timezone.now(),
    )
    group = PlayerGroup.objects.get(
        match_data=tracker, team=access.team, starting_type__name="Reserve"
    )
    group.players.add(player)
    tracker.refresh_from_db()
    job = enqueue(
        access,
        source.local_match_id,
        "publish",
        tracker.live_revision,
        options=MatchFormOptions(captain_player_id=player.pk),
    )
    payload = form()
    payload["Permissions"]["TeamEditAllowed"] = False
    provider = Mock()
    provider.read.side_effect = [
        payload,
        {"Details": {"ClassAttributes": {"AllowsBasePlayers": False}}},
    ]
    with pytest.raises(MatchFormError, match="knkv_access_denied"):
        execute(job, provider, Mock())
    provider.replace.assert_not_called()


@pytest.fixture
def finished_scope(
    scope: tuple[Match, MatchData, MatchFormAccess],
) -> tuple[Match, MatchData, MatchFormAccess]:
    """Enable automatic uploads for a classified finished fixture."""
    source, tracker, access = scope
    edition = CompetitionEdition.objects.create(
        season=source.season, discipline="outdoor", phase="autumn", gender="mixed"
    )
    klass = CompetitionClass.objects.create(edition=edition, code="first", category="a")
    source.pool.competition_class = klass
    source.pool.save()
    source.starts_at = timezone.now() - timedelta(hours=2)
    source.save()
    access.auto_substitutions = True
    access.save()
    MatchData.objects.filter(pk=tracker.pk).update(status="finished")
    tracker.refresh_from_db()
    return source, tracker, access


@pytest.mark.django_db
@pytest.mark.parametrize("home", [True, False])
def test_auto_discovery_is_opt_in_a_category_and_runs_once(
    finished_scope: tuple[Match, MatchData, MatchFormAccess], home: bool
) -> None:
    """Auto discovery is opt in a category and runs once."""
    source, _tracker, access = finished_scope
    if not home:
        access.team = source.local_match.away_team
    access.auto_substitutions = False
    access.save()
    discover()
    assert not MatchFormSync.objects.exists()
    source.pool.competition_class.category = "b"
    source.pool.competition_class.save()
    access.auto_substitutions = True
    access.save()
    discover()
    assert not MatchFormSync.objects.exists()
    source.pool.competition_class.category = "a"
    source.pool.competition_class.save()
    discover()
    discover()
    assert MatchFormSync.objects.filter(action="substitutions").count() == 1


@pytest.mark.django_db
def test_substitution_upload_uses_paused_clock_and_reconciles_after_timeout(
    finished_scope: tuple[Match, MatchData, MatchFormAccess],
) -> None:
    """Substitution upload uses paused clock and reconciles after timeout."""
    source, tracker, access = finished_scope
    out = Player.objects.create(
        knkv_person_id="P1",
        name="Synthetic out",
        knkv_privacy="OPEN",
        knkv_observed_at=timezone.now(),
    )
    incoming = Player.objects.create(
        knkv_person_id="P2",
        name="Synthetic in",
        knkv_privacy="OPEN",
        knkv_observed_at=timezone.now(),
    )
    group = PlayerGroup.objects.get(
        match_data=tracker, team=access.team, starting_type__name="Aanval"
    )
    start = timezone.now() - timedelta(minutes=20)
    part = MatchPart.objects.create(match_data=tracker, part_number=2, start_time=start)
    Pause.objects.create(
        match_data=tracker,
        match_part=part,
        start_time=start + timedelta(minutes=2),
        end_time=start + timedelta(minutes=4),
        active=False,
    )
    change = PlayerChange.objects.create(
        match_data=tracker,
        match_part=part,
        player_group=group,
        player_out=out,
        player_in=incoming,
        time=start + timedelta(minutes=7),
    )
    tracker.refresh_from_db()
    job = enqueue(access, source.local_match_id, "substitutions", tracker.live_revision)
    upstream = events_form([event("opponent", "T2")])
    details = {
        "PublicMatchId": "M1",
        "EventTimeResolution": "MINUTE",
        "MatchPeriod": [
            {
                "PeriodId": 7,
                "IsPlayPeriod": True,
                "IsPenaltyTime": False,
                "PlayTime": 30,
            },
            {
                "PeriodId": 9,
                "IsPlayPeriod": True,
                "IsPenaltyTime": False,
                "PlayTime": 30,
            },
        ],
    }
    provider = Mock()
    provider.read.side_effect = lambda resource, *args, **kwargs: deepcopy(
        upstream if resource == "events" else details
    )

    def write_then_timeout(
        resource: str, match_id: str, original: dict, updated: dict, **kwargs: object
    ) -> dict:
        upstream.update(deepcopy(updated))
        raise MatchFormError("connection_failed")

    provider.replace.side_effect = write_then_timeout
    with pytest.raises(MatchFormError, match="connection_failed"):
        execute(job, provider, Mock())
    own = upstream["MatchFormMatchEvents"]["MatchEvent"][1]
    assert own["OffsetTime"] == "35"
    assert own["PeriodId"] == details["MatchPeriod"][1]["PeriodId"]
    assert own["PersonId"] == "P1"
    assert own["OtherPersonId"] == "P2"
    job.refresh_from_db()
    assert own["ClientEventId"] in job.published_event_ids
    provider.replace.side_effect = (
        lambda resource, match_id, original, updated, **kwargs: deepcopy(updated)
    )
    execute(job, provider, Mock())
    assert len(
        provider.replace.call_args.args[3]["MatchFormMatchEvents"]["MatchEvent"]
    ) == len({"own", "opponent"})
    assert job.event_count == 1
    change.delete()
    execute(job, provider, Mock())
    assert provider.replace.call_args.args[3]["MatchFormMatchEvents"]["MatchEvent"] == [
        event("opponent", "T2")
    ]
    assert job.event_count == 0


@pytest.mark.parametrize(
    ("captain", "code"), [("P2", "captain_not_selected"), ("P1", "captain_must_start")]
)
def test_captain_must_be_selected_and_start_when_base_players_are_used(
    captain: str, code: str
) -> None:
    """Apply the KNKV captain validation before sending a form."""
    with pytest.raises(MatchFormError, match=code):
        publish_players(
            form(), True, {"P1": False}, allows_base=True, captain_id=captain
        )


@pytest.fixture
def captain_scope(
    scope: tuple[Match, MatchData, MatchFormAccess],
) -> tuple[Match, MatchData, MatchFormAccess, Player]:
    """Give the bound team a linked starting captain."""
    source, tracker, access = scope
    player = Player.objects.create(
        knkv_person_id="P1",
        name="Synthetic captain",
        knkv_privacy="OPEN",
        knkv_observed_at=timezone.now(),
    )
    PlayerGroup.objects.get(
        match_data=tracker, team=access.team, starting_type__name="Aanval"
    ).players.add(player)
    tracker.refresh_from_db()
    return source, tracker, access, player


@pytest.mark.django_db
@pytest.mark.parametrize("choice", ["missing", "unselected", "opponent"])
def test_api_rejects_captains_outside_this_teams_selection(
    captain_scope: tuple[Match, MatchData, MatchFormAccess, Player], choice: str
) -> None:
    """A private publication cannot nominate an absent or opposing player."""
    source, tracker, access, _player = captain_scope
    data = {"action": "publish", "expected_revision": tracker.live_revision}
    if choice != "missing":
        other = Player.objects.create(
            knkv_person_id="OTHER",
            name="Synthetic other",
            knkv_privacy="OPEN",
            knkv_observed_at=timezone.now(),
        )
        if choice == "opponent":
            PlayerGroup.objects.get(
                match_data=tracker,
                team=source.local_match.away_team,
                starting_type__name="Aanval",
            ).players.add(other)
            tracker.refresh_from_db()
            data["expected_revision"] = tracker.live_revision
        data["captain_player_id"] = str(other.pk)
    api = APIClient()
    api.force_authenticate(access.user)
    response = api.post(
        f"/api/competition/match-forms/{source.local_match_id}/{access.team_id}/", data
    )
    assert response.status_code == status.HTTP_409_CONFLICT
    assert response.data["code"] == (
        "captain_required" if choice == "missing" else "captain_not_selected"
    )
    assert not MatchFormSync.objects.filter(action="publish").exists()


@pytest.mark.django_db
@pytest.mark.parametrize("missing", [None, "captain", "submission"])
def test_publish_persists_selected_captain_and_verifies_provider_readback(
    captain_scope: tuple[Match, MatchData, MatchFormAccess, Player], missing: str | None
) -> None:
    """Verify both the chosen captain and team submission in the provider readback."""
    source, tracker, access, player = captain_scope
    api = APIClient()
    api.force_authenticate(access.user)
    response = api.post(
        f"/api/competition/match-forms/{source.local_match_id}/{access.team_id}/",
        {
            "action": "publish",
            "expected_revision": tracker.live_revision,
            "captain_player_id": str(player.pk),
        },
    )
    assert response.status_code == status.HTTP_202_ACCEPTED
    assert response.data["captain_player_id"] == str(player.pk)
    job = MatchFormSync.objects.get(action="publish")
    assert job.captain_player_id == player.pk
    original = form()
    original["MatchFormTeamPersons"]["MatchFormTeamPerson"][1]["Captain"] = True
    provider = Mock()
    provider.read.side_effect = [
        original,
        {"Details": {"ClassAttributes": {"AllowsBasePlayers": True}}},
    ]

    def replace(
        _resource: str,
        _match_id: str,
        _original: dict,
        updated: dict,
        **_kwargs: object,
    ) -> dict:
        result = deepcopy(updated)
        if missing == "captain":
            result["MatchFormTeamPersons"]["MatchFormTeamPerson"][0]["Captain"] = False
        if missing == "submission":
            result["InputForm"]["CaptainApproved"] = False
        return result

    provider.replace.side_effect = replace
    if missing is None:
        execute(job, provider, Mock())
        sent = provider.replace.call_args.args[3]
        assert [
            r["PersonId"]
            for r in sent["MatchFormTeamPersons"]["MatchFormTeamPerson"]
            if r.get("Captain")
        ] == ["P1"]
        assert sent["InputForm"]["CaptainApproved"] is True
        assert sent["InputForm"]["TeamLocked"] is False
    else:
        with pytest.raises(MatchFormError, match="publication_not_confirmed"):
            execute(job, provider, Mock())


@pytest.mark.parametrize("official_approved", [True, False])
def test_team_submission_preserves_the_officials_existing_approval(
    official_approved: bool,
) -> None:
    """Team submission changes CaptainApproved, never the official-owned TeamLocked."""
    original = form()
    original["InputForm"]["TeamLocked"] = official_approved
    updated = publish_players(
        original, True, {"P1": True}, allows_base=True, captain_id="P1"
    )
    assert updated["InputForm"]["CaptainApproved"] is True
    assert updated["InputForm"]["TeamLocked"] is official_approved
    assert original["InputForm"]["CaptainApproved"] is False


@pytest.mark.django_db
@pytest.mark.parametrize("selected", [True, False])
@pytest.mark.parametrize("duplicate", [True, False])
def test_form_withdraws_private_identity_and_captain(
    scope: tuple[Match, MatchData, MatchFormAccess], selected: bool, duplicate: bool
) -> None:
    """Private observations erase public imports even outside the selected lineup."""
    source, tracker, access = scope
    importer = Importer(source.season, timezone.now())
    importer.apply("team_roster", "T1", {"TeamPersonOverview": [person("P1")]})
    player = Player.objects.get(knkv_person_id="P1")
    reserve = PlayerGroup.objects.get(
        match_data=tracker, team=access.team, starting_type__name="Reserve"
    )
    reserve.players.add(player)
    tracker.refresh_from_db()
    revision = tracker.live_revision
    private = {**person("P1", "PRIVATE"), "OnMatchForm": selected, "Captain": True}
    payload = form()
    payload["MatchFormTeamPersons"]["MatchFormTeamPerson"] = [private]
    if duplicate:
        payload["MatchFormTeamPersons"]["MatchFormTeamPerson"].append({
            **person("P1"),
            "OnMatchForm": True,
            "Captain": True,
        })
    job = enqueue(access, source.local_match_id, "import", revision)
    provider = Mock()
    provider.read.return_value = payload
    execute(job, provider, Mock())
    player.refresh_from_db()
    tracker.refresh_from_db()
    job.refresh_from_db()
    assert not Player.objects.filter(pk=player.pk).exists()
    assert player.knkv_privacy == "PRIVATE"
    assert not player.name
    assert not player.knkv_memberships.exists()
    assert job.captain_player_id is None
    assert job.player_count == 0
    assert tracker.live_revision == revision + 1
    provider.replace.assert_not_called()


@pytest.mark.django_db
@pytest.mark.parametrize("private_first", [True, False])
def test_form_does_not_create_private_identity_from_visible_duplicate(
    scope: tuple[Match, MatchData, MatchFormAccess], private_first: bool
) -> None:
    """A private duplicate wins regardless of response ordering."""
    source, tracker, access = scope
    rows = [
        {**person("P1"), "OnMatchForm": True, "Captain": True},
        {**person("P1", "PRIVATE"), "OnMatchForm": True, "Captain": True},
    ]
    payload = form()
    payload["MatchFormTeamPersons"]["MatchFormTeamPerson"] = (
        rows[::-1] if private_first else rows
    )
    job = enqueue(access, source.local_match_id, "import", tracker.live_revision)
    provider = Mock()
    provider.read.return_value = payload
    execute(job, provider, Mock())
    assert not Player.all_objects.filter(knkv_person_id="P1").exists()
    assert job.captain_player_id is None
    assert job.player_count == 0


@pytest.mark.django_db
@pytest.mark.parametrize("identity", ["unlinked", "private", "duplicate"])
def test_publication_rejects_invalid_selection_before_provider_write(
    captain_scope: tuple[Match, MatchData, MatchFormAccess, Player], identity: str
) -> None:
    """Every selected player must have one usable provider identity."""
    source, tracker, access, captain = captain_scope
    reserve = PlayerGroup.objects.get(
        match_data=tracker, team=access.team, starting_type__name="Reserve"
    )
    player = (
        captain
        if identity == "duplicate"
        else Player.objects.create(
            name="Synthetic reserve",
            knkv_person_id="PRIVATE" if identity == "private" else None,
        )
    )
    reserve.players.add(player)
    tracker.refresh_from_db()
    job = enqueue(
        access,
        source.local_match_id,
        "publish",
        tracker.live_revision,
        options=MatchFormOptions(captain_player_id=captain.pk),
    )
    provider = Mock()
    provider.read.side_effect = [
        form(),
        {"Details": {"ClassAttributes": {"AllowsBasePlayers": True}}},
    ]
    with pytest.raises(MatchFormError, match="players_not_linked"):
        execute(job, provider, Mock())
    provider.replace.assert_not_called()


@pytest.mark.django_db
@pytest.mark.parametrize("changed_upstream", [False, True])
def test_publication_timeout_recovers_after_submission_locks_and_match_starts(
    captain_scope: tuple[Match, MatchData, MatchFormAccess, Player],
    changed_upstream: bool,
) -> None:
    """A committed submission is recognized from durable intent, without another PUT."""
    source, tracker, access, player = captain_scope
    job = enqueue(
        access,
        source.local_match_id,
        "publish",
        tracker.live_revision,
        options=MatchFormOptions(captain_player_id=player.pk),
    )
    provider = Mock()
    current = form()
    provider.read.side_effect = lambda resource, *_args, **_kwargs: (
        deepcopy(current)
        if resource == "players"
        else {"Details": {"ClassAttributes": {"AllowsBasePlayers": True}}}
    )

    def commit_then_timeout(
        _resource: str, _match: str, _original: dict, updated: dict, **_kwargs: object
    ) -> dict:
        current.clear()
        current.update(deepcopy(updated))
        current["Permissions"] = {"TeamEditAllowed": False, "TeamViewAllowed": True}
        raise MatchFormError("connection_failed")

    provider.replace.side_effect = commit_then_timeout
    with pytest.raises(MatchFormError, match="connection_failed"):
        execute(job, provider, Mock())
    job = MatchFormSync.objects.get(pk=job.pk)
    assert set(job.publication_intent) == {"digest", "allows_base"}
    MatchData.objects.filter(pk=tracker.pk).update(status="active", live_revision=999)
    if changed_upstream:
        current["MatchFormTeamPersons"]["MatchFormTeamPerson"][0]["Captain"] = False
        with pytest.raises(MatchFormError, match="knkv_changed"):
            execute(job, provider, Mock())
    else:
        execute(job, provider, Mock())
        assert job.player_count == 1
    assert provider.replace.call_count == 1


@pytest.mark.django_db
@pytest.mark.parametrize("state", ["succeeded", "failed", "pending", "running"])
def test_automatic_corrections_requeue_only_terminal_jobs_on_a_new_revision(
    finished_scope: tuple[Match, MatchData, MatchFormAccess],
    state: str,
) -> None:
    """Retry new revisions once; preserve active jobs and unchanged failures."""
    _source, tracker, _access = finished_scope
    discover()
    job = MatchFormSync.objects.get(action="substitutions")
    job.state = state
    job.published_event_ids = ["previous-owned-event"]
    job.save()
    discover()
    job.refresh_from_db()
    assert job.state == state
    MatchData.objects.filter(pk=tracker.pk).update(
        live_revision=tracker.live_revision + 1
    )
    discover()
    job.refresh_from_db()
    assert job.state == ("pending" if state in {"succeeded", "failed"} else state)
    assert job.expected_revision == tracker.live_revision + (
        state in {"succeeded", "failed"}
    )
    assert job.published_event_ids == ["previous-owned-event"]


@pytest.mark.parametrize("permissions", [{}, {"TeamViewAllowed": False}])
def test_import_requires_at_least_provider_view_permission(permissions: dict) -> None:
    """Read-only import must not bypass provider access control."""
    payload = form()
    payload["Permissions"] = permissions
    with pytest.raises(MatchFormError, match="knkv_access_denied"):
        player_rows(payload, True, editing=False)


@pytest.mark.parametrize(
    ("minutes_before", "last_attempt", "due"),
    [
        (61, None, False),
        (60, None, True),
        (45, None, True),
        (59, 60, False),
        (31, 60, False),
        (30, 60, True),
        (29, 30, False),
        (26, 30, False),
        (25, 30, True),
        (20, 25, True),
        (5, 10, True),
        (1, 5, False),
        (0, 5, False),
    ],
)
def test_import_schedule_boundaries(
    minutes_before: int, last_attempt: int | None, due: bool
) -> None:
    """Late discovery catches up once; retries follow match-relative slots."""
    start = timezone.now()
    job = (
        None
        if last_attempt is None
        else MatchFormSync(
            state="failed",
            updated_at=start - timedelta(minutes=last_attempt),
        )
    )
    assert import_is_due(start, start - timedelta(minutes=minutes_before), job) is due


@pytest.mark.django_db
def test_scheduled_imports_retry_empty_results_then_stop_after_players_arrive(
    scope: tuple[Match, MatchData, MatchFormAccess],
) -> None:
    """The server schedules imports without browser requests and stops on success."""
    source, _tracker, _access = scope
    start = timezone.now() + timedelta(hours=1)
    source.starts_at = start
    source.save()
    first_minute, success_minute = 60, 25
    for minute, should_queue in [
        (60, True),
        (40, False),
        (30, True),
        (26, False),
        (25, True),
        (20, False),
    ]:
        now = start - timedelta(minutes=minute)
        with patch("django.utils.timezone.now", return_value=now):
            discover()
        job = MatchFormSync.objects.get(action="import")
        assert (job.state == "pending") is should_queue
        assert job.automatic is True
        if should_queue:
            job.state = "failed" if minute == first_minute else "succeeded"
            job.player_count = 12 if minute == success_minute else 0
            job.updated_at = now
            job.save()
    assert MatchFormSync.objects.count() == 1


@pytest.mark.django_db
def test_scheduled_import_expiring_in_queue_does_not_contact_knkv(
    scope: tuple[Match, MatchData, MatchFormAccess],
) -> None:
    """A delayed job cannot import after the scheduled kickoff."""
    source, tracker, access = scope
    source.starts_at = timezone.now() + timedelta(minutes=30)
    source.save()
    job = enqueue(
        access,
        source.local_match_id,
        "import",
        tracker.live_revision,
        options=MatchFormOptions(automatic=True),
    )
    provider = Mock()
    with (
        patch("django.utils.timezone.now", return_value=source.starts_at),
        pytest.raises(MatchFormError, match="import_not_due"),
    ):
        execute(job, provider, Mock())
    provider.read.assert_not_called()


@pytest.mark.parametrize("automatic", [True, False])
@pytest.mark.django_db
def test_scheduled_import_failure_uses_schedule_instead_of_transport_retry(
    scope: tuple[Match, MatchData, MatchFormAccess],
    automatic: bool,
) -> None:
    """Scheduled failures await the next slot; manual requests retain retries."""
    source, tracker, access = scope
    job = enqueue(access, source.local_match_id, "import", tracker.live_revision)
    job.automatic = automatic
    job.save()
    source.starts_at = timezone.now() + timedelta(minutes=30)
    source.save()
    provider = Mock()
    provider.read.side_effect = MatchFormError("connection_failed")
    drain(lambda _gate: provider, Mock())
    job.refresh_from_db()
    assert job.state == ("failed" if automatic else "pending")


@pytest.mark.django_db
def test_manual_import_can_repeat_without_page_refresh_cooldown(
    scope: tuple[Match, MatchData, MatchFormAccess],
) -> None:
    """Explicit requests remain available immediately after a completed import."""
    source, tracker, access = scope
    job = enqueue(access, source.local_match_id, "import", tracker.live_revision)
    job.state = "succeeded"
    job.save()
    repeated = enqueue(access, source.local_match_id, "import", tracker.live_revision)
    assert repeated.state == "pending"
    assert not repeated.automatic


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("minutes_before", "retry_before"), [(60, 30), (45, 30), (30, 25), (26, 25), (5, 0)]
)
def test_abandoned_import_waits_until_the_next_slot(
    scope: tuple[Match, MatchData, MatchFormAccess],
    minutes_before: int,
    retry_before: int,
) -> None:
    """A crash waits for the next slot; the final slot expires at kickoff."""
    source, tracker, access = scope
    now = timezone.now()
    source.starts_at = now + timedelta(minutes=minutes_before)
    Match.objects.filter(pk=source.pk).update(starts_at=source.starts_at)
    provider = Mock()
    provider.read.side_effect = KeyboardInterrupt
    with patch("django.utils.timezone.now", return_value=now):
        job = enqueue(
            access,
            source.local_match_id,
            "import",
            tracker.live_revision,
            options=MatchFormOptions(automatic=True),
        )
        with pytest.raises(KeyboardInterrupt):
            drain(lambda _gate: provider, Mock())
    job.refresh_from_db()
    assert job.state == "running"
    assert job.next_attempt_at == source.starts_at - timedelta(minutes=retry_before)


@pytest.mark.django_db
@pytest.mark.parametrize("minutes_before", [61, 0])
def test_automatic_enqueue_outside_window_leaves_no_job(
    scope: tuple[Match, MatchData, MatchFormAccess], minutes_before: int
) -> None:
    """Enqueue itself enforces the window without relying on discovery filters."""
    source, tracker, access = scope
    now = timezone.now()
    source.starts_at = now + timedelta(minutes=minutes_before)
    source.save()
    with (
        patch("django.utils.timezone.now", return_value=now),
        pytest.raises(MatchFormError, match="import_not_due"),
    ):
        enqueue(
            access,
            source.local_match_id,
            "import",
            tracker.live_revision,
            options=MatchFormOptions(automatic=True),
        )
    assert not MatchFormSync.objects.exists()


@pytest.mark.django_db
@pytest.mark.parametrize("enabled", [True, False])
def test_discovery_handles_upcoming_and_finished_matches_for_one_account(
    finished_scope: tuple[Match, MatchData, MatchFormAccess], enabled: bool
) -> None:
    """One discovery pass handles both actions, only for enabled accounts."""
    finished, _tracker, access = finished_scope
    access.enabled = enabled
    access.save()
    payload = match_payload()
    payload.update(
        PublicMatchId="M2",
        MatchDateTime=(timezone.now() + timedelta(minutes=45)).isoformat(),
        Status="SCHEDULED",
    )
    payload["Pool"] = {**payload["Pool"], "PoolId": 11}
    Importer(finished.season, timezone.now()).match(payload, result=False)
    publish_catalogue()
    upcoming = Match.objects.get(external_id="M2")
    MatchData.objects.update_or_create(
        match_link=upcoming.local_match, defaults={"status": "upcoming"}
    )
    discover()
    assert set(MatchFormSync.objects.values_list("match_id", "action")) == (
        {
            (finished.local_match_id, "substitutions"),
            (upcoming.local_match_id, "import"),
        }
        if enabled
        else set()
    )


@pytest.mark.django_db
@pytest.mark.parametrize("action", ["import", "substitutions"])
@pytest.mark.parametrize("match_count", [1, 8])
def test_idle_discovery_query_count_does_not_grow_with_matches(
    finished_scope: tuple[Match, MatchData, MatchFormAccess],
    action: str,
    match_count: int,
) -> None:
    """Idle polling reads receipts in bulk and does not lock settled matches."""
    source, _tracker, access = finished_scope
    now = timezone.now()
    start = (
        now + timedelta(minutes=45) if action == "import" else now - timedelta(hours=2)
    )
    source.starts_at = start
    source.save()
    for index in range(1, match_count):
        payload = match_payload()
        payload.update(
            PublicMatchId=f"M{index + 1}",
            MatchDateTime=(start - timedelta(minutes=index)).isoformat(),
        )
        payload["Pool"] = {**payload["Pool"], "PoolId": 11}
        Importer(source.season, now).match(payload, result=False)
    publish_catalogue()
    Pool.objects.filter(external_id="11").update(
        competition_class_id=source.pool.competition_class_id
    )
    MatchData.objects.update(status="upcoming" if action == "import" else "finished")
    trackers = MatchData.objects.all()
    assert trackers.count() == match_count
    MatchFormSync.objects.all().delete()
    MatchFormSync.objects.bulk_create([
        MatchFormSync(
            access=access,
            match_id=tracker.match_link_id,
            action=action,
            state="succeeded",
            player_count=12,
            expected_revision=tracker.live_revision,
            updated_at=now,
        )
        for tracker in trackers
    ])
    with CaptureQueriesContext(connection) as queries:
        discover()
    expected_queries = 3  # Account bindings, candidate matches, and their receipts.
    assert len(queries) == expected_queries
    assert all(query["sql"].lstrip().upper().startswith("SELECT") for query in queries)
    assert all("FOR UPDATE" not in query["sql"].upper() for query in queries)


@pytest.mark.django_db
def test_discovery_does_not_use_another_teams_receipt(
    scope: tuple[Match, MatchData, MatchFormAccess],
) -> None:
    """A settled opponent receipt cannot suppress this team's import."""
    source, tracker, access = scope
    source.starts_at = timezone.now() + timedelta(minutes=30)
    source.save()
    opponent = MatchFormAccess.objects.create(
        user=access.user, team=source.local_match.away_team, enabled=False
    )
    MatchFormSync.objects.create(
        access=opponent,
        match_id=source.local_match_id,
        action="import",
        state="succeeded",
        player_count=12,
        expected_revision=tracker.live_revision,
    )
    discover()
    own_job = MatchFormSync.objects.get(access=access)
    assert own_job.state == "pending"
    assert own_job.action == "import"


@pytest.mark.django_db
def test_form_enqueue_wakes_after_commit_and_rollback_is_silent(
    scope: tuple[Match, MatchData, MatchFormAccess],
) -> None:
    """A manual form action does not wait for timed discovery."""
    source, tracker, access = scope
    with patch("apps.competition.tasks.sync_match_forms.apply_async") as publish:
        with TestCase.captureOnCommitCallbacks(execute=True):
            enqueue(access, source.local_match_id, "import", tracker.live_revision)
            publish.assert_not_called()
        publish.assert_called_once_with(countdown=0, expires=300)
        publish.reset_mock()
        MatchFormSync.objects.all().delete()
        with TestCase.captureOnCommitCallbacks(execute=True), transaction.atomic():
            enqueue(access, source.local_match_id, "import", tracker.live_revision)
            transaction.set_rollback(True)
        publish.assert_not_called()


@pytest.mark.django_db
def test_form_drain_does_not_scan_discovery(
    scope: tuple[Match, MatchData, MatchFormAccess],
) -> None:
    """Executing a queued action never scans all enabled teams and fixtures."""
    with patch("apps.competition.services.match_form_worker.discover") as discovery:
        assert drain(Mock(), Mock()) == "idle"
        discovery.assert_not_called()


@pytest.mark.django_db
@pytest.mark.parametrize("result", ["succeeded", "busy"])
def test_form_worker_continues_backlog_but_does_not_spin_on_busy_provider(
    scope: tuple[Match, MatchData, MatchFormAccess],
    result: str,
) -> None:
    """Due work continues directly; a provider lease is left to recovery."""
    source, tracker, access = scope
    enqueue(access, source.local_match_id, "import", tracker.live_revision)
    with (
        patch("apps.competition.tasks.run_match_form_queue", return_value=result),
        patch("apps.competition.tasks.sync_match_forms.apply_async") as publish,
    ):
        assert sync_match_forms.run() == result
        assert publish.call_count == (0 if result == "busy" else 1)


@pytest.mark.django_db
def test_discovery_recovers_a_committed_form_job_without_a_message(
    scope: tuple[Match, MatchData, MatchFormAccess],
) -> None:
    """A lost after-commit wakeup cannot permanently strand a private form action."""
    source, tracker, access = scope
    enqueue(access, source.local_match_id, "import", tracker.live_revision)
    with (
        patch("apps.competition.tasks.discover"),
        patch("apps.competition.tasks.sync_match_forms.apply_async") as publish,
    ):
        discover_match_forms.run()
        publish.assert_called_once_with(expires=300)


@pytest.mark.django_db
@pytest.mark.parametrize("rollback", [False, True])
def test_finishing_match_records_substitutions_with_final_revision(
    finished_scope: tuple[Match, MatchData, MatchFormAccess],
    rollback: bool,
) -> None:
    """Finish persists the intent atomically and publishes only after commit."""
    source, tracker, _access = finished_scope
    tracker.status, tracker.parts = "active", 1
    tracker.save(update_fields=["status", "parts"])
    create_match_part(match_data=tracker)
    with patch("apps.competition.tasks.sync_match_forms.apply_async") as publish:
        with TestCase.captureOnCommitCallbacks(execute=True), transaction.atomic():
            apply_tracker_command(
                source.local_match,
                team=source.local_match.home_team,
                payload={"command": "part_end"},
            )
            tracker.refresh_from_db()
            job = MatchFormSync.objects.get(action="substitutions")
            assert job.expected_revision == tracker.live_revision
            assert job.state == "pending"
            publish.assert_not_called()
            transaction.set_rollback(rollback)
        assert (
            MatchFormSync.objects.filter(action="substitutions").exists()
            is not rollback
        )
        assert publish.called is not rollback


@pytest.mark.django_db
def test_finished_match_correction_requeues_without_discovery_tick(
    finished_scope: tuple[Match, MatchData, MatchFormAccess],
) -> None:
    """A later editor revision creates fresh work while retaining provider receipts."""
    source, tracker, access = finished_scope
    job = enqueue(access, source.local_match_id, "substitutions", tracker.live_revision)
    job.state, job.published_event_ids = "succeeded", ["owned-event"]
    job.save()
    record_match_change(tracker, resources=[LiveResource.EVENTS], publisher=Mock())
    job.refresh_from_db()
    assert job.state == "pending"
    assert job.expected_revision == tracker.live_revision
    assert job.published_event_ids == ["owned-event"]


@pytest.mark.django_db
def test_correction_during_provider_upload_gets_a_successor(
    finished_scope: tuple[Match, MatchData, MatchFormAccess],
) -> None:
    """Changes after snapshot capture survive an in-flight upload's completion."""
    source, tracker, access = finished_scope
    job = enqueue(access, source.local_match_id, "substitutions", tracker.live_revision)

    def upload(*args: object) -> None:
        record_match_change(tracker, resources=[LiveResource.EVENTS], publisher=Mock())

    with patch(
        "apps.competition.services.match_form_worker.execute", side_effect=upload
    ):
        assert drain(Mock(), Mock()) == "succeeded"
    job.refresh_from_db()
    assert job.state == "pending"
    assert job.expected_revision == tracker.live_revision


@pytest.mark.django_db
def test_enabling_team_and_rescheduling_fixture_queue_due_work(
    finished_scope: tuple[Match, MatchData, MatchFormAccess],
) -> None:
    """Connection and fixture changes do not wait for global discovery."""
    source, tracker, access = finished_scope
    access.save()
    assert MatchFormSync.objects.get(action="substitutions").state == "pending"
    MatchFormSync.objects.all().delete()
    MatchData.objects.filter(pk=tracker.pk).update(status="upcoming")
    source.starts_at = timezone.now() + timedelta(minutes=45)
    source.save(update_fields=["starts_at"])
    assert MatchFormSync.objects.get(action="import").automatic is True


@pytest.mark.django_db
@pytest.mark.parametrize("restriction", ["disabled", "opt_out", "category_b"])
def test_finish_event_preserves_substitution_opt_in_and_category(
    finished_scope: tuple[Match, MatchData, MatchFormAccess],
    restriction: str,
) -> None:
    """Local events do not bypass the existing team authorization policy."""
    source, tracker, access = finished_scope
    if restriction == "category_b":
        source.pool.competition_class.category = "b"
        source.pool.competition_class.save()
    else:
        MatchFormAccess.objects.filter(pk=access.pk).update(**{
            "enabled" if restriction == "disabled" else "auto_substitutions": False
        })
    record_match_change(tracker, resources=[LiveResource.EVENTS], publisher=Mock())
    assert not MatchFormSync.objects.exists()


@pytest.mark.django_db
def test_statistics_revisions_do_not_trigger_substitution_uploads(
    finished_scope: tuple[Match, MatchData, MatchFormAccess],
) -> None:
    """Both event dispatch and recovery ignore derived-only revisions."""
    source, tracker, access = finished_scope
    job = enqueue(access, source.local_match_id, "substitutions", tracker.live_revision)
    job.state = "succeeded"
    job.save()
    record_match_change(
        tracker,
        resources=[LiveResource.STATS, LiveResource.IMPACTS],
        publisher=Mock(),
    )
    discover()
    job.refresh_from_db()
    assert job.state == "succeeded"
    assert job.expected_revision == tracker.live_revision


@pytest.mark.django_db
def test_publishing_historical_fixture_does_not_upload_old_substitutions(
    finished_scope: tuple[Match, MatchData, MatchFormAccess],
) -> None:
    """Event triggers preserve the existing recent-match eligibility window."""
    source, _tracker, _access = finished_scope
    source.starts_at = timezone.now() - timedelta(days=30)
    source.save(update_fields=["starts_at"])
    assert not MatchFormSync.objects.exists()
