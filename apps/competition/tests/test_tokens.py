"""Renewal and rotated-session persistence without real credentials."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
import json
from pathlib import Path
import time
from unittest.mock import Mock, patch

import pytest

from apps.competition.adapters.outbound.sportlink import SportlinkClient, retry_delay
from apps.competition.adapters.outbound.tokens import TokenStore
from apps.competition.application.ports import (
    AuthenticationRequiredError,
    ProviderCooldownError,
)
from apps.competition.models import SyncResource


@pytest.fixture
def store(tmp_path: Path) -> TokenStore:
    """Provide a private fabricated session file."""
    path = tmp_path / "session.json"
    path.write_text(
        json.dumps({
            "client_id": "example-client",
            "user_agent": "synthetic-client",
            "access_token": "old-access",
            "refresh_token": "old-refresh",
            "secret": "example-secret",
        })
    )
    path.chmod(0o600)
    return TokenStore(path)


def renewed_response() -> Mock:
    """Return a synthetic successful OAuth renewal."""
    return Mock(
        status_code=200,
        json=Mock(
            return_value={
                "access_token": "new-access",
                "refresh_token": "new-refresh",
                "expires_in": 3600,
            }
        ),
    )


def test_401_refreshes_once_and_saves_rotated_tokens(store: TokenStore) -> None:
    """Retry the collection with new credentials and retain renewal across runs."""
    client = SportlinkClient(store.access_token, store)
    resource = SyncResource(kind="clubs", etag="")
    success = Mock(status_code=200, headers={}, json=Mock(return_value={"Club": []}))
    with (
        patch.object(
            client.session, "get", side_effect=[Mock(status_code=401), success]
        ) as get,
        patch.object(client.session, "post", return_value=renewed_response()) as post,
    ):
        assert client.fetch(resource).data == {"Club": []}
    assert get.call_count == len(["first", "retry"])
    assert post.call_count == 1
    assert post.call_args.kwargs["data"]["grant_type"] == "refresh_token"
    assert client.session.headers["Authorization"] == "Bearer new-access"
    saved = json.loads(store.path.read_text())
    assert saved["refresh_token"] == "new-refresh"
    assert saved["expires_at"] > time.time()
    assert store.path.stat().st_mode & 0o077 == 0
    client.close()


def test_expired_session_renews_before_first_get(store: TokenStore) -> None:
    """Known expiry avoids an unnecessary unauthorized collection request."""
    store.data["expires_at"] = 0
    client = SportlinkClient(store.access_token, store)
    with (
        patch.object(
            client.session, "get", return_value=Mock(status_code=304, headers={})
        ) as get,
        patch.object(client.session, "post", return_value=renewed_response()) as post,
    ):
        client.fetch(SyncResource(kind="clubs", etag='"v1"'))
    assert get.call_count == post.call_count == 1
    client.close()


def test_revoked_refresh_requires_signin_without_retry_loop(store: TokenStore) -> None:
    """Do not fall back to passwords or repeatedly replay a rejected grant."""
    original = store.path.read_text()
    client = SportlinkClient(store.access_token, store)
    with (
        patch.object(client.session, "get", return_value=Mock(status_code=401)),
        patch.object(
            client.session, "post", return_value=Mock(status_code=400)
        ) as post,
        pytest.raises(AuthenticationRequiredError),
    ):
        client.fetch(SyncResource(kind="clubs", etag=""))
    assert post.call_count == 1
    assert store.path.read_text() == original
    client.close()


def test_public_session_file_is_rejected(store: TokenStore) -> None:
    """Protect refresh credentials from unintended local readers."""
    store.path.chmod(0o644)
    with pytest.raises(ValueError, match="permissions"):
        TokenStore(store.path)


def test_malformed_refresh_does_not_replace_working_file(store: TokenStore) -> None:
    """A malformed renewal leaves the previous credentials recoverable."""
    original = store.path.read_text()
    with pytest.raises(ValueError, match="access token"):
        store.rotate({"refresh_token": "bad"})
    assert store.path.read_text() == original


def test_retry_after_supports_http_dates_and_long_delays() -> None:
    """Respect provider cooldown dates and do not shorten multi-day waits."""
    deadline = datetime.now(UTC) + timedelta(days=2)
    minimum = 172790
    assert retry_delay(format_datetime(deadline, usegmt=True)) >= minimum
    assert retry_delay("172800") == minimum + 10


def test_token_rate_limit_preserves_provider_cooldown(store: TokenStore) -> None:
    """A token endpoint 429 must stop GETs and carry its delay to the scheduler."""
    store.data["expires_at"] = 0
    client = SportlinkClient(store.access_token, store)
    with (
        patch.object(
            client.session,
            "post",
            return_value=Mock(status_code=429, headers={"Retry-After": "1800"}),
        ),
        patch.object(client.session, "get") as get,
        pytest.raises(ProviderCooldownError) as caught,
    ):
        client.fetch(SyncResource(kind="clubs"))
    expected = 1800
    assert caught.value.seconds == expected
    get.assert_not_called()
    client.close()
