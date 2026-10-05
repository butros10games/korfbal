"""Bounded, context-aware enrichment of known Sportlink app fixtures."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
import hashlib
import json
from typing import Any

from django.db import transaction
from django.db.models import Q, QuerySet
from django.utils import timezone

from apps.competition.models import Match, Pool, SyncResource
from apps.schedule.models import Season


DETAIL_FIELDS = {
    "match_timing": "playing_time_observed_at",
    "match_facility": "facility_observed_at",
    "match_rules": "rules_observed_at",
}
DETAIL_STATES = ("unobserved", "available", "empty", "stale", "unsupported")
EMPTY_REFRESH = timedelta(days=30)
METADATA_CONTEXT_VERSION = 1
MAX_DETAIL_SELECTION = 1000


@dataclass(frozen=True)
class DetailSelection:
    """An explicit, resumable database-only selection; IDs are exact provider IDs."""

    kinds: tuple[str, ...] = tuple(DETAIL_FIELDS)
    states: tuple[str, ...] = ("unobserved", "stale", "empty")
    source_ids: tuple[str, ...] = ()
    statuses: tuple[str, ...] = ()
    start_date: date | None = None
    end_date: date | None = None
    after: int = 0
    limit: int | None = None


def source_matches(season: Season) -> QuerySet[Match]:
    """Do not send archive or Dataservice identities to the app endpoints."""
    return Match.objects.filter(season=season).exclude(
        Q(external_id__startswith="ds:") | Q(external_id__startswith="archive:")
    )


def _context_fields(value: object) -> dict[str, Any]:
    """Ignore observation freshness when versioning retained source context."""
    if not isinstance(value, dict):
        return {}
    return {
        key: row.get("value")
        for key, row in (value.get("fields") or {}).items()
        if isinstance(row, dict)
    }


def metadata_context(match: Match, kind: str) -> str:
    """Version only inputs that can alter the endpoint's meaning."""
    values: dict[str, Any] = {
        "version": METADATA_CONTEXT_VERSION,
        "source": match.external_id,
        "home": match.home_team_id,
    }
    if kind == "match_facility":
        values["kickoff"] = match.starts_at.astimezone(UTC).isoformat()
    else:
        cached = match._state.fields_cache.get("pool")
        pool = (
            {
                "class_name": cached.class_name,
                "sport": cached.sport,
                "source_context": cached.source_context,
            }
            if cached is not None
            else Pool.objects
            .filter(pk=match.pool_id)
            .values("class_name", "sport", "source_context")
            .first()
            if match.pool_id is not None
            else None
        )
        values.update(pool_id=match.pool_id, pool=pool)
        if pool:
            pool["source_context"] = _context_fields(pool["source_context"])
    encoded = json.dumps(values, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode()).hexdigest()


def capture_metadata_context(season: Season, source_id: str, kind: str) -> str | None:
    """Capture immediately before provider I/O, never after its response."""
    if kind not in DETAIL_FIELDS:
        return None
    match = source_matches(season).filter(external_id=source_id).first()
    return metadata_context(match, kind) if match else None


def component_state(match: Match, kind: str) -> str:
    """Report legacy observations conservatively and never imply archive support."""
    if match.external_id.startswith(("archive:", "ds:")):
        return "unsupported"
    observation = match.metadata_observations.get(kind, {})
    stamp = getattr(match, DETAIL_FIELDS[kind])
    if observation.get("context") and observation["context"] != metadata_context(
        match, kind
    ):
        return "stale"
    if stamp is None:
        retained = observation.get("observed_at") or (
            match.facility_details
            if kind == "match_facility"
            else match.match_rules
            if kind == "match_rules"
            else match.playing_time_minutes
        )
        return "stale" if retained else "unobserved"
    if observation.get("state") in {"available", "empty"}:
        return observation["state"]
    available = (
        useful_facility(match.facility_details)
        if kind == "match_facility"
        else _useful(match.match_rules)
        if kind == "match_rules"
        else bool(match.playing_time_minutes)
    )
    return "available" if available else "empty"


def detail_candidates(season: Season, kind: str, now: datetime) -> QuerySet[Match]:
    """Select missing/stale components and due empty results without broad reads."""
    stamp = DETAIL_FIELDS[kind]
    return source_matches(season).filter(
        Q(**{stamp + "__isnull": True})
        | Q(**{
            f"metadata_observations__{kind}__state": "empty",
            f"metadata_observations__{kind}__next_refresh_at__lte": now.isoformat(),
        })
    )


def selected_matches(season: Season, selection: DetailSelection) -> list[Match]:
    """Read only the selected chunk; unsupported identities stay visible in preview."""
    matches = Match.objects.filter(season=season, pk__gt=selection.after)
    if selection.source_ids:
        matches = matches.filter(external_id__in=selection.source_ids)
    if selection.statuses:
        matches = matches.filter(status__in=selection.statuses)
    if selection.start_date:
        matches = matches.filter(starts_at__date__gte=selection.start_date)
    if selection.end_date:
        matches = matches.filter(starts_at__date__lte=selection.end_date)
    matches = matches.select_related("pool").order_by("pk")
    return list(matches[: selection.limit] if selection.limit else matches)


def preview_details(
    season: Season, *, selection: DetailSelection | None = None
) -> dict[str, Any]:
    """Preview performs no HTTP, checkpoint, or fixture writes."""
    selection = selection or DetailSelection()
    rows = selected_matches(season, selection)
    counts = Counter()
    states: dict[str, dict[str, int]] = {}
    selected_ids = []
    for row in rows:
        eligible = False
        for kind in selection.kinds:
            state = component_state(row, kind)
            states.setdefault(kind, dict.fromkeys(DETAIL_STATES, 0))[state] += 1
            if state in selection.states and state != "unsupported":
                counts[kind] += 1
                eligible = True
        if eligible:
            selected_ids.append(row.external_id)
    return {
        "matches": len(rows),
        "missing_by_kind": {kind: counts[kind] for kind in selection.kinds},
        "states_by_kind": states,
        "selected_source_ids": selected_ids,
        "cursor": rows[-1].pk if rows else selection.after,
        "remaining_detail_requests": sum(counts.values()),
        "unsupported_matches": sum(
            row.external_id.startswith(("archive:", "ds:")) for row in rows
        ),
        "note": (
            "App identities only; one GET per selected component. "
            "Existing retry ceilings, backoff and provider cooldowns remain effective."
        ),
    }


def queue_missing_details(
    season: Season,
    *,
    match_ids: set[int] | None = None,
    include_timing: bool = True,
    selection: DetailSelection | None = None,
) -> int:
    """Queue missing or invalidated components without resetting retry evidence."""
    if selection is not None:
        matches = selected_matches(season, selection)
        kinds = selection.kinds
        states = selection.states
    else:
        query = source_matches(season)
        if match_ids is not None:
            query = query.filter(pk__in=match_ids)
        matches = list(query.select_related("pool"))
        kinds = tuple(
            kind for kind in DETAIL_FIELDS if include_timing or kind != "match_timing"
        )
        states = ("unobserved", "stale", "empty")
    identities = [row.external_id for row in matches]
    existing = set(
        SyncResource.objects.filter(
            season=season, kind__in=kinds, source_id__in=identities
        ).values_list("kind", "source_id")
    )
    now = timezone.now()
    pending = [
        SyncResource(
            season=season, kind=kind, source_id=row.external_id, next_sync_at=now
        )
        for row in matches
        for kind in kinds
        if component_state(row, kind) in states
        and (kind, row.external_id) not in existing
    ]
    SyncResource.objects.bulk_create(pending, ignore_conflicts=True, batch_size=500)
    return len(pending)


def invalidate_metadata(match: Match, before: dict[str, str]) -> list[str]:
    """Invalidate changed inputs, retaining payloads and retry/backoff evidence."""
    changed = []
    for kind, previous in before.items():
        if metadata_context(match, kind) != previous:
            stamp = DETAIL_FIELDS[kind]
            observed = getattr(match, stamp)
            observation = match.metadata_observations.get(kind, {})
            if observed is not None or observation:
                match.metadata_observations = {
                    **match.metadata_observations,
                    kind: {
                        **observation,
                        "state": "stale",
                        "context": previous,
                        "observed_at": observed.isoformat()
                        if observed
                        else observation.get("observed_at"),
                    },
                }
                if "metadata_observations" not in changed:
                    changed.append("metadata_observations")
            if observed is not None:
                setattr(match, stamp, None)
                changed.append(stamp)
            SyncResource.objects.filter(
                season=match.season,
                kind=kind,
                source_id=match.external_id,
                failures=0,
            ).update(next_sync_at=timezone.now(), etag="")
    return changed


def invalidate_pool_metadata(pool_id: int) -> int:
    """Invalidate only this pool's timing and rule observations."""
    matches = Match.objects.filter(pool_id=pool_id)
    count = matches.update(playing_time_observed_at=None, rules_observed_at=None)
    SyncResource.objects.filter(
        kind__in=("match_timing", "match_rules"),
        failures=0,
        source_id__in=matches.values("external_id"),
        season_id__in=matches.values("season_id"),
    ).update(next_sync_at=timezone.now(), etag="")
    return count


def _useful(value: object) -> bool:
    if isinstance(value, dict):
        return any(_useful(item) for item in value.values())
    if isinstance(value, list):
        return any(_useful(item) for item in value)
    return bool(value.strip()) if isinstance(value, str) else value is not None


def useful_facility(data: dict[str, Any]) -> bool:
    """Verify only a populated playing facility name or address."""
    return any(
        isinstance(data.get(key), str) and data[key].strip()
        for key in ("FacilityName", "Address")
    )


def observe_component(
    match: Match, kind: str, observed_at: datetime, *, available: bool
) -> None:
    """Persist explicit empty outcomes and the context that was actually observed."""
    state = "available" if available else "empty"
    observation = {
        "state": state,
        "context_version": METADATA_CONTEXT_VERSION,
        "context": metadata_context(match, kind),
        "observed_at": observed_at.isoformat(),
    }
    if not available:
        observation["next_refresh_at"] = (observed_at + EMPTY_REFRESH).isoformat()
    match.metadata_observations = {**match.metadata_observations, kind: observation}


@transaction.atomic
def _import_metadata(  # noqa: PLR0913 -- Bind response to identity, component and I/O context.
    season: Season,
    source_id: str,
    data: dict[str, Any],
    observed_at: datetime,
    *,
    component: tuple[str, str],
    expected_context: str | None = None,
) -> None:
    """Fence old responses from certifying a corrected fixture.

    Raises:
        ValueError: The endpoint identifies a different or unsupported match.

    """
    field, stamp = component
    kind = "match_facility" if field == "facility_details" else "match_rules"
    if source_id.startswith(("archive:", "ds:")):
        raise ValueError("Unsupported match metadata identity")
    if "PublicMatchId" in data and str(data["PublicMatchId"]) != source_id:
        raise ValueError("Unrecognized match metadata")
    payload = {
        key: value
        for key, value in data.items()
        if key not in {"PublicMatchId", "Error"}
    }
    match = Match.objects.select_for_update(no_key=True).get(
        season=season, external_id=source_id
    )
    previous = getattr(match, stamp)
    if (previous is not None and previous > observed_at) or (
        expected_context is not None
        and expected_context != metadata_context(match, kind)
    ):
        return
    setattr(match, field, payload)
    setattr(match, stamp, observed_at)
    observe_component(
        match,
        kind,
        observed_at,
        available=useful_facility(payload)
        if kind == "match_facility"
        else _useful(payload),
    )
    match.save(update_fields=(field, stamp, "metadata_observations"))


def import_facility(
    season: Season,
    source_id: str,
    data: dict[str, Any],
    observed_at: datetime,
    *,
    expected_context: str | None = None,
) -> None:
    """Read MatchFacility, never nullable result-details Location."""
    _import_metadata(
        season,
        source_id,
        data,
        observed_at,
        component=("facility_details", "facility_observed_at"),
        expected_context=expected_context,
    )


def import_rules(
    season: Season,
    source_id: str,
    data: dict[str, Any],
    observed_at: datetime,
    *,
    expected_context: str | None = None,
) -> None:
    """Retain rules without guessing enforcement semantics from labels."""
    _import_metadata(
        season,
        source_id,
        data,
        observed_at,
        component=("match_rules", "rules_observed_at"),
        expected_context=expected_context,
    )
