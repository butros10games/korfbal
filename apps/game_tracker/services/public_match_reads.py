"""Revisioned public match read models shared by all spectators."""

from contextlib import suppress
import json
from time import time
from typing import Any

from django.db import connection

from apps.competition.services.match_prediction import match_prediction
from apps.game_tracker.application.ports import PublicLiveStoreError, PublishedLiveStore
from apps.game_tracker.models import MatchData, MatchLiveChange
from apps.game_tracker.services.match_stats_payload import build_match_stats_payload
from apps.game_tracker.services.timeline_reads import (
    MATCH_TIMELINE_IDENTITY_VERSION,
    consistent_timeline_read,
    read_match_events,
    read_match_shots,
)
from apps.kwt_common.utils.match_summary import build_match_summaries


PUBLIC_MATCH_RESOURCES = ("summary", "stats", "events", "shots")


def resource_key(match_id: str, resource: str) -> str:
    """Keep cache cardinality bounded by matches and known public resources."""
    return f"{match_id}:{resource}"


def build_public_match_reads(match_id: str) -> dict[str, dict[str, Any]]:
    """Build all public resources once from a consistent, non-locking snapshot."""
    started = time()
    with consistent_timeline_read():
        data = (
            MatchData.objects
            .select_related(
                "match_link__home_team__club",
                "match_link__away_team__club",
                "match_link__season",
            )
            .filter(match_link_id=match_id)
            .first()
        )
        if data is None:
            return {}
        match = data.match_link
        summary = build_match_summaries([data])[0]
        summary["prediction"] = match_prediction(match)
        payloads = {
            "summary": summary,
            "stats": build_match_stats_payload(match=match, match_data=data),
            "events": read_match_events(
                match_data_id=data.pk,
                since_revision=None,
                current_identity=True,
            ).to_payload(),
            "shots": read_match_shots(
                match_data_id=data.pk,
                since_revision=None,
                current_identity=True,
            ).to_payload(),
        }
        history = list(
            MatchLiveChange.objects
            .filter(
                match_data=data,
                revision__lte=data.live_revision,
            )
            .order_by("-revision")
            .values("revision", "resources", "changed_ids")[:512]
        )
    history.reverse()
    base = history[0]["revision"] - 1 if history else data.live_revision
    expected = base + 1
    for row in history:
        if row["revision"] != expected:
            base = row["revision"] - 1
        expected = row["revision"] + 1
    if not history or history[-1]["revision"] != data.live_revision:
        base = data.live_revision
    return {
        resource: {
            "revision": data.live_revision,
            "created_at": started,
            "payload": payload,
            "history_base": base,
            "push_base": max(
                (
                    row["revision"]
                    for row in history
                    if resource in row["resources"]
                    and row["revision"] < data.live_revision
                ),
                default=base,
            ),
            "incomplete_revision": max(
                (
                    row["revision"]
                    for row in history
                    if resource in row["resources"]
                    and resource not in row["changed_ids"]
                ),
                default=0,
            ),
            "id_revisions": {
                str(item_id): row["revision"]
                for row in history
                for item_id in row["changed_ids"].get(resource, [])
            },
        }
        for resource, payload in payloads.items()
    }


def publish_public_match_reads(*, match_id: str, store: PublishedLiveStore) -> None:
    """Publish shared resources before notifying their readers."""
    for resource, envelope in build_public_match_reads(match_id).items():
        store.put(resource_key(match_id, resource), envelope)


def render_public_match_read(
    envelope: dict[str, Any],
    *,
    resource: str,
    since_revision: int | None = None,
    current_identity: bool = False,
) -> dict[str, Any]:
    """Render exact timeline deltas from bounded shared revision metadata."""
    payload = envelope["payload"]
    if (
        resource not in {"events", "shots"}
        or not current_identity
        or since_revision is None
    ):
        return payload
    if (
        not max(envelope["history_base"], envelope["incomplete_revision"])
        <= since_revision
        <= envelope["revision"]
    ):
        return payload
    changed = {
        item_id
        for item_id, revision in envelope["id_revisions"].items()
        if revision > since_revision
    }
    items = payload[resource]
    ids = {str(item["event_id"]) for item in items}
    return {
        **{key: value for key, value in payload.items() if key != resource},
        "mode": "delta",
        "base_revision": since_revision,
        "upsert": [item for item in items if str(item["event_id"]) in changed],
        "deleted_ids": sorted(changed - ids),
        "order": [str(item["event_id"]) for item in items],
    }


def read_public_match_resource(
    *,
    match_id: str,
    resource: str,
    store: PublishedLiveStore,
    since_revision: int | None = None,
    identity_version: str | None = None,
) -> dict[str, Any] | None:
    """Read a shared response, coalescing cold misses outside caller transactions."""
    if connection.in_atomic_block or resource not in PUBLIC_MATCH_RESOURCES:
        return None
    key = resource_key(match_id, resource)
    envelope = None
    with suppress(PublicLiveStoreError):
        envelope = store.get(key)
    if envelope is None or (
        since_revision is not None and envelope["revision"] < since_revision
    ):

        def build() -> dict[str, Any] | None:
            envelopes = build_public_match_reads(match_id)
            for name, value in envelopes.items():
                with suppress(PublicLiveStoreError):
                    store.put(resource_key(match_id, name), value)
            return envelopes.get(resource)

        envelope = store.recover(key, since_revision or 0, build)
    if envelope is None:
        return None
    return render_public_match_read(
        envelope,
        resource=resource,
        since_revision=since_revision,
        current_identity=identity_version == str(MATCH_TIMELINE_IDENTITY_VERSION),
    )


def read_public_match_updates(
    *, match_id: str, revision: int, resources: list[str], store: PublishedLiveStore
) -> dict[str, Any]:
    """Attach bounded, revision-matched public responses without rebuilding reads."""
    updates = {}
    remaining = 64 * 1024
    for resource in PUBLIC_MATCH_RESOURCES:
        if resource not in resources:
            continue
        envelope = store.get(resource_key(match_id, resource))
        if envelope is None or envelope["revision"] != revision:
            continue
        base = envelope.get("push_base", revision - 1)
        payload = render_public_match_read(
            envelope,
            resource=resource,
            since_revision=base if base < revision else None,
            current_identity=True,
        )
        size = len(json.dumps(payload, separators=(",", ":")).encode())
        if size <= remaining:
            updates[resource] = payload
            remaining -= size
    return updates
