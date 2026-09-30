"""Application services for iOS Live Activities (Lock Screen and Dynamic Island).

The Expo app starts a ``MatchLiveActivity`` for a match and registers its
ActivityKit push token here. Every committed tracker revision then pushes the
score and clock to the phone; a finished match ends the activity.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import json
import logging
from typing import Any

from django.db import transaction
from django.utils import timezone

from apps.player.application.ports import (
    LiveActivityDeliveryError,
    LiveActivityPushClient,
)
from apps.player.models.live_activity import MatchLiveActivity


logger = logging.getLogger(__name__)

# Must match the name passed to ``createLiveActivity`` in the Expo app.
LIVE_ACTIVITY_NAME = "MatchLiveActivity"
# A finished match stays visible on the Lock Screen for a while.
END_DISMISSAL_SECONDS = 60 * 60
# Pushes follow tracker changes while iOS runs the clock itself, so a quiet
# spell is not staleness. Without news, a running period turns stale once it
# overruns its scheduled end; a paused or idle match (time-out, half time)
# after a longer silence. The Expo app applies the same rule locally.
RUNNING_STALE_GRACE = timedelta(minutes=10)
IDLE_STALE_AFTER = timedelta(minutes=30)
HALVES = 2


class LiveActivityNotFoundError(Exception):
    """Raised when a user does not own the requested activity."""


@dataclass(frozen=True, slots=True)
class LiveActivityProps:
    """Content state rendered by the widget; keys mirror the TypeScript props."""

    home_name: str
    away_name: str
    home_score: int
    away_score: int
    status: str
    period_label: str
    clock_label: str
    paused: bool
    # ISO instants for the widget's self-running clock (empty when idle).
    clock_start_at: str = ""
    clock_end_at: str = ""
    clock_paused_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Serialize with the camelCase keys the widget reads."""
        return {
            "homeName": self.home_name,
            "awayName": self.away_name,
            "homeScore": self.home_score,
            "awayScore": self.away_score,
            "status": self.status,
            "periodLabel": self.period_label,
            "clockLabel": self.clock_label,
            "paused": self.paused,
            "clockStartAt": self.clock_start_at,
            "clockEndAt": self.clock_end_at,
            "clockPausedAt": self.clock_paused_at,
        }


@dataclass(frozen=True, slots=True)
class LiveActivityPushResult:
    """Delivery totals for one match revision."""

    sent: int = 0
    failed: int = 0
    ended: int = 0
    skipped: int = 0


def _parse(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def clock_label(timer: dict[str, Any], *, now: datetime) -> str:
    """Return the match minute shown next to the score, or empty when idle."""
    started = _parse(timer.get("time"))
    if timer.get("type") not in {"active", "pause"} or started is None:
        return ""
    reference = _parse(timer.get("calc_to")) if timer.get("type") == "pause" else now
    reference = reference or now
    elapsed = (reference - started).total_seconds() - float(
        timer.get("pause_length") or 0
    )
    length = int(timer.get("length") or 0)
    minutes = max(0, int(elapsed // 60))
    if length:
        minutes = min(minutes, length // 60)
    return f"{minutes}'"


def clock_range(timer: dict[str, Any]) -> tuple[str, str, str]:
    """Return the running clock as (start, end, paused_at) ISO instants.

    The start already absorbs completed pauses, so ``now - start`` is the
    elapsed match time; iOS renders that itself until the next push.
    """
    started = _parse(timer.get("time"))
    if timer.get("type") not in {"active", "pause"} or started is None:
        return "", "", ""
    start = started + timedelta(seconds=float(timer.get("pause_length") or 0))
    end = start + timedelta(seconds=int(timer.get("length") or 0))
    paused_at = _parse(timer.get("calc_to")) if timer.get("type") == "pause" else None
    return (
        start.isoformat(),
        end.isoformat(),
        paused_at.isoformat() if paused_at else "",
    )


def period_label(*, status: str, current_part: int, parts: int) -> str:
    """Return the Dutch period description for the current match state."""
    if status == "finished":
        return "Afgelopen"
    if status == "upcoming":
        return "Nog niet begonnen"
    if parts == HALVES:
        return "1e helft" if current_part <= 1 else "2e helft"
    return f"{current_part}e periode"


def build_live_activity_props(
    *,
    snapshot: dict[str, Any],
    home_name: str,
    away_name: str,
    now: datetime | None = None,
) -> LiveActivityProps:
    """Turn a published live snapshot into widget content state."""
    now = now or timezone.now()
    status = str(snapshot.get("status") or "upcoming")
    score = snapshot.get("score") or {}
    timer = snapshot.get("timer") or {}
    start_at, end_at, paused_at = (
        ("", "", "") if status == "finished" else clock_range(timer)
    )
    return LiveActivityProps(
        home_name=home_name,
        away_name=away_name,
        home_score=int(score.get("home") or 0),
        away_score=int(score.get("away") or 0),
        status=status,
        period_label=period_label(
            status=status,
            current_part=int(snapshot.get("current_part") or 1),
            parts=int(snapshot.get("parts") or 2),
        ),
        clock_label="" if status == "finished" else clock_label(timer, now=now),
        paused=bool(snapshot.get("paused")),
        clock_start_at=start_at,
        clock_end_at=end_at,
        clock_paused_at=paused_at,
    )


def live_activity_stale_at(props: LiveActivityProps, *, now: datetime) -> datetime:
    """Return when iOS should mark the shown score as out of date."""
    running = props.status == "active" and not props.paused
    period_end = _parse(props.clock_end_at) if running else None
    if period_end is not None and not props.clock_paused_at:
        return max(period_end, now) + RUNNING_STALE_GRACE
    return now + IDLE_STALE_AFTER


def build_live_activity_payload(
    props: LiveActivityProps, *, event: str, now: datetime
) -> dict[str, Any]:
    """Build the ActivityKit APNs payload expected by expo-widgets."""
    timestamp = int(now.timestamp())
    aps: dict[str, Any] = {
        "timestamp": timestamp,
        "event": event,
        "content-state": {
            "name": LIVE_ACTIVITY_NAME,
            "props": json.dumps(props.to_dict(), separators=(",", ":")),
        },
    }
    if event == "end":
        aps["dismissal-date"] = timestamp + END_DISMISSAL_SECONDS
    else:
        aps["stale-date"] = int(live_activity_stale_at(props, now=now).timestamp())
    return {"aps": aps}


@transaction.atomic
def register_live_activity(
    *, user_id: int, match_id: str, push_token: str
) -> tuple[MatchLiveActivity, bool]:
    """Store or refresh an activity token; a rotated token replaces its owner."""
    return MatchLiveActivity.objects.update_or_create(
        push_token=push_token.strip(),
        defaults={
            "user_id": user_id,
            "match_id": match_id,
            "is_active": True,
        },
    )


@transaction.atomic
def end_live_activity(*, user_id: int, push_token: str) -> None:
    """Stop pushing to an activity the phone already ended.

    Raises:
        LiveActivityNotFoundError: The user owns no activity with that token.

    """
    activity = (
        MatchLiveActivity.objects
        .select_for_update()
        .filter(user_id=user_id, push_token=push_token.strip())
        .first()
    )
    if activity is None:
        raise LiveActivityNotFoundError
    if activity.is_active:
        activity.is_active = False
        activity.save(update_fields=["is_active", "updated_at"])


def has_active_live_activities(match_id: str) -> bool:
    """Return whether any phone is showing this match."""
    return MatchLiveActivity.objects.filter(match_id=match_id, is_active=True).exists()


def push_live_activities(
    *,
    match_id: str,
    snapshot: dict[str, Any],
    team_names: tuple[str, str],
    client: LiveActivityPushClient,
    now: datetime | None = None,
) -> LiveActivityPushResult:
    """Push the snapshot to every active activity for the match."""
    now = now or timezone.now()
    revision = int(snapshot.get("live_revision") or 0)
    home_name, away_name = team_names
    props = build_live_activity_props(
        snapshot=snapshot, home_name=home_name, away_name=away_name, now=now
    )
    event = "end" if props.status == "finished" else "update"
    payload = build_live_activity_payload(props, event=event, now=now)
    sent = failed = ended = skipped = 0
    for activity in MatchLiveActivity.objects.filter(
        match_id=match_id, is_active=True
    ).order_by("created_at"):
        if event == "update" and activity.last_pushed_revision >= revision > 0:
            skipped += 1
            continue
        try:
            client.send(token=activity.push_token, payload=payload)
        except LiveActivityDeliveryError as error:
            failed += 1
            if error.permanent:
                activity.is_active = False
                activity.save(update_fields=["is_active", "updated_at"])
            else:
                logger.warning(
                    "Live activity push failed for %s: %s %s",
                    activity.id_uuid,
                    error.status_code,
                    error.reason,
                )
            continue
        sent += 1
        activity.last_pushed_revision = revision
        if event == "end":
            ended += 1
            activity.is_active = False
        activity.save(update_fields=["last_pushed_revision", "is_active", "updated_at"])
    return LiveActivityPushResult(
        sent=sent, failed=failed, ended=ended, skipped=skipped
    )


def push_live_activities_for_match(
    *,
    match_id: str,
    client: LiveActivityPushClient,
    read_snapshot: Callable[[str], dict[str, Any] | None],
    read_team_names: Callable[[str], tuple[str, str] | None],
) -> LiveActivityPushResult:
    """Push the current published state of a match, if anyone is watching."""
    if not has_active_live_activities(match_id):
        return LiveActivityPushResult()
    snapshot = read_snapshot(match_id)
    names = read_team_names(match_id)
    if snapshot is None or names is None:
        return LiveActivityPushResult()
    return push_live_activities(
        match_id=match_id, snapshot=snapshot, team_names=names, client=client
    )
