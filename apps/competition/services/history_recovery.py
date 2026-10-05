"""Read-only recovery manifests and fingerprinted, bounded explicit retries."""

from __future__ import annotations

from collections.abc import Iterable
import hashlib
import json
from typing import Any, TypedDict, Unpack

from django.db import transaction
from django.utils import timezone

from apps.competition.models import HistoricalResource
from apps.competition.services.history import KINDS, PROVIDERS
from apps.competition.services.history_editions import edition_scopes
from apps.schedule.queries.seasons import season_edition


VERSION = 1
MAX_SELECTION = 1000
BEFORE_FIELDS = (
    "key",
    "provider",
    "kind",
    "source_id",
    "state",
    "coverage",
    "reason",
    "attempts",
    "etag",
    "evidence",
    "next_attempt_at",
    "fetched_at",
    "start_date",
    "end_date",
    "sport",
)


def before_values(resource: HistoricalResource) -> dict[str, Any]:
    """Capture public checkpoint state, excluding credentials and provider payloads."""
    return {
        "id": resource.pk,
        "season": str(resource.season_id),
        **{field: getattr(resource, field) for field in BEFORE_FIELDS},
    }


def fingerprint(values: dict[str, Any]) -> str:
    """Digest the exact canonical state used for stale-plan rejection."""
    return hashlib.sha256(
        json.dumps(values, default=str, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


class RecoverySelection(TypedDict, total=False):
    """Explicit filters and bounds for one reviewed recovery slice."""

    editions: Iterable[int]
    provider: str | None
    kinds: Iterable[str]
    reasons: Iterable[str]
    resource_ids: Iterable[int]
    source_ids: Iterable[str]
    cursor: int
    limit: int
    max_requests: int


def validate_selection(options: RecoverySelection) -> None:
    """Reject unbounded, unsupported or malformed recovery selections.

    Raises:
        ValueError: Scope, filters or bounds are invalid.

    """
    limit = options.get("limit", 20)
    budget = options.get("max_requests", 20)
    if (
        not 1 <= limit <= MAX_SELECTION
        or not 1 <= budget <= MAX_SELECTION
        or options.get("cursor", 0) < 0
    ):
        raise ValueError(
            "Recovery bounds must be between 1 and 1000; cursor must be nonnegative"
        )
    if not any(options.get(key) for key in ("editions", "resource_ids", "source_ids")):
        raise ValueError(
            "Recovery requires an edition or explicit checkpoint/source IDs"
        )
    provider = options.get("provider")
    if provider is not None and provider not in PROVIDERS:
        raise ValueError("Unknown history provider")
    if provider == "archive":
        raise ValueError("Local archives have no provider recovery endpoint")
    if set(options.get("kinds", ())) - (KINDS - {"edition_scan"}):
        raise ValueError("Unknown or non-fetchable history kind")


def plan_recovery(**options: Unpack[RecoverySelection]) -> dict[str, Any]:
    """Select a stable bounded slice without changing checkpoints or making HTTP."""
    editions, kinds, reasons = (
        list(options.get("editions", ())),
        list(options.get("kinds", ())),
        list(options.get("reasons", ())),
    )
    resource_ids, source_ids = (
        list(options.get("resource_ids", ())),
        list(options.get("source_ids", ())),
    )
    validate_selection({
        **options,
        "editions": editions,
        "kinds": kinds,
        "reasons": reasons,
        "resource_ids": resource_ids,
        "source_ids": source_ids,
    })
    provider = options.get("provider")
    cursor, limit, max_requests = (
        options.get("cursor", 0),
        options.get("limit", 20),
        options.get("max_requests", 20),
    )
    query = (
        HistoricalResource.objects
        .filter(pk__gt=cursor)
        .exclude(kind="edition_scan")
        .exclude(provider="archive")
        .select_related("season")
        .order_by("pk")
    )
    if provider is not None:
        query = query.filter(provider=provider)
    if kinds:
        query = query.filter(kind__in=kinds)
    if reasons:
        query = query.filter(reason__in=reasons)
    if resource_ids:
        query = query.filter(pk__in=resource_ids)
    if source_ids:
        query = query.filter(source_id__in=source_ids)
    if editions:
        # Explicit season context also covers spring and full-year scopes.
        scope_ids = {
            scope.pk for edition in editions for scope in edition_scopes(edition)
        }
        query = query.filter(season_id__in=scope_ids)
    selected = list(query[:limit])
    rows = []
    for resource in selected:
        before = json.loads(json.dumps(before_values(resource), default=str))
        rows.append({
            "id": resource.pk,
            "edition": season_edition(resource.season),
            "before": before,
            "fingerprint": fingerprint(before),
            "invalidate_etag": True,
        })
    manifest = {
        "version": VERSION,
        "filters": {
            "editions": editions,
            "provider": provider,
            "kinds": kinds,
            "reasons": reasons,
            "resource_ids": resource_ids,
            "source_ids": source_ids,
        },
        "cursor": cursor,
        "next_cursor": selected[-1].pk if selected else cursor,
        "limit": limit,
        "max_requests": max_requests,
        "rows": rows,
    }
    manifest["fingerprint"] = fingerprint(manifest)
    return manifest


@transaction.atomic
def apply_recovery(manifest: dict[str, Any]) -> dict[str, Any]:
    """Check all reviewed before-values before rearming exactly the selected work.

    Raises:
        ValueError: The manifest is invalid, modified, duplicated or stale.

    """
    digest = manifest.get("fingerprint")
    if manifest.get("version") != VERSION or digest != fingerprint({
        key: value for key, value in manifest.items() if key != "fingerprint"
    }):
        raise ValueError("Invalid recovery manifest fingerprint or version")
    rows = manifest.get("rows")
    budget = manifest.get("max_requests")
    if (
        not isinstance(rows, list)
        or len(rows) > MAX_SELECTION
        or isinstance(budget, bool)
        or not isinstance(budget, int)
        or not 1 <= budget <= MAX_SELECTION
    ):
        raise ValueError("Invalid recovery manifest bounds")
    keys = [row["id"] for row in rows]
    if len(set(keys)) != len(keys):
        raise ValueError("Duplicate recovery checkpoint")
    resources = {
        row.pk: row
        for row in HistoricalResource.objects.select_for_update(no_key=True).filter(
            pk__in=keys
        )
    }
    if len(resources) != len(keys):
        raise ValueError("Recovery checkpoint no longer exists")
    for row in rows:
        resource = resources[row["id"]]
        if (
            resource.kind == "edition_scan"
            or resource.provider == "archive"
            or row.get("invalidate_etag") is not True
            or fingerprint(before_values(resource)) != row.get("fingerprint")
            or fingerprint(row["before"]) != row.get("fingerprint")
        ):
            raise ValueError("Stale or invalid recovery checkpoint")
    now = timezone.now()
    for resource in resources.values():
        evidence = {
            **resource.evidence,
            "recovery": {
                "manifest": digest,
                "before_fingerprint": fingerprint(before_values(resource)),
                "requested_at": now.isoformat(),
            },
        }
        HistoricalResource.objects.filter(pk=resource.pk).update(
            state="pending",
            attempts=0,
            reason="",
            etag="",
            next_attempt_at=now,
            evidence=evidence,
        )
    return {
        "rearmed": len(keys),
        "resource_ids": keys,
        "max_requests": budget,
        "next_cursor": manifest["next_cursor"],
        "manifest": digest,
    }
