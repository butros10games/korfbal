"""Tournament live-revision service tests."""

from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass, field

from django.contrib.auth import get_user_model
from django.utils import timezone
import pytest

from apps.tournament.models import Tournament
from apps.tournament.services.live import touch_tournament


pytestmark = pytest.mark.django_db
OnCommitCapture = Callable[
    ...,
    AbstractContextManager[list[Callable[[], None]]],
]


@dataclass(slots=True)
class RecordingPublisher:
    """Record publications without requiring Channels infrastructure."""

    changes: list[tuple[str, int]] = field(default_factory=list)

    def publish(self, *, tournament_id: str, revision: int) -> None:
        """Record one tournament revision."""
        self.changes.append((tournament_id, revision))


def test_touch_tournament_commits_revision_before_publication(
    django_capture_on_commit_callbacks: OnCommitCapture,
) -> None:
    """The application service publishes only through its injected capability."""
    owner = get_user_model().objects.create_user(username="live-manager")
    tournament = Tournament.objects.create(
        name="Live",
        slug="live",
        owner=owner,
        starts_at=timezone.now(),
    )
    publisher = RecordingPublisher()

    with django_capture_on_commit_callbacks(execute=True):
        revision = touch_tournament(tournament, publisher=publisher)

    tournament.refresh_from_db()
    assert revision == 1
    assert tournament.live_revision == 1
    assert publisher.changes == [(str(tournament.id_uuid), 1)]
