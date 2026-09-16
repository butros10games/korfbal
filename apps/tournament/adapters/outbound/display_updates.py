"""Build one bounded, public display update per committed publication."""

from __future__ import annotations

import json
from typing import Any

from django.core.cache import caches
from django.db import transaction

from apps.tournament.models import Tournament
from apps.tournament.services.snapshot import build_tournament_snapshot


_MAX_BYTES = 256 * 1024
_MAX_CHANGES = 4096
_PUBLIC_STATUSES = {
    Tournament.Status.PUBLISHED,
    Tournament.Status.LIVE,
    Tournament.Status.FINISHED,
}


def _changes(before: object, after: object, path: tuple[str | int, ...] = ()) -> list:
    if before == after:
        return []
    if (
        isinstance(before, dict)
        and isinstance(after, dict)
        and before.keys() == after.keys()
    ):
        return [
            change
            for key in after
            for change in _changes(before[key], after[key], (*path, key))
        ]
    if (
        isinstance(before, list)
        and isinstance(after, list)
        and len(before) == len(after)
    ):
        return [
            change
            for index, value in enumerate(after)
            for change in _changes(before[index], value, (*path, index))
        ]
    return [[list(path), after]]


def _encode(payload: dict[str, Any]) -> str:
    return json.dumps(payload, separators=(",", ":"))


@transaction.atomic
def build_display_update(tournament_id: str) -> tuple[int, bytes | None]:
    """Read and checkpoint a public snapshot under the aggregate publication lock.

    Cache state only reduces bytes; a missing checkpoint sends a full replacement.
    Holding the aggregate lock serializes checkpoint writers across API workers.
    Private/token-only displays never put their contents on the public channel.
    """
    tournament = Tournament.objects.select_for_update().get(pk=tournament_id)
    revision = tournament.live_revision
    cache = caches["public_live"]
    key = f"tournament-display:v1:{tournament_id}"
    if (
        tournament.visibility != Tournament.Visibility.PUBLIC
        or tournament.status not in _PUBLIC_STATUSES
    ):
        cache.delete(key)
        return revision, None
    snapshot = build_tournament_snapshot(tournament)
    payload = {"version": 1, "tournament_id": tournament_id, "revision": revision}
    full = _encode({**payload, "snapshot": snapshot})
    if len(full.encode()) > _MAX_BYTES:
        cache.delete(key)
        return revision, None
    previous = cache.get(key)
    encoded = full
    if isinstance(previous, dict):
        base = previous.get("tournament", {}).get("live_revision")
        if isinstance(base, int) and base < revision:
            changes = _changes(previous, snapshot)
            patch = _encode({
                **payload,
                "base_revision": base,
                "changes": changes,
            })
            if len(changes) <= _MAX_CHANGES and len(patch.encode()) < len(
                full.encode()
            ):
                encoded = patch
    cache.set(key, snapshot, timeout=120)
    # Encode the SSE frame once, not once for every connected display.
    return revision, f"event: tournament.changed\ndata: {encoded}\n\n".encode()
