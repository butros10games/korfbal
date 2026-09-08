"""Reviewed identity links preserve accounts, ownership and provider checkpoints."""

from datetime import timedelta
import json
from pathlib import Path
from unittest.mock import patch

from django.contrib.auth.models import User
from django.core.management import call_command
from django.utils import timezone
import pytest

from apps.club.models import Club
from apps.competition.models import RosterMembership, SyncLease, SyncResource, Team
from apps.competition.services.importer import Importer
from apps.competition.services.player_linking import link_players
from apps.competition.services.player_photos import photo_name
from apps.competition.services.rosters import withdraw_people
from apps.competition.tests.test_importer import team_payload
from apps.competition.tests.test_player_photos import publish, roster
from apps.game_tracker.tests.tracker_test_helpers import OnCommitCapture
from apps.player.models import Player, PlayerClubMembership
from apps.schedule.models import Season
from apps.team.models import TeamData
from apps.team.models.team import Team as NativeTeam


Pair = tuple[Player, Player, TeamData, list[dict[str, str]]]


@pytest.fixture
def pair(season: Season) -> Pair:
    """Create an account on a native roster and its imported duplicate."""
    importer = Importer(season, timezone.now())
    importer.team(team_payload("T1"))
    importer.apply("team_roster", "T1", roster())
    source = Player.objects.get()
    user = User.objects.create_user(username="Example_P")
    target = Player.all_objects.get(user=user)
    target.stats_visibility = "private"
    target.save()
    club = Club.objects.create(name="Example Club")
    team = NativeTeam.objects.create(name="1", club=club)
    data = TeamData.objects.create(team=team, season=season)
    data.players.add(source, target)
    source_team = Team.objects.get(external_id="T1")
    source_team.local_team_data = data
    source_team.save()
    RosterMembership.objects.filter(player=source).update(
        published_team_data=data, local_link_created=True
    )
    return (
        source,
        target,
        data,
        [{"knkv_person_id": "P1", "account_player_id": str(target.pk)}],
    )


@pytest.mark.django_db
def test_preview_and_merge_preserve_account_and_manual_roster(
    pair: Pair, season: Season
) -> None:
    """A private source withdrawal cannot remove the account's manual membership."""
    source, target, data, links = pair
    assert link_players(links)[0]["status"] == "ready"
    assert Player.all_objects.filter(pk=source.pk).exists()
    assert link_players(links, apply=True)[0]["status"] == "linked"
    target.refresh_from_db()
    assert target.knkv_person_id == "P1"
    assert target.user.username == "Example_P"
    assert target.stats_visibility == "private"
    assert data.players.count() == 1
    assert not RosterMembership.objects.get().local_link_created
    assert link_players(links, apply=True)[0]["status"] == "already_linked"
    withdraw_people({"P1"}, season, timezone.now() + timedelta(seconds=1))
    assert data.players.filter(pk=target.pk).exists()
    assert Player.objects.filter(pk=target.pk).exists()


@pytest.mark.django_db
def test_future_import_reuses_account_and_keeps_name(
    pair: Pair, season: Season
) -> None:
    """Stable KNKV identity prevents another duplicate after a fresh fetch."""
    source, target, _, links = pair
    old_pk = source.pk
    target.name = "My preferred name"
    target.save()
    link_players(links, apply=True)
    Importer(season, timezone.now() + timedelta(seconds=1)).apply(
        "team_roster", "T1", roster()
    )
    assert not Player.all_objects.filter(pk=old_pk).exists()
    assert Player.all_objects.get(knkv_person_id="P1").pk == target.pk
    target.refresh_from_db()
    assert target.name == "My preferred name"


@pytest.mark.django_db
def test_photo_copy_preserves_checkpoint_without_provider_request(
    pair: Pair, django_capture_on_commit_callbacks: OnCommitCapture
) -> None:
    """Cached photos move to the surviving UUID; retries retain their limits."""
    source, target, _, links = pair
    old_name = publish(source)
    resource = SyncResource.objects.get(kind="player_photo")
    resource.failures = 3
    resource.save()
    with django_capture_on_commit_callbacks(execute=True):
        link_players(links, apply=True)
    target.refresh_from_db()
    resource.refresh_from_db()
    assert resource.source_id == str(target.pk)
    expected_failures = 3
    assert resource.failures == expected_failures
    assert target.profile_picture.name == photo_name(target)
    assert target.profile_picture.storage.exists(target.profile_picture.name)
    assert not target.profile_picture.storage.exists(old_name)


@pytest.mark.django_db
def test_native_photo_and_profile_preferences_win(pair: Pair) -> None:
    """Existing account uploads are not overwritten by KNKV."""
    _, target, _, links = pair
    target.profile_picture = "profile_pictures/my-upload.png"
    target.profile_picture_visibility = "private"
    target.save()
    link_players(links, apply=True)
    target.refresh_from_db()
    assert target.profile_picture.name == "profile_pictures/my-upload.png"
    assert target.profile_picture_visibility == "private"
    assert not target.knkv_photo
    assert not SyncResource.objects.filter(kind="player_photo").exists()


@pytest.mark.django_db
def test_active_importer_and_native_source_history_block_merges(pair: Pair) -> None:
    """Do not race the importer or cascade-delete manually recorded history."""
    source, _, data, links = pair
    SyncLease.objects.create(
        key="sportlink", expires_at=timezone.now() + timedelta(minutes=1)
    )
    with pytest.raises(ValueError, match="Stop the competition importer"):
        link_players(links, apply=True)
    SyncLease.objects.all().delete()
    PlayerClubMembership.objects.create(player=source, club=data.team.club)
    with pytest.raises(ValueError, match="native history"):
        link_players(links, apply=True)
    assert Player.all_objects.filter(pk=source.pk).exists()


@pytest.mark.django_db
def test_duplicate_pairs_and_conflicting_identity_rejected(pair: Pair) -> None:
    """A mapping must be one-to-one and may not replace an existing KNKV link."""
    source, target, _, links = pair
    with pytest.raises(ValueError, match="only once"):
        link_players(links + links, apply=True)
    target.knkv_person_id = "other"
    target.save()
    with pytest.raises(ValueError, match="different KNKV identity"):
        link_players(links, apply=True)
    assert Player.all_objects.filter(pk=source.pk).exists()


@pytest.mark.django_db
def test_failure_rolls_back_relations_and_identity(pair: Pair) -> None:
    """Photo storage failure cannot leave a half-merged account."""
    source, target, data, links = pair
    with (
        patch(
            "apps.competition.services.player_linking._move_photo",
            side_effect=OSError("storage unavailable"),
        ),
        pytest.raises(OSError, match="storage unavailable"),
    ):
        link_players(links, apply=True)
    assert Player.all_objects.filter(pk=source.pk, knkv_person_id="P1").exists()
    target.refresh_from_db()
    assert target.knkv_person_id is None
    expected_players = 2
    assert data.players.count() == expected_players
    assert RosterMembership.objects.get().player_id == source.pk


@pytest.mark.django_db
def test_new_season_staff_links_remain_importer_owned(
    pair: Pair, season: Season
) -> None:
    """Keep historical manual links while moving imported staff to their own season."""
    source, target, data, links = pair
    indoor = Season.objects.create(
        name="Indoor example", start_date=season.start_date, end_date=season.end_date
    )
    indoor_data = TeamData.objects.create(team=data.team, season=indoor)
    indoor_data.staff.add(source)
    indoor_data.coach.add(source)
    source_team = Team.objects.get(external_id="T1")
    RosterMembership.objects.filter(player=source).update(
        published_team_data=indoor_data,
        local_link_created=False,
        local_staff_link_created=True,
        local_coach_link_created=True,
    )
    link_players(links, apply=True)
    assert data.players.filter(pk=target.pk).exists()
    assert indoor_data.staff.filter(pk=target.pk).exists()
    assert indoor_data.coach.filter(pk=target.pk).exists()
    assert RosterMembership.objects.get(team=source_team).local_staff_link_created
    withdraw_people({"P1"}, season, timezone.now() + timedelta(seconds=1))
    assert not indoor_data.staff.exists()
    assert not indoor_data.coach.exists()
    assert data.players.filter(pk=target.pk).exists()


@pytest.mark.django_db
def test_command_defaults_to_preview(pair: Pair, tmp_path: Path) -> None:
    """Require --apply even when the private pair file is complete."""
    source, _, _, links = pair
    path = tmp_path / "links.json"
    path.write_text(json.dumps(links))
    call_command("link_competition_players", links=path)
    assert Player.all_objects.filter(pk=source.pk).exists()
