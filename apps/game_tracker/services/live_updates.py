"""Durable revision tracking for live match state."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from functools import partial

from django.db import connection, transaction
from django.utils import timezone

from apps.game_tracker.application.ports import MatchChangePublisher
from apps.game_tracker.models import MatchData, MatchLiveChange
from apps.game_tracker.realtime.contracts import ALL_LIVE_RESOURCES, LiveResource


_LIVE_CHANGE_RETENTION = 512


def _record_match_change_in_transaction(
    match_data: MatchData,
    *,
    resources: frozenset[LiveResource],
    changed_ids: Mapping[LiveResource, Iterable[str]],
    publisher: MatchChangePublisher,
) -> int:
    locked = MatchData.objects.select_for_update().filter(pk=match_data.pk).first()
    if locked is None:
        return match_data.live_revision
    locked.live_revision += 1
    locked.live_changed_at = timezone.now()
    locked.save(update_fields=["live_revision", "live_changed_at"])

    MatchLiveChange.objects.create(
        match_data=locked,
        revision=locked.live_revision,
        resources=sorted(resource.value for resource in resources),
        changed_ids={
            resource.value: sorted({str(value) for value in values})
            for resource, values in changed_ids.items()
            if resource in resources
        },
    )

    # Bound storage while retaining ample history for reconnecting clients.
    if locked.live_revision > _LIVE_CHANGE_RETENTION:
        MatchLiveChange.objects.filter(
            match_data=locked,
            revision__lte=locked.live_revision - _LIVE_CHANGE_RETENTION,
        ).delete()

    match_data.live_revision = locked.live_revision
    match_data.live_changed_at = locked.live_changed_at

    transaction.on_commit(
        partial(
            publisher.publish,
            match_id=str(locked.match_link.id_uuid),
            revision=locked.live_revision,
            resources=resources,
        ),
    )
    return locked.live_revision


def record_match_change(
    match_data: MatchData,
    *,
    resources: Iterable[LiveResource] = ALL_LIVE_RESOURCES,
    changed_ids: Mapping[LiveResource, Iterable[str]] | None = None,
    publisher: MatchChangePublisher,
) -> int:
    """Increment a match revision and publish affected resources after commit."""
    normalized = frozenset(resources)
    normalized_changed_ids = changed_ids or {}
    if not normalized:
        return match_data.live_revision

    if connection.in_atomic_block:
        return _record_match_change_in_transaction(
            match_data,
            resources=normalized,
            changed_ids=normalized_changed_ids,
            publisher=publisher,
        )

    with transaction.atomic():
        return _record_match_change_in_transaction(
            match_data,
            resources=normalized,
            changed_ids=normalized_changed_ids,
            publisher=publisher,
        )


@dataclass(frozen=True, slots=True)
class MatchChangeSummary:
    """Resources and complete entity-id deltas for a revision range."""

    resources: frozenset[LiveResource]
    changed_ids: dict[LiveResource, frozenset[str]]
    complete_id_resources: frozenset[LiveResource]
    history_complete: bool


def summarize_match_changes(
    match_data: MatchData,
    *,
    since_revision: int,
) -> MatchChangeSummary:
    """Summarize durable changes through ``match_data.live_revision``."""
    current_revision = match_data.live_revision
    if since_revision > current_revision:
        return MatchChangeSummary(
            frozenset(ALL_LIVE_RESOURCES),
            {},
            frozenset(),
            False,
        )
    if since_revision == current_revision:
        return MatchChangeSummary(frozenset(), {}, frozenset(), True)

    rows = list(
        MatchLiveChange.objects
        .filter(
            match_data=match_data,
            revision__gt=since_revision,
            revision__lte=current_revision,
        )
        .order_by("revision")
        .values("revision", "resources", "changed_ids")
    )
    expected_revisions = list(range(max(1, since_revision + 1), current_revision + 1))
    actual_revisions = [int(row["revision"]) for row in rows]
    if actual_revisions != expected_revisions:
        return MatchChangeSummary(
            frozenset(ALL_LIVE_RESOURCES),
            {},
            frozenset(),
            False,
        )

    resources: set[LiveResource] = set()
    ids: dict[LiveResource, set[str]] = {}
    complete: set[LiveResource] = set(ALL_LIVE_RESOURCES)
    for row in rows:
        row_resources = {
            LiveResource(value)
            for value in row["resources"]
            if value in LiveResource._value2member_map_
        }
        resources.update(row_resources)
        row_changed_ids = row["changed_ids"]
        for resource in row_resources:
            if resource.value not in row_changed_ids:
                complete.discard(resource)
                continue
            ids.setdefault(resource, set()).update(
                str(value) for value in row_changed_ids[resource.value]
            )

    return MatchChangeSummary(
        frozenset(resources),
        {resource: frozenset(values) for resource, values in ids.items()},
        frozenset(resource for resource in complete if resource in resources),
        True,
    )
