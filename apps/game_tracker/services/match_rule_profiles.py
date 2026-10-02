"""Apply a resolved rule profile to a tracked match without rewriting history.

Untouched matches take the profile directly, including its clock. Once
tracking has started, or an administrator configured the match, a different
profile is only recorded as pending review: changing period lengths under
recorded events would silently change playing minutes, impacts and
eligibility.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from django.db import transaction
from django.utils import timezone

from apps.game_tracker.domain.match_rules import (
    MANUAL,
    OFFICIAL_TIMING,
    MatchRules,
)
from apps.game_tracker.models import MatchData, MatchPart, Shot
from apps.game_tracker.services.match_impact_persistence import (
    persist_match_impact_rows_with_breakdowns,
)
from apps.game_tracker.services.match_minutes import persist_match_minutes


APPLIED = "applied"
UNCHANGED = "unchanged"
PENDING_REVIEW = "pending_review"
KEPT = "kept"


def tracking_started(match_data: MatchData) -> bool:
    """Tell whether the match has live or recorded tracking state.

    A result imported from the provider (``score_source`` knkv/archive) is not
    tracking: such a match has no periods or events the clock could misplace.
    """
    return bool(
        match_data.live_revision
        or match_data.command_sequence
        or match_data.event_sequence
        or match_data.status == "active"
        or (match_data.status == "finished" and match_data.score_source == "tracker")
        or Shot.objects.filter(match_data=match_data).exists()
        or MatchPart.objects.filter(match_data=match_data).exists()
    )


def apply_rule_profile(match_data: MatchData, rules: MatchRules) -> str:
    """Store a profile on a locked match, or keep it for review.

    The caller must hold the match row lock (``select_for_update(no_key=True)``).

    Returns:
        applied, unchanged, pending_review or kept (a better existing profile).

    """
    snapshot = rules.as_snapshot()
    current = match_data.match_rules()
    if match_data.rules == snapshot:
        return _clear_pending(match_data)
    # Rejected or missing official timing never replaces an official observation.
    if current.source == OFFICIAL_TIMING and rules.source != OFFICIAL_TIMING:
        return KEPT
    if match_data.rules_source == MANUAL or tracking_started(match_data):
        return _pending(match_data, snapshot)
    clock = rules.tracker_clock()
    updates: dict[str, Any] = {
        "rules": snapshot,
        "rules_source": rules.source,
        "rules_pending": {},
    }
    if clock is not None:
        updates["parts"], updates["part_length"] = clock
    MatchData.objects.filter(pk=match_data.pk).update(**updates)
    for field, value in updates.items():
        setattr(match_data, field, value)
    return APPLIED


def accept_pending_rules(match_data: MatchData) -> MatchRules | None:
    """Apply a reviewed pending profile, including the clock of a tracked match.

    The caller locks the match and recomputes derived minutes and impacts.

    Returns:
        The applied rules, or None when nothing was pending.

    """
    snapshot = (match_data.rules_pending or {}).get("rules")
    if not snapshot:
        return None
    rules = _rules(snapshot, match_data)
    updates: dict[str, Any] = {
        "rules": snapshot,
        "rules_source": rules.source,
        "rules_pending": {},
    }
    clock = rules.tracker_clock()
    if clock is not None:
        updates["parts"], updates["part_length"] = clock
        updates["current_part"] = min(match_data.current_part, clock[0])
    MatchData.objects.filter(pk=match_data.pk).update(**updates)
    for field, value in updates.items():
        setattr(match_data, field, value)
    return rules


def accept_reviewed_rules(
    match_link_id: object, *, record_change: Callable[[MatchData], object]
) -> dict[str, int] | None:
    """Apply a reviewed pending profile and rebuild the match's derived rows.

    ``record_change`` publishes the new live revision (the caller's composition
    root binds it), so cached projections and clients refetch.

    Returns:
        Rebuilt row counts, or None when nothing was pending.

    """
    with transaction.atomic():
        tracker = MatchData.objects.select_for_update(no_key=True).get(
            match_link_id=match_link_id
        )
        if accept_pending_rules(tracker) is None:
            return None
        record_change(tracker)
    tracker.refresh_from_db()
    counts = {"minutes_rows": persist_match_minutes(match_data=tracker)}
    if tracker.status == "finished":
        counts["impact_rows"] = persist_match_impact_rows_with_breakdowns(
            match_data=tracker
        )
    return counts


def mark_manual(match_data: MatchData) -> None:
    """Record that an administrator configured the clock directly."""
    rules = match_data.match_rules()
    snapshot = {
        **rules.as_snapshot(),
        "source": MANUAL,
        "periods": [match_data.part_length // 60] * match_data.parts
        if match_data.part_length % 60 == 0
        else None,
    }
    match_data.rules = snapshot
    match_data.rules_source = MANUAL


def _rules(snapshot: dict[str, Any], match_data: MatchData) -> MatchRules:
    probe = MatchData(
        rules=snapshot, parts=match_data.parts, part_length=match_data.part_length
    )
    return probe.match_rules()


def _pending(match_data: MatchData, snapshot: dict[str, Any]) -> str:
    pending = match_data.rules_pending or {}
    if pending.get("rules") == snapshot:
        return PENDING_REVIEW
    value = {"rules": snapshot, "observed_at": timezone.now().isoformat()}
    MatchData.objects.filter(pk=match_data.pk).update(rules_pending=value)
    match_data.rules_pending = value
    return PENDING_REVIEW


def _clear_pending(match_data: MatchData) -> str:
    if match_data.rules_pending:
        MatchData.objects.filter(pk=match_data.pk).update(rules_pending={})
        match_data.rules_pending = {}
    return UNCHANGED


__all__ = [
    "APPLIED",
    "KEPT",
    "PENDING_REVIEW",
    "UNCHANGED",
    "accept_pending_rules",
    "accept_reviewed_rules",
    "apply_rule_profile",
    "mark_manual",
    "tracking_started",
]
