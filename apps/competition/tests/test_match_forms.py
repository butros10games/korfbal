"""Synthetic DWF contracts, account isolation and recoverable publication."""

from copy import deepcopy
from datetime import timedelta
from unittest.mock import Mock

from django.contrib.auth.models import User
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
    SyncLease,
)
from apps.competition.services.importer import Importer
from apps.competition.services.match_form_payloads import (
    merge_substitutions,
    publish_players,
)
from apps.competition.services.match_form_worker import discover_finished, drain
from apps.competition.services.match_forms import enqueue, execute
from apps.competition.services.publishing import publish_catalogue
from apps.competition.tests.test_importer import match_payload
from apps.competition.tests.test_rosters import person
from apps.game_tracker.models import (
    GroupType,
    MatchData,
    MatchPart,
    Pause,
    PlayerChange,
    PlayerGroup,
)
from apps.game_tracker.services.match_mutations import MatchRevisionConflictError
from apps.game_tracker.services.player_groups import ensure_player_groups_for_match_data
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
def test_import_adds_reserves_once_preserves_manual_divisions_and_revision(
    scope: tuple[Match, MatchData, MatchFormAccess],
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
    provider.read.return_value["MatchFormTeamPersons"]["MatchFormTeamPerson"][0][
        "Captain"
    ] = True
    execute(job, provider, Mock())
    job.refresh_from_db()
    assert job.captain_player_id == assigned.pk
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
    discover_finished()
    assert not MatchFormSync.objects.exists()
    access.auto_substitutions = True
    access.save()
    source.pool.competition_class.category = "b"
    source.pool.competition_class.save()
    discover_finished()
    assert not MatchFormSync.objects.exists()
    source.pool.competition_class.category = "a"
    source.pool.competition_class.save()
    discover_finished()
    discover_finished()
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
