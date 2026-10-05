"""Bounded fixture reconciliation manifests; apply only identity-proven changes."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
import hashlib
import json
from typing import Any

from django.db import transaction
from django.db.models import Count, F, Q, QuerySet, Window

from apps.competition.models import Match, MatchMembership
from apps.competition.services.fixture_linkage import (
    desired_fixture,
    fixture_decision,
    fixture_dependencies,
    locally_owned,
    native_fixture,
)
from apps.competition.services.publishing import serialize_with_publication
from apps.competition.services.seasons import phase_split, target_season
from apps.game_tracker.models import MatchData
from apps.schedule.models import Match as NativeMatch


MAX_LINKAGE_SELECTION = 1000
SUPERSEDED = ("SUSPENDED", "CANCELLED", "POSTPONED")
RELATIONS = (
    "season",
    "local_match",
    "pool__local_pool",
    "home_team__group",
    "away_team__group",
)


@dataclass(frozen=True)
class LinkageSelection:
    """Restrict review to an edition, season, pool or explicit source identities."""

    season_id: object | None = None
    edition: int | None = None
    source_ids: tuple[str, ...] = ()
    pool_ids: tuple[str, ...] = ()
    reasons: tuple[str, ...] = ()
    after: int = 0
    limit: int = 100


def candidates(selection: LinkageSelection) -> QuerySet[Match]:
    """Bound SQL candidates before loading relations or dependency counts."""
    rows = Match.objects.filter(pk__gt=selection.after)
    if selection.season_id:
        rows = rows.filter(season_id=selection.season_id)
    if selection.edition is not None:
        rows = rows.filter(season__edition=selection.edition)
    if selection.source_ids:
        rows = rows.filter(external_id__in=selection.source_ids)
    if selection.pool_ids:
        rows = rows.filter(pool__external_id__in=selection.pool_ids)
    duplicate_native = (
        NativeMatch.objects
        .filter(
            season_id__in=rows.values("local_match__season_id"),
            home_team_id__in=rows.values("local_match__home_team_id"),
            away_team_id__in=rows.values("local_match__away_team_id"),
            start_time__in=rows.values("local_match__start_time"),
        )
        .annotate(
            fixture_count=Window(
                expression=Count("pk"),
                partition_by=(
                    F("season_id"),
                    F("home_team_id"),
                    F("away_team_id"),
                    F("start_time"),
                ),
            )
        )
        .filter(fixture_count__gt=1)
        .values("pk")
    )
    return (
        rows
        .filter(
            Q(local_match_id__in=duplicate_native)
            | Q(local_match=None)
            | ~Q(local_match__pool_id=F("pool__local_pool_id"))
            | ~Q(local_match__start_time=F("starts_at"))
            | ~Q(local_match__home_team_id=F("home_team__group__local_team_id"))
            | ~Q(local_match__away_team_id=F("away_team__group__local_team_id"))
        )
        .select_related(*RELATIONS)
        .order_by("pk")
    )


def _fingerprint(value: object) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()


def _tracker_state(tracker: MatchData) -> dict[str, Any]:
    return {
        key: getattr(tracker, key)
        for key in (
            "status",
            "home_score",
            "away_score",
            "live_revision",
            "command_sequence",
            "event_sequence",
            "score_source",
        )
    }


def _routed_season(row: Match) -> tuple[Any, str | None]:
    """Route like the publisher; unresolved periods and sports await review."""
    sport = row.home_team.sport
    if sport != row.away_team.sport:
        return None, "season_discipline_unresolved"
    phase = row.pool.phase if row.pool_id else ""
    if row.pool_id:
        if "review" in row.pool.phase_evidence:
            return None, "period_review"
        if not phase and phase_split(row.season_id, sport):
            return None, "period_review"
    season = target_season(row.season, sport, phase)
    if season is None:
        return None, "season_discipline_unresolved"
    published = (
        row.pool.local_pool.season_id
        if row.pool_id and row.pool.local_pool_id
        else None
    )
    if published is not None and published != season.pk:
        return None, "season_repair_required"
    return season, None


def describe(row: Match) -> dict[str, Any]:
    """Inspect one bounded fixture and report only nonpersonal repair evidence."""
    home = row.home_team.group.local_team_id if row.home_team.group_id else None
    away = row.away_team.group.local_team_id if row.away_team.group_id else None
    pool_id = row.pool.local_pool_id if row.pool_id else None
    season, routing = _routed_season(row)
    desired = desired_fixture(
        row, home, away, pool_id, season_id=season.pk if season else row.season_id
    )
    entry: dict[str, Any] = {
        "pk": row.pk,
        "source_id": row.external_id,
        "status": row.status,
        "local_created": row.local_created,
        "desired": desired,
        "source_selections": MatchMembership.objects.filter(match=row).count(),
        "selection_observed_at": row.lineup_observed_at.isoformat()
        if row.lineup_observed_at
        else None,
        "private_selection_counts": row.private_lineup_counts,
        "action": None,
        "reason": routing or "unresolved_participants",
    }
    if home is None or away is None or season is None:
        entry["fingerprint"] = _fingerprint(entry)
        return entry
    if row.local_match_id:
        _linked_evidence(row, desired, entry)
    else:
        _unlinked_evidence(row, desired, entry)
    entry["notification_id"] = (
        str(row.schedule_notification_id) if row.schedule_notification_id else None
    )
    entry["source_updated_at"] = row.updated_at.isoformat()
    entry["fingerprint"] = _fingerprint(entry)
    return entry


def _linked_evidence(
    row: Match, desired: dict[str, Any], entry: dict[str, Any]
) -> None:
    """Classify linked native values and exact duplicate dependencies."""
    local = row.local_match
    tracker = MatchData.objects.get(match_link_id=local.pk)
    current = native_fixture(local)
    dependencies = fixture_dependencies(row, tracker)
    entry.update(
        current=current,
        dependencies=dependencies,
        tracker=_tracker_state(tracker),
        baseline=row.published_schedule.get("fixture"),
    )
    duplicates = (
        NativeMatch.objects
        .filter(
            season_id=local.season_id,
            home_team_id=local.home_team_id,
            away_team_id=local.away_team_id,
            start_time=local.start_time,
        )
        .exclude(pk=local.pk)
        .count()
    )
    entry["duplicate_native_fixtures"] = duplicates
    entry["reason"] = fixture_decision(
        row, tracker, current, desired, dependencies=dependencies
    )
    if duplicates:
        entry["reason"] = "duplicate_native_review"
    elif entry["reason"] == "safe_update":
        entry["action"] = "update_fixture"


def _unlinked_evidence(
    row: Match, desired: dict[str, Any], entry: dict[str, Any]
) -> None:
    """Require exact fixture keys for canonical twins or existing native links."""
    twins = list(
        Match.objects
        .filter(
            season_id=row.season_id,
            home_team_id=row.home_team_id,
            away_team_id=row.away_team_id,
            starts_at=row.starts_at,
            status="FINAL",
            home_score__isnull=False,
            away_score__isnull=False,
            local_match__isnull=False,
        )
        .exclude(pk=row.pk)
        .select_related("local_match")[:2]
    )
    native = list(
        NativeMatch.objects.filter(
            season_id=desired["season_id"],
            home_team_id=desired["home_team_id"],
            away_team_id=desired["away_team_id"],
            start_time=row.starts_at,
        ).order_by("pk")[:2]
    )
    entry["native_candidates"] = [str(match.pk) for match in native]
    if row.status in SUPERSEDED and twins:
        entry["reason"] = "superseded"
        if len(twins) == 1:
            twin = twins[0]
            tracker = MatchData.objects.get(match_link_id=twin.local_match_id)
            entry.update(
                target_source_id=twin.external_id,
                target_pk=twin.pk,
                target_native=native_fixture(twin.local_match),
                target_tracker=_tracker_state(tracker),
            )
            selections = MatchMembership.objects.filter(match=row)
            overlap = MatchMembership.objects.filter(
                match=twin, player_id__in=selections.values("player_id")
            ).exists()
            wrong_side = selections.exclude(
                team_id__in=(twin.home_team_id, twin.away_team_id)
            ).exists()
            if all((
                entry["source_selections"],
                not (overlap or wrong_side or locally_owned(tracker)),
                twin.pool_id == row.pool_id,
                native_fixture(twin.local_match) == desired,
                row.lineup_observed_at is not None,
                not twin.lineup_observed_at,
                not twin.private_lineup_counts,
                not MatchMembership.objects.filter(match=twin).exists(),
            )):
                entry.update(
                    reason="safe_selection_relink", action="move_selection_links"
                )
            elif entry["source_selections"]:
                entry["reason"] = "protected_selection_review"
    elif len(native) > 1:
        entry["reason"] = "duplicate_native_review"
    elif len(native) == 1:
        local = native[0]
        claim = (
            Match.objects.filter(local_match=local).values_list("pk", flat=True).first()
        )
        entry.update(current=native_fixture(local), native_claimant=claim)
        if claim is None and native_fixture(local) == desired:
            entry.update(
                reason="safe_exact_link",
                action="link_fixture",
                target_native_id=str(local.pk),
            )
        else:
            entry["reason"] = "claimed_or_pool_review"
    else:
        entry["reason"] = "missing_native_review"


def preview_linkage(selection: LinkageSelection) -> dict[str, Any]:
    """Make an idempotent manifest with no provider, native or checkpoint writes."""
    rows = list(candidates(selection)[: selection.limit])
    entries = [describe(row) for row in rows]
    if selection.reasons:
        entries = [entry for entry in entries if entry["reason"] in selection.reasons]
    return {
        "version": 1,
        "dry_run": True,
        "http_requests": 0,
        "cursor": rows[-1].pk if rows else selection.after,
        "counts": dict(Counter(entry["reason"] for entry in entries)),
        "entries": entries,
    }


@transaction.atomic
def apply_entry(entry: dict[str, Any]) -> str:
    """Recheck a manifest under scoped locks; stale/protected entries never apply."""
    # Wait for any publication pass first: it locks trackers before fixtures and
    # source rows, the reverse of the per-entry order below.
    serialize_with_publication()
    row = (
        Match.objects
        .select_for_update(no_key=True, of=("self",))
        .select_related(*RELATIONS)
        .get(pk=entry["pk"])
    )
    if row.local_match_id:
        row.local_match = NativeMatch.objects.select_for_update(no_key=True).get(
            pk=row.local_match_id
        )
        MatchData.objects.select_for_update(no_key=True).get(
            match_link_id=row.local_match_id
        )
    elif entry.get("target_native_id"):
        NativeMatch.objects.select_for_update(no_key=True).get(
            pk=entry["target_native_id"]
        )
        MatchData.objects.select_for_update(no_key=True).get(
            match_link_id=entry["target_native_id"]
        )
    elif entry.get("target_pk"):
        target = Match.objects.select_for_update(no_key=True).get(pk=entry["target_pk"])
        target.local_match = NativeMatch.objects.select_for_update(no_key=True).get(
            pk=target.local_match_id
        )
        MatchData.objects.select_for_update(no_key=True).get(
            match_link_id=target.local_match_id
        )
    current = describe(row)
    if current["fingerprint"] != entry.get("fingerprint"):
        return "stale_manifest"
    action = current["action"]
    if action == "update_fixture":
        desired = current["desired"]
        NativeMatch.objects.filter(pk=row.local_match_id).update(
            start_time=row.starts_at,
            pool_id=desired["pool_id"],
            home_team_id=desired["home_team_id"],
            away_team_id=desired["away_team_id"],
        )
        row.published_schedule = {**row.published_schedule, "fixture": desired}
        row.save(update_fields=("published_schedule",))
    elif action == "link_fixture":
        row.local_match_id = current["target_native_id"]
        row.local_created = False
        row.published_at = None
        row.save(update_fields=("local_match", "local_created", "published_at"))
    elif action == "move_selection_links":
        target = Match.objects.get(pk=current["target_pk"])
        MatchMembership.objects.filter(match=row).update(match=target)
        target.lineup_observed_at = row.lineup_observed_at
        target.private_lineup_counts = row.private_lineup_counts
        target.save(update_fields=("lineup_observed_at", "private_lineup_counts"))
    else:
        return "review_only"
    return "applied"


def apply_manifest(manifest: dict[str, Any]) -> dict[str, int]:
    """Apply only a previously inspected finite manifest, independently per fixture.

    Raises:
        ValueError: The manifest format or selection limit is invalid.

    """
    if (
        manifest.get("version") != 1
        or not isinstance(manifest.get("entries"), list)
        or len(manifest["entries"]) > MAX_LINKAGE_SELECTION
    ):
        raise ValueError(
            "Expected a version-1 linkage manifest of at most 1000 entries"
        )
    return dict(Counter(apply_entry(entry) for entry in manifest["entries"]))
