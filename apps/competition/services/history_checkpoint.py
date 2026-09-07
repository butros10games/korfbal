"""Atomic historical publication checkpoints across provider adapters."""

from datetime import timedelta
from typing import Any
from uuid import UUID

from django.db import transaction
from django.utils import timezone

from apps.competition.models import HistoricalResource, SyncLease
from apps.competition.services.history import apply_app
from apps.competition.services.history_dataservice import apply_dataservice


@transaction.atomic
def checkpoint(
    resource: HistoricalResource, data: dict[str, Any], *, owner: UUID | None = None
) -> None:
    """Commit normalized competition data and its progress marker atomically."""
    lease = None
    if owner is not None:
        lease = SyncLease.objects.select_for_update().get(key="sportlink", owner=owner)
    resource.reason = ""
    if resource.provider == "app":
        apply_app(resource, data)
    else:
        apply_dataservice(resource, data)
    if resource.state == "pending":
        resource.state = "fetched"
    if resource.state != "fetched":
        # Split or blocked payloads cannot be reconstructed from an HTTP 304
        # after an operator resets the state for an explicit retry.
        resource.etag = ""
    resource.fetched_at = timezone.now()
    resource.attempts = 0
    resource.save()
    if lease is not None:
        lease.expires_at = timezone.now() + timedelta(seconds=120)
        lease.save(update_fields=("expires_at",))
