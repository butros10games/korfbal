"""Show a linked recording on its match page, synced to the tracked periods.

A recording linked to a match becomes public only after an editor publishes
it. Editors see unpublished recordings so they can sync them first. Each tracked
match part gets one anchor: the video second where that part starts. Because
tracked gebeurtenissen carry wall-clock times, one anchor per part places every
gebeurtenis on the video, including across timeouts and camera pauses between
parts. A camera that splits its footage into files can lose a few seconds at
each split; editors record those as breaks (where the video skips real time,
and by how much) so the rest of the period stays in sync.
"""

from dataclasses import dataclass
import math
from typing import Protocol

from django.contrib.auth.models import AbstractBaseUser, AnonymousUser
from django.db import transaction
from django.db.models import F

from apps.game_tracker.models import MatchPart
from apps.schedule.models import Match
from apps.video_analysis.models import MatchVideoPublication, Recording, StoredFile


MAX_BREAKS = 50
MAX_SKIPPED_SECONDS = 3600.0


class PlaybackUrls(Protocol):
    """Capability: sign a browser-playable URL for a recording's stored video."""

    def playback_url(self, recording: Recording) -> str | None:
        """Return a playable URL, or None when the video cannot be served."""
        ...


class MatchVideoConflictError(Exception):
    """Another editor saved the video settings after this editor loaded them."""

    def __init__(self, expected: int, current: int) -> None:
        """Keep both revisions for a structured HTTP 409."""
        super().__init__("The match video changed; reload and try again.")
        self.expected_revision = expected
        self.revision = current


class MatchVideoBreakError(ValueError):
    """A recording break is invalid."""


@dataclass(frozen=True)
class MatchVideoUpdate:
    """Validated editor input; omitted fields keep their saved values."""

    expected_revision: int
    published: bool | None = None
    anchors: dict[str, float | None] | None = None
    breaks: list[tuple[float, float]] | None = None
    """Replaces every break: ``(video_seconds, skipped_seconds)`` pairs."""


def select_recording(match: Match) -> Recording | None:
    """Pick the match's playable recording, preferring a published one.

    Returns:
        The recording, or None when the match has no stored video.

    """
    recordings = (
        Recording.objects
        .filter(match=match)
        .select_related("workspace", "publication")
        # PostgreSQL sorts NULL first in descending order: recordings without a
        # publication row must not outrank a published one.
        .order_by(F("publication__published").desc(nulls_last=True), "-pk")
    )
    for recording in recordings:
        video = recording.metadata.get("video")
        if (
            video
            and StoredFile.objects.filter(
                workspace=recording.workspace, relative_path=video
            ).exists()
        ):
            return recording
    return None


def _publication(recording: Recording) -> MatchVideoPublication | None:
    try:
        return recording.publication
    except MatchVideoPublication.DoesNotExist:
        return None


def _parts(match: Match) -> list[MatchPart]:
    return list(
        MatchPart.objects.filter(match_data__match_link=match).order_by(
            "part_number", "start_time"
        )
    )


def read(match: Match, *, can_edit: bool, urls: PlaybackUrls) -> dict:
    """Describe the match video for the viewer.

    Returns:
        ``{"video": ... | None, "can_edit": bool}``.

    """
    recording = select_recording(match)
    publication = _publication(recording) if recording else None
    published = bool(publication and publication.published)
    if recording is None or not (published or can_edit):
        return {"video": None, "can_edit": can_edit}
    anchors = publication.anchors if publication else {}
    return {
        "video": {
            "url": urls.playback_url(recording),
            "duration_seconds": float(recording.metadata.get("duration_seconds") or 0),
            "published": published,
            "revision": publication.revision if publication else 0,
            "parts": [
                {
                    "match_part_id": str(part.id_uuid),
                    "part_number": part.part_number,
                    "start_time": part.start_time.isoformat(),
                    "end_time": part.end_time.isoformat() if part.end_time else None,
                    "video_seconds": anchors.get(str(part.id_uuid)),
                }
                for part in _parts(match)
            ],
            "breaks": publication.breaks if publication else [],
        },
        "can_edit": can_edit,
    }


def update(
    match: Match,
    user: AbstractBaseUser | AnonymousUser,
    change: MatchVideoUpdate,
) -> None:
    """Apply an editor's publish/sync change under the publication row lock.

    Raises:
        LookupError: The match has no stored recording.
        ValueError: An anchor names another match's part or leaves the video,
            or a recording break is invalid (``MatchVideoBreakError``).
        MatchVideoConflictError: The settings changed since the editor loaded them.

    """
    recording = select_recording(match)
    if recording is None:
        raise LookupError("This match has no video.")
    duration = float(recording.metadata.get("duration_seconds") or 0)
    part_ids = {str(part.id_uuid) for part in _parts(match)}
    with transaction.atomic():
        MatchVideoPublication.objects.get_or_create(recording=recording)
        publication = MatchVideoPublication.objects.select_for_update().get(
            recording=recording
        )
        if publication.revision != change.expected_revision:
            raise MatchVideoConflictError(
                change.expected_revision, publication.revision
            )
        if change.anchors is not None:
            anchors = dict(publication.anchors)
            for part_id, seconds in change.anchors.items():
                if part_id not in part_ids:
                    raise ValueError("Unknown match part.")
                if seconds is None:
                    anchors.pop(part_id, None)
                elif not (math.isfinite(seconds) and 0 <= seconds <= duration):
                    raise ValueError("The sync point must lie within the video.")
                else:
                    anchors[part_id] = round(seconds, 3)
            publication.anchors = anchors
        if change.breaks is not None:
            publication.breaks = _breaks(change.breaks, duration)
        if change.published is not None:
            publication.published = change.published
        publication.revision += 1
        publication.updated_by = user if user.is_authenticated else None
        publication.save()


def _breaks(breaks: list[tuple[float, float]], duration: float) -> list[dict]:
    """Validate breaks and store them in video order.

    Returns:
        The stored ``{"video_seconds", "skipped_seconds"}`` rows.

    Raises:
        MatchVideoBreakError: A break leaves the video, skips an impossible
            amount of time, or repeats a video moment.

    """
    if len(breaks) > MAX_BREAKS:
        raise MatchVideoBreakError("Too many recording breaks.")
    rows: dict[float, float] = {}
    for video_seconds, skipped_seconds in breaks:
        if not (math.isfinite(video_seconds) and 0 < video_seconds <= duration):
            raise MatchVideoBreakError("A recording break must lie within the video.")
        if not (
            math.isfinite(skipped_seconds)
            and 0 <= skipped_seconds <= MAX_SKIPPED_SECONDS
        ):
            raise MatchVideoBreakError("A recording break skips at most an hour.")
        at = round(video_seconds, 3)
        if at in rows:
            raise MatchVideoBreakError("Two recording breaks share a moment.")
        rows[at] = round(skipped_seconds, 3)
    return [
        {"video_seconds": at, "skipped_seconds": skipped}
        for at, skipped in sorted(rows.items())
    ]
