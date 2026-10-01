"""Publish imported competition data in short passes beside the provider manager.

Publication only touches the database, so it runs in its own worker while the
provider manager keeps requesting. Passes serialize on the ``publication`` lease;
each is a short transaction, so the importer never waits long for a row lock.
"""

from __future__ import annotations

from collections import Counter
from datetime import timedelta
import time
import uuid

from django.utils import timezone

from apps.competition.models import SyncLease
from apps.competition.services.publishing import (
    MatchBounds,
    pending_matches,
    publish_catalogue,
)
from apps.competition.services.schedule_notifications import ScheduleChangeDispatcher
from apps.schedule.models import Season


# Fixtures per pass: short transactions, so imports of the same rows wait briefly.
PUBLICATION_CHUNK = 300
LEASE_SECONDS = 120


def claim_publication() -> uuid.UUID | None:
    """Claim the publication lease, or return None while another pass runs."""
    now, owner = timezone.now(), uuid.uuid4()
    lease, _ = SyncLease.objects.get_or_create(
        key="publication", defaults={"expires_at": now}
    )
    claimed = SyncLease.objects.filter(pk=lease.pk, expires_at__lte=now).update(
        owner=owner, expires_at=now + timedelta(seconds=LEASE_SECONDS)
    )
    return owner if claimed else None


def release_publication(owner: uuid.UUID) -> None:
    """Release the publication lease immediately."""
    SyncLease.objects.filter(key="publication", owner=owner).update(
        owner=None, expires_at=timezone.now()
    )


def publish_backlog(
    *,
    schedule_changes: ScheduleChangeDispatcher,
    live_season: Season | None,
    owner: uuid.UUID,
    deadline: float,
) -> dict[str, object]:
    """Sweep pending fixtures in chunks; the live season goes first every pass.

    Returns:
        Publication counts, passes, and whether pending fixtures remain unswept.

    """
    counts: Counter[str] = Counter()
    conflicts = passes = 0
    cursor: int | None = 0
    while cursor is not None and time.monotonic() < deadline:
        if live_season is not None and pending_matches({live_season.pk}).exists():
            # Live results first: usually a few rows, published in full.
            result = publish_catalogue(
                schedule_changes=schedule_changes,
                bounds=MatchBounds(seasons={live_season.pk}, deadline=deadline),
                alongside_import=True,
            )
            counts.update(result["counts"])
            conflicts += len(result["blocked"])
        result = publish_catalogue(
            schedule_changes=schedule_changes,
            bounds=MatchBounds(
                limit=PUBLICATION_CHUNK, deadline=deadline, after=cursor
            ),
            alongside_import=True,
        )
        passes += 1
        counts.update(result["counts"])
        conflicts += len(result["blocked"])
        cursor = result["last_match"]
        SyncLease.objects.filter(key="publication", owner=owner).update(
            expires_at=timezone.now() + timedelta(seconds=LEASE_SECONDS)
        )
    return {
        "passes": passes,
        "counts": dict(counts),
        "conflicts": conflicts,
        "more": cursor is not None,
        "unpublished": pending_matches().count(),
    }
