"""Associate private recordings with canonical tracked matches."""

from typing import Any
from uuid import UUID

from django.db import transaction
from django.db.models import Q

from apps.schedule.models import Match
from apps.video_analysis.engine.store import ConflictError
from apps.video_analysis.models import Recording, Workspace
from apps.video_analysis.services.review import publish_later


MIN_SEARCH = 2
PAGE_SIZE = 20


def summary(match: Match) -> dict:
    """Expose sporting context without player or tracker credentials."""
    return {
        "id": str(match.pk),
        "title": f"{match.home_team} - {match.away_team}",
        "start_time": match.start_time.isoformat(),
        "tracked": hasattr(match, "tracker_data"),
    }


def search(term: str) -> dict:
    """Search both teams on the server, returning a bounded result page."""
    term = term.strip()
    if len(term) < MIN_SEARCH:
        return {"matches": [], "has_more": False}
    matches = Match.objects.select_related(
        "home_team__club", "away_team__club", "tracker_data"
    )
    for word in term[:120].split()[:8]:
        matches = matches.filter(
            Q(home_team__name__icontains=word)
            | Q(away_team__name__icontains=word)
            | Q(home_team__club__name__icontains=word)
            | Q(away_team__club__name__icontains=word)
        )
    rows = list(matches.order_by("-start_time", "pk")[: PAGE_SIZE + 1])
    return {
        "matches": [summary(m) for m in rows[:PAGE_SIZE]],
        "has_more": len(rows) > PAGE_SIZE,
    }


def attach(workspace: Workspace, data: dict) -> dict:
    """Enrich API navigation without copying the relationship into annotations."""
    links = {
        row.source_id: summary(row.match) if row.match else None
        for row in Recording.objects.filter(workspace=workspace).select_related(
            "match__home_team__club", "match__away_team__club", "match__tracker_data"
        )
    }
    for recording in data["matches"]:
        recording["linked_match"] = links.get(recording["id"])
    return data


@transaction.atomic
def save(workspace: Workspace, payload: dict[str, Any]) -> dict:
    """Change only the association, rejecting stale link edits.

    Raises:
        ValueError: A recording, match or expected association is invalid.
        ConflictError: Someone changed this recording's association meanwhile.

    """
    locked = Workspace.objects.select_for_update().get(pk=workspace.pk)
    recording = Recording.objects.filter(
        workspace=workspace, source_id=payload.get("recording_id")
    ).first()
    if recording is None or not recording.metadata.get("video"):
        raise ValueError("Choose a recording with video")
    current = str(recording.match_id) if recording.match_id else None
    if "expected_match_id" not in payload:
        raise ValueError("Expected match link is required")
    if payload["expected_match_id"] != current:
        raise ConflictError("Match link changed. Reload before saving your edit.")
    if "match_id" not in payload:
        raise ValueError("Choose a match or explicitly unlink it")
    target = payload["match_id"]
    match = None
    if target is not None:
        if not isinstance(target, str):
            raise ValueError("Invalid match ID")
        try:
            key = UUID(target)
        except ValueError as exc:
            raise ValueError("Invalid match ID") from exc
        match = (
            Match.objects
            .select_related("home_team__club", "away_team__club", "tracker_data")
            .filter(pk=key)
            .first()
        )
        if match is None:
            raise ValueError("Unknown match")
    if current != (str(match.pk) if match else None):
        recording.match = match
        recording.save(update_fields=["match"])
        locked.revision += 1
        locked.save(update_fields=["revision"])
        publish_later(workspace)
    return {
        "linked_match": summary(match) if match else None,
        "revision": locked.revision,
    }
