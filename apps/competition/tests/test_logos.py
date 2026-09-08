"""Provider badges reuse native image storage without repeated HTTP or lost uploads."""

import base64
from datetime import timedelta
from http import HTTPStatus
from io import BytesIO
from unittest.mock import Mock, patch

from django.utils import timezone
from PIL import Image
import pytest

from apps.club.models import Club as AppClub
from apps.competition.adapters.outbound.logos import fetch_logo
from apps.competition.adapters.outbound.sportlink import SportlinkClient, retry_delay
from apps.competition.application.ports import FetchResult
from apps.competition.models import Club, SyncLease, SyncResource
from apps.competition.services.importer import Importer
from apps.competition.services.logos import cache_logo, logo_name, publish_logo
from apps.competition.services.publishing import publish_catalogue
from apps.competition.services.sync import sync
from apps.schedule.models import Season


BUCKET = "KNKV-production-REPL"
RETRY_SECONDS = 600
DIGEST = "D256E0B24875DDAE414A2D22D5694468"


def payload() -> dict:
    """Minimal club response with the observed binary reference shape."""
    return {
        "ClubId": "logo-club",
        "ClubName": "Logo Club",
        "ClubLogo": {"Bucket": BUCKET, "Hash": DIGEST},
    }


def image_payload() -> dict:
    """Generate a tiny synthetic PNG rather than copying a real badge."""
    stream = BytesIO()
    Image.new("RGB", (8, 8), "red").save(stream, format="PNG")
    return {
        "name": logo_name(BUCKET, DIGEST),
        "image": base64.b64encode(stream.getvalue()).decode(),
    }


@pytest.mark.django_db
def test_discovery_cache_and_native_publication(season: Season) -> None:
    """A repeated feed queues once and a cached hash never uses the network."""
    importer = Importer(season, timezone.now())
    importer.apply("clubs", "", {"Club": [payload()]})
    importer.apply("clubs", "", {"Club": [payload()]})
    assert SyncResource.objects.filter(kind="club_logo").count() == 1
    cache_logo("logo-club", image_payload())
    publish_catalogue()
    source = Club.objects.get()
    local = AppClub.objects.get()
    assert local.logo.name == source.cached_logo == source.published_logo
    assert local.get_club_logo() == local.logo.url
    gate = Mock()
    with patch("apps.competition.adapters.outbound.logos.requests.get") as request:
        result = fetch_logo(source.external_id, gate, retry_delay)
    assert result.data == {"name": source.cached_logo}
    request.assert_not_called()
    gate.before_request.assert_not_called()


@pytest.mark.django_db
def test_user_upload_survives_import_and_corrections(season: Season) -> None:
    """Both preexisting and later manual images remain authoritative."""
    importer = Importer(season, timezone.now())
    importer.apply("clubs", "", {"Club": [payload()]})
    publish_catalogue()
    local = AppClub.objects.get()
    local.logo = "club_pictures/my-upload.png"
    local.save()
    cache_logo("logo-club", image_payload())
    local.refresh_from_db()
    assert local.logo.name == "club_pictures/my-upload.png"
    source = Club.objects.get()
    source.published_logo = "club_pictures/previous-import.png"
    source.save()
    publish_logo(source)
    local.refresh_from_db()
    assert local.logo.name == "club_pictures/my-upload.png"


@pytest.mark.django_db
def test_changed_reference_requeues_and_invalid_reference_is_ignored(
    season: Season,
) -> None:
    """Only new valid hashes reset a completed logo checkpoint."""
    importer = Importer(season, timezone.now())
    importer.apply("clubs", "", {"Club": [payload()]})
    resource = SyncResource.objects.get(kind="club_logo")
    resource.fetched_at = timezone.now()
    resource.save()
    changed = payload()
    changed["ClubLogo"]["Hash"] = "ABC123"
    importer.apply("clubs", "", {"Club": [changed]})
    resource.refresh_from_db()
    assert resource.fetched_at is None
    changed["ClubLogo"]["Bucket"] = "../../arbitrary-host"
    importer.apply("clubs", "", {"Club": [changed]})
    assert Club.objects.get().logo_bucket == BUCKET


@pytest.mark.django_db
def test_logo_transport_is_paced_and_does_not_forward_credentials(
    season: Season,
) -> None:
    """Use the fixed binary host, disable redirects and preserve rate-limit delays."""
    Importer(season, timezone.now()).apply("clubs", "", {"Club": [payload()]})
    gate = Mock()
    with patch("apps.competition.adapters.outbound.logos.requests.get") as request:
        response = request.return_value.__enter__.return_value
        response.status_code = 429
        response.headers = {"Retry-After": "600"}
        result = fetch_logo("logo-club", gate, retry_delay)
    gate.before_request.assert_called_once()
    assert result.retry_after == RETRY_SECONDS
    assert request.call_args.args[0].startswith("https://binaries.sportlink.com/")
    assert request.call_args.kwargs["allow_redirects"] is False
    assert "headers" not in request.call_args.kwargs


@pytest.mark.django_db
def test_invalid_image_does_not_change_native_logo(season: Season) -> None:
    """Bad image bodies are retryable failures, never published files."""
    Importer(season, timezone.now()).apply("clubs", "", {"Club": [payload()]})
    with pytest.raises(ValueError, match="Invalid club logo"):
        cache_logo(
            "logo-club",
            {
                "name": logo_name(BUCKET, DIGEST),
                "image": base64.b64encode(b"not an image").decode(),
            },
        )
    assert not Club.objects.get().cached_logo


@pytest.mark.django_db
def test_changed_logo_download_updates_previous_import(season: Season) -> None:
    """The adapter and importer replace a badge when its hash changes."""
    importer = Importer(season, timezone.now())
    importer.apply("clubs", "", {"Club": [payload()]})
    publish_catalogue()
    cache_logo("logo-club", image_payload())
    previous = AppClub.objects.get().logo.name
    changed = payload()
    changed["ClubLogo"]["Hash"] = "ABC123"
    importer.apply("clubs", "", {"Club": [changed]})
    gate = Mock()
    with patch("apps.competition.adapters.outbound.logos.requests.get") as request:
        response = request.return_value.__enter__.return_value
        response.status_code = 200
        response.iter_content.return_value = [
            base64.b64decode(image_payload()["image"])
        ]
        result = fetch_logo("logo-club", gate, retry_delay)
    assert result.data is not None
    importer.apply("club_logo", "logo-club", result.data)
    source = Club.objects.get()
    assert source.cached_logo != previous
    assert (
        AppClub.objects.get().logo.name == source.cached_logo == source.published_logo
    )
    gate.before_request.assert_called_once()


@pytest.mark.django_db
@pytest.mark.parametrize("response_status", [200, 401])
def test_authenticated_logo_refresh_is_bounded_and_paced(
    season: Season, response_status: int
) -> None:
    """Use the app session on the verified binary host and renew at most once."""
    Importer(season, timezone.now()).apply("clubs", "", {"Club": [payload()]})
    resource = SyncResource.objects.get(kind="club_logo")
    store = Mock()
    store.data = {"user_agent": "synthetic-app"}
    store.needs_refresh.return_value = False
    store.access_token = "renewed-synthetic"
    client = SportlinkClient("original-synthetic", store=store)
    gate = Mock()
    rejected = Mock(status_code=401)
    response = Mock(status_code=response_status, headers={})
    response.__enter__ = Mock(return_value=response)
    response.__exit__ = Mock(return_value=False)
    response.iter_content.return_value = [base64.b64decode(image_payload()["image"])]
    with (
        patch.object(client.session, "get", side_effect=[rejected, response]) as get,
        patch.object(
            client.session, "post", return_value=Mock(status_code=200)
        ) as post,
    ):
        result = client.fetch(resource, gate)
    assert result.status == response_status
    assert get.call_count == len([rejected, response])
    assert gate.before_request.call_count == len([rejected, post, response])
    post.assert_called_once()
    rejected.close.assert_called_once()
    assert client.session.headers["Authorization"] == "Bearer renewed-synthetic"
    assert get.call_args.args[0] == f"https://binaries.sportlink.com/{BUCKET}/{DIGEST}"
    assert get.call_args.kwargs["stream"] is True
    assert get.call_args.kwargs["allow_redirects"] is False
    client.close()


@pytest.mark.django_db
@pytest.mark.parametrize("status_code", [401, 403, 429])
def test_logo_auth_rejection_is_local_but_rate_limit_is_global(
    season: Season, status_code: int
) -> None:
    """A rejected badge cannot delay unrelated discovery; 429 still stops all work."""
    Importer(season, timezone.now()).apply("clubs", "", {"Club": [payload()]})
    resource = SyncResource.objects.get(kind="club_logo")
    now = timezone.now()
    SyncResource.objects.exclude(pk=resource.pk).update(
        fetched_at=now, next_sync_at=now + timedelta(days=1)
    )
    SyncResource.objects.get_or_create(
        season=season,
        kind="clubs",
        source_id="",
        defaults={"fetched_at": now, "next_sync_at": now + timedelta(days=1)},
    )
    client = Mock()
    client.fetch.return_value = FetchResult(status_code, retry_after=RETRY_SECONDS)
    result = sync(season, client, budget=1)
    assert result["failed"] == 1
    lease = SyncLease.objects.get(key="sportlink")
    if status_code == HTTPStatus.TOO_MANY_REQUESTS:
        assert lease.expires_at >= now + timedelta(seconds=RETRY_SECONDS)
    else:
        assert lease.expires_at <= timezone.now()
    resource.refresh_from_db()
    assert resource.failures == 1
    assert resource.last_error == f"http_{status_code}"


@pytest.mark.django_db
def test_cached_logo_skips_even_expired_session_refresh(season: Season) -> None:
    """A cached immutable badge needs neither OAuth nor another image request."""
    Importer(season, timezone.now()).apply("clubs", "", {"Club": [payload()]})
    cache_logo("logo-club", image_payload())
    store = Mock()
    store.data = {"user_agent": "synthetic"}
    store.needs_refresh.return_value = True
    client = SportlinkClient("synthetic", store=store)
    with (
        patch.object(client.session, "get") as get,
        patch.object(client.session, "post") as post,
    ):
        result = client.fetch(SyncResource.objects.get(kind="club_logo"), Mock())
    assert result.status == HTTPStatus.OK
    get.assert_not_called()
    post.assert_not_called()
    client.close()


@pytest.mark.django_db
def test_untrusted_binary_reference_never_reaches_authenticated_transport(
    season: Season,
) -> None:
    """Reject a malformed stored bucket before invoking the authorized callback."""
    Importer(season, timezone.now()).apply("clubs", "", {"Club": [payload()]})
    Club.objects.update(logo_bucket="../../other-host")
    request = Mock()
    with pytest.raises(ValueError, match="Invalid club logo reference"):
        fetch_logo("logo-club", Mock(), retry_delay, request=request)
    request.assert_not_called()
