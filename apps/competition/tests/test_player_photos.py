"""Native photo publication, request reuse and privacy withdrawal regressions."""

import base64
from datetime import timedelta
from unittest.mock import MagicMock, Mock, patch

from django.utils import timezone
import pytest

from apps.competition.adapters.outbound.sportlink import SportlinkClient
from apps.competition.models import SyncResource
from apps.competition.services.importer import Importer
from apps.competition.services.player_photos import cache_photo, photo_name
from apps.competition.tests.test_importer import team_payload
from apps.competition.tests.test_logos import BUCKET, DIGEST, image_payload
from apps.competition.tests.test_rosters import person
from apps.game_tracker.tests.tracker_test_helpers import OnCommitCapture
from apps.player.models import Player
from apps.schedule.models import Season


def roster(privacy: str = "NORMAL", digest: str = DIGEST) -> dict:
    """Use synthetic identities and a validated binary reference."""
    row = person(privacy=privacy)
    row["Photo"] = {"Bucket": BUCKET, "Hash": digest}
    return {"TeamPersonOverview": [row]}


def seed(season: Season) -> tuple[Importer, Player]:
    """Discover a photo through the ordinary roster dispatch."""
    importer = Importer(season, timezone.now())
    importer.team(team_payload("T1"))
    importer.apply("team_roster", "T1", roster())
    return importer, Player.objects.get()


def publish(player: Player) -> str:
    """Publish a small generated PNG via the normal importer image service."""
    name = photo_name(player)
    cache_photo(str(player.pk), {**image_payload(), "name": name})
    player.refresh_from_db()
    return name


@pytest.mark.django_db
def test_native_photo_cache_and_variant_deduplication(season: Season) -> None:
    """One native image and queue entry serve repeated and indoor/outdoor feeds."""
    importer, player = seed(season)
    importer.team(team_payload("T2"))
    importer.apply("team_roster", "T2", roster())
    assert SyncResource.objects.filter(kind="player_photo").count() == 1
    name = publish(player)
    assert player.profile_picture.name == name
    assert player.profile_picture.storage.exists(name)
    assert player.get_profile_picture() == player.profile_picture.url
    client = SportlinkClient("synthetic", user_agent="synthetic")
    with patch.object(client, "_logo_get") as get:
        result = client.fetch(SyncResource.objects.get(kind="player_photo"), Mock())
    assert result.data == {"name": name}
    get.assert_not_called()
    client.close()


@pytest.mark.django_db
@pytest.mark.parametrize("privacy", ["LIMITED", "PRIVATE", "UNKNOWN"])
def test_withdrawal_erases_file_and_prevents_pending_download(
    season: Season,
    privacy: str,
    django_capture_on_commit_callbacks: OnCommitCapture,
) -> None:
    """Name visibility never implies permission to retain the photo."""
    importer, player = seed(season)
    name = publish(player)
    feed = SyncResource.objects.get(kind="player_photo")
    with django_capture_on_commit_callbacks(execute=True):
        importer.apply("team_roster", "T1", roster(privacy))
    player.refresh_from_db()
    assert not player.profile_picture
    assert not player.knkv_photo
    assert not player.profile_picture.storage.exists(name)
    client = SportlinkClient("synthetic", user_agent="synthetic")
    with patch.object(client, "_logo_get") as get:
        client.fetch(feed, Mock())
    get.assert_not_called()
    # An in-flight response cannot restore a withdrawn file.
    cache_photo(str(player.pk), {**image_payload(), "name": name})
    assert not player.profile_picture.storage.exists(name)
    client.close()


@pytest.mark.django_db
def test_changed_photo_resets_only_changed_reference(season: Season) -> None:
    """Unchanged rosters preserve bounded retries; new hashes get a fresh attempt."""
    importer, player = seed(season)
    old_name = photo_name(player)
    feed = SyncResource.objects.get(kind="player_photo")
    feed.failures = 6
    feed.fetched_at = timezone.now()
    feed.save()
    importer.apply("team_roster", "T1", roster())
    feed.refresh_from_db()
    max_attempts = 6
    assert feed.failures == max_attempts
    importer.apply("team_roster", "T1", roster(digest="ABC123"))
    feed.refresh_from_db()
    player.refresh_from_db()
    assert feed.failures == 0
    assert feed.fetched_at is None
    cache_photo(str(player.pk), {**image_payload(), "name": old_name})
    player.refresh_from_db()
    assert not player.profile_picture
    publish(player)
    assert player.profile_picture.name != old_name


@pytest.mark.django_db
def test_native_upload_wins_during_download(season: Season) -> None:
    """The importer never overwrites or erases an independently uploaded image."""
    importer, player = seed(season)
    name = photo_name(player)
    player.profile_picture = "profile_pictures/manual.png"
    player.save()
    cache_photo(str(player.pk), {**image_payload(), "name": name})
    importer.apply("team_roster", "T1", roster("LIMITED"))
    player.refresh_from_db()
    assert player.profile_picture.name == "profile_pictures/manual.png"


@pytest.mark.django_db
def test_stale_identity_does_not_download(season: Season) -> None:
    """Queued work needs a current visible roster observation."""
    _, player = seed(season)
    Player.all_objects.filter(pk=player.pk).update(
        knkv_observed_at=timezone.now() - timedelta(days=9)
    )
    client = SportlinkClient("synthetic", user_agent="synthetic")
    with patch.object(client, "_logo_get") as get:
        client.fetch(SyncResource.objects.get(kind="player_photo"), Mock())
    get.assert_not_called()
    client.close()


@pytest.mark.django_db
@pytest.mark.parametrize("privacy", ["LIMITED", "PRIVATE", "UNKNOWN"])
def test_non_photo_privacy_never_queues(season: Season, privacy: str) -> None:
    """Do not fan out binary requests merely because a Photo field is present."""
    importer = Importer(season, timezone.now())
    importer.team(team_payload("T1"))
    importer.apply("team_roster", "T1", roster(privacy))
    assert not SyncResource.objects.filter(kind="player_photo").exists()


@pytest.mark.django_db
def test_authorized_download_publishes_through_dispatch(season: Season) -> None:
    """Player photos reuse the authenticated, rate-gated binary request path."""
    importer, player = seed(season)
    client = SportlinkClient("synthetic", user_agent="synthetic")
    gate = Mock()
    response = MagicMock()
    response.__enter__.return_value = response
    response.status_code = 200
    response.iter_content.return_value = [base64.b64decode(image_payload()["image"])]
    with patch.object(client, "_logo_get", return_value=response) as get:
        result = client.fetch(SyncResource.objects.get(kind="player_photo"), gate)
    get.assert_called_once_with(
        f"https://binaries.sportlink.com/{BUCKET}/{DIGEST}", gate
    )
    assert result.data is not None
    importer.apply("player_photo", str(player.pk), result.data)
    player.refresh_from_db()
    assert player.profile_picture.name == photo_name(player)
    client.close()


@pytest.mark.django_db
def test_fresh_roster_requeues_previously_skipped_photo(season: Season) -> None:
    """A stale-identity skip does not defer a newly eligible image for years."""
    importer, _ = seed(season)
    resource = SyncResource.objects.get(kind="player_photo")
    resource.fetched_at = timezone.now()
    resource.next_sync_at = timezone.now() + timedelta(days=365)
    resource.save()
    importer.apply("team_roster", "T1", roster())
    resource.refresh_from_db()
    assert resource.fetched_at is None
    assert resource.next_sync_at <= timezone.now()
