"""Small public score/clock snapshots, independent of private tracker state."""

from functools import partial
from typing import Any

from django.db import connection, models
from django.utils import timezone

from apps.game_tracker.application.ports import PublicLiveSnapshotCache
from apps.game_tracker.models import MatchData, Pause, Shot
from apps.game_tracker.services.live_updates import summarize_match_changes
from apps.game_tracker.services.timeline_reads import consistent_timeline_read
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
    timer: dict[str, Any] = {"type": "deactivated", "match_data_id": str(match_data.pk)}
    paused = True
    if part is not None:
        pauses = list(Pause.objects.filter(match_data=match_data, match_part=part))
        active_pause = next((pause for pause in pauses if pause.active), None)
        paused = match_data.status != "active" or active_pause is not None
        timer.update({
            "type": "pause" if active_pause else "active",
            "time": part.start_time.isoformat(),
            "length": match_data.part_length,
            "pause_length": sum(
                pause.length().total_seconds() for pause in pauses if not pause.active
            ),
        })
        if active_pause and active_pause.start_time:
            timer["calc_to"] = active_pause.start_time.isoformat()
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


def read_public_live(
    *,
    match_id: object,
    snapshots: PublicLiveSnapshotCache,
    since_revision: int | None = None,
) -> dict[str, Any] | None:
    """Read one consistent revision without row locks or private roster queries.

    Nested transactions bypass shared storage: their writes may roll back, and
    their isolation level cannot safely be changed to a read-only snapshot.
    """
    share = not connection.in_atomic_block
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
        if since_revision is not None and match_data.live_revision <= since_revision:
            return {
                "changed": False,
                "server_time": timezone.now().isoformat(),
                "last_changed_at": match_data.live_changed_at.isoformat(),
                "live_revision": match_data.live_revision,
            }
        build = partial(_build_public_snapshot, match_data)
        payload = (
            snapshots.get_or_build(
                (
                    f"public-live:v1:{match_data.pk}:{match_data.live_revision}:"
                    f"{match_data.match_link.home_team_id}:{match_data.match_link.away_team_id}"
                ),
                build,
            )
            if share
            else build()
        )
        if since_revision is not None:
            summary = summarize_match_changes(match_data, since_revision=since_revision)
            payload["resources"] = sorted(
                resource.value for resource in summary.resources
            )
    if payload["timer"]["type"] != "deactivated":
        payload["timer"]["server_time"] = timezone.now().isoformat()
    return payload
