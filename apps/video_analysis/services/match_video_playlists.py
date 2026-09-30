"""Named, ordered sets of saved clips on a match video.

A playlist refers to clip annotations of the same recording. Clips removed
later simply drop out of it. Viewers see playlists marked for viewers once the
video is published, and only the clips they may see.
"""

from collections.abc import Mapping
from typing import Any
import uuid

from django.contrib.auth.models import AbstractBaseUser, AnonymousUser

from apps.schedule.models import Match
from apps.video_analysis.models import MatchVideoAnnotation, MatchVideoPlaylist
from apps.video_analysis.services.match_video_annotations import (
    MAX_LABEL,
    VISIBILITIES,
    AnnotationError,
    editable_recording,
    visible_recording,
)


MAX_CLIPS = 100


def _serialize(
    playlist: MatchVideoPlaylist, clips: Mapping[str, MatchVideoAnnotation]
) -> dict[str, Any]:
    author = playlist.author
    return {
        "id": str(playlist.id),
        "title": playlist.title,
        "annotation_ids": [
            annotation_id
            for annotation_id in playlist.annotation_ids
            if annotation_id in clips
        ],
        "visibility": playlist.visibility,
        "author": (author.get_full_name() or author.get_username()) if author else None,
        "created_at": playlist.created_at.isoformat(),
    }


def _clips(recording_id: int, *, can_edit: bool) -> dict[str, MatchVideoAnnotation]:
    annotations = MatchVideoAnnotation.objects.filter(
        recording_id=recording_id, kind="clip"
    )
    if not can_edit:
        annotations = annotations.filter(visibility="viewers")
    return {str(annotation.id): annotation for annotation in annotations}


def list_playlists(match: Match, *, can_edit: bool) -> list[dict[str, Any]]:
    """Playlists this viewer may see, with only the clips they may see.

    Returns:
        The visible playlists, newest first.

    """
    recording = visible_recording(match, can_edit=can_edit)
    if recording is None:
        return []
    playlists = MatchVideoPlaylist.objects.filter(recording=recording).select_related(
        "author"
    )
    if not can_edit:
        playlists = playlists.filter(visibility="viewers")
    clips = _clips(recording.pk, can_edit=can_edit)
    return [_serialize(playlist, clips) for playlist in playlists]


def _title(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise AnnotationError("A playlist needs a title.")
    if len(value.strip()) > MAX_LABEL:
        raise AnnotationError("title is too long.")
    return value.strip()


def _annotation_ids(
    value: object, clips: Mapping[str, MatchVideoAnnotation]
) -> list[str]:
    if not isinstance(value, list) or not value or len(value) > MAX_CLIPS:
        raise AnnotationError("A playlist holds one to a hundred clips.")
    normalized: list[str] = []
    for item in value:
        try:
            key = str(uuid.UUID(str(item)))
        except ValueError as error:
            raise AnnotationError("annotation_ids must be clip IDs.") from error
        if key not in clips:
            raise AnnotationError("Every item must be a clip on this video.")
        normalized.append(key)
    return normalized


def _visibility(value: object) -> str:
    if not isinstance(value, str) or value not in VISIBILITIES:
        raise AnnotationError("visibility must be editors or viewers.")
    return str(value)


def _apply(
    playlist: MatchVideoPlaylist,
    data: Mapping[str, object],
    clips: Mapping[str, MatchVideoAnnotation],
) -> None:
    unknown = set(data) - {"title", "annotation_ids", "visibility"}
    if unknown:
        raise AnnotationError(f"Unknown fields: {', '.join(sorted(unknown))}.")
    if "title" in data:
        playlist.title = _title(data["title"])
    if "annotation_ids" in data:
        playlist.annotation_ids = _annotation_ids(data["annotation_ids"], clips)
    if "visibility" in data:
        playlist.visibility = _visibility(data["visibility"])


def create(
    match: Match,
    user: AbstractBaseUser | AnonymousUser,
    data: Mapping[str, object],
) -> dict[str, Any]:
    """Add a playlist of this video's clips.

    Returns:
        The created playlist.

    Raises:
        AnnotationError: The input is invalid.

    """
    recording, _ = editable_recording(match)
    if "title" not in data or "annotation_ids" not in data:
        raise AnnotationError("title and annotation_ids are required.")
    clips = _clips(recording.pk, can_edit=True)
    playlist = MatchVideoPlaylist(
        recording=recording, author=user if user.is_authenticated else None
    )
    _apply(playlist, data, clips)
    playlist.save()
    return _serialize(playlist, clips)


def _playlist(match: Match, playlist_id: str) -> MatchVideoPlaylist:
    recording, _ = editable_recording(match)
    try:
        return MatchVideoPlaylist.objects.select_related("author").get(
            recording=recording, id=playlist_id
        )
    except (MatchVideoPlaylist.DoesNotExist, ValueError) as error:
        raise LookupError("This playlist does not exist.") from error


def update(
    match: Match, playlist_id: str, data: Mapping[str, object]
) -> dict[str, Any]:
    """Change a playlist; omitted fields keep their values.

    Returns:
        The updated playlist.

    """
    playlist = _playlist(match, playlist_id)
    clips = _clips(playlist.recording_id, can_edit=True)
    _apply(playlist, data, clips)
    playlist.save()
    return _serialize(playlist, clips)


def delete(match: Match, playlist_id: str) -> None:
    """Remove a playlist; its clips stay."""
    _playlist(match, playlist_id).delete()
