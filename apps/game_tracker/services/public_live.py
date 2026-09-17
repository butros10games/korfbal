"""Small public score/clock snapshots, independent of private tracker state."""

from contextlib import suppress
from copy import deepcopy
from functools import partial
from time import time
from typing import Any

from django.db import connection, models
from django.utils import timezone

from apps.game_tracker.application.ports import (
    PublicLiveStoreError,
    PublishedLiveStore,
)
from apps.game_tracker.models import MatchData, MatchLiveChange, Shot
from apps.game_tracker.realtime.contracts import ALL_LIVE_RESOURCES, LiveResource
from apps.game_tracker.services.timeline_reads import consistent_timeline_read
from apps.game_tracker.services.tracker_clock_queries import read_clock_state
from apps.game_tracker.services.tracker_commands.base import current_part


def _build_public_snapshot(match_data: MatchData) -> dict[str, Any]:
    match = match_data.match_link
    if match_data.status == "finished" and match_data.score_source in {
        "knkv",
        "archive",
    }:
        home, away = match_data.home_score, match_data.away_score
    else:
        totals = dict(
            Shot.objects
            .filter(match_data=match_data, scored=True)
            .values("team_id")
            .annotate(total=models.Count("pk"))
            .values_list("team_id", "total")
        )
        home, away = (
            totals.get(match.home_team_id, 0),
            totals.get(match.away_team_id, 0),
        )
    part = current_part(match_data)
    paused, timer = read_clock_state(match_data, part)
    # Published payloads contain only revision-stable fields. Render time is fresh.
    timer.pop("server_time", None)
    return {
        "match_id": str(match.pk),
        "match_data_id": str(match_data.pk),
        "status": match_data.status,
        "current_part": match_data.current_part,
        "parts": match_data.parts,
        "paused": paused,
        "timer": timer,
        "score": {"home": home, "away": away},
        "last_changed_at": match_data.live_changed_at.isoformat(),
        "live_revision": match_data.live_revision,
    }


def build_published_live(match_id: str) -> dict[str, Any] | None:
    """Build public state and bounded resource history from one database snapshot."""
    created_at = time()
    with consistent_timeline_read():
        match_data = (
            MatchData.objects
            .select_related("match_link")
            .fetch_mode(models.FETCH_RAISE)
            .filter(match_link_id=match_id)
            .first()
        )
        if match_data is None:
            return None
        payload = _build_public_snapshot(match_data)
        history = list(
            MatchLiveChange.objects
            .filter(match_data=match_data, revision__lte=match_data.live_revision)
            .order_by("-revision")
            .values("revision", "resources")[:512]
        )
    history.reverse()
    history_base = history[0]["revision"] - 1 if history else match_data.live_revision
    expected_revision = history_base + 1
    for row in history:
        if row["revision"] != expected_revision:
            history_base = row["revision"] - 1
        expected_revision = row["revision"] + 1
    if not history or history[-1]["revision"] != match_data.live_revision:
        history_base = match_data.live_revision
    # Readers need the last affected revision per resource, not 512 repeated lists.
    resource_revisions = {
        resource: row["revision"]
        for row in history
        for resource in row["resources"]
        if resource in LiveResource._value2member_map_
    }
    return {
        "revision": payload["live_revision"],
        "created_at": created_at,
        "payload": payload,
        "history_base": history_base,
        "resource_revisions": resource_revisions,
    }


def publish_public_live(*, match_id: str, store: PublishedLiveStore) -> None:
    """Publish committed state; storage failures propagate to durable job recovery."""
    envelope = build_published_live(match_id)
    if envelope is not None:
        store.put(match_id, envelope)


def read_published_live(
    *, match_id: object, store: PublishedLiveStore, since_revision: int | None = None
) -> dict[str, Any] | None:
    """Read shared public state without SQL; recover authoritatively on a miss."""
    match_key = str(match_id)
    shared = not connection.in_atomic_block
    envelope = None
    if not shared and since_revision is not None and since_revision >= 0:
        # Caller-owned transactions cannot use the shared cache. An unchanged
        # response needs only one metadata read, not score/clock/history queries.
        current = (
            MatchData.objects
            .filter(match_link_id=match_key)
            .values("live_revision", "live_changed_at")
            .first()
        )
        if current is not None and current["live_revision"] == since_revision:
            return {
                "changed": False,
                "server_time": timezone.now().isoformat(),
                "last_changed_at": current["live_changed_at"].isoformat(),
                "live_revision": current["live_revision"],
            }
    if shared:
        with suppress(PublicLiveStoreError):
            envelope = store.get(match_key)
    # A client may already know a revision whose publication is still pending.
    if envelope is None or (
        since_revision is not None and envelope["revision"] < since_revision
    ):
        build = partial(build_published_live, match_key)
        envelope = (
            store.recover(
                match_key, since_revision if since_revision is not None else -1, build
            )
            if shared
            else build()
        )
        if envelope is None:
            return None
    return render_published_live(envelope, since_revision=since_revision)


def read_cached_public_live(
    *, match_id: str, store: PublishedLiveStore, since_revision: int | None = None
) -> dict[str, Any] | None:
    """Return only a fresh committed cache hit; never recover through SQL."""
    if connection.in_atomic_block:
        return None
    with suppress(PublicLiveStoreError):
        envelope = store.get(match_id)
        if envelope is not None and (
            since_revision is None or envelope["revision"] >= since_revision
        ):
            return render_published_live(envelope, since_revision=since_revision)
    return None


def render_published_live(
    envelope: dict[str, Any], *, since_revision: int | None = None
) -> dict[str, Any]:
    """Render the shared public response contract with a current server clock."""
    payload = deepcopy(envelope["payload"])
    revision = envelope["revision"]
    if since_revision is not None:
        if since_revision == revision:
            return {
                "changed": False,
                "server_time": timezone.now().isoformat(),
                "last_changed_at": payload["last_changed_at"],
                "live_revision": revision,
            }
        complete = (
            since_revision < revision
            and max(0, since_revision) >= envelope["history_base"]
        )
        payload["resources"] = sorted(
            {
                resource
                for resource, changed_at in envelope["resource_revisions"].items()
                if changed_at > since_revision
            }
            if complete
            else {resource.value for resource in ALL_LIVE_RESOURCES}
        )
    if payload["timer"]["type"] != "deactivated":
        payload["timer"]["server_time"] = timezone.now().isoformat()
    return payload
