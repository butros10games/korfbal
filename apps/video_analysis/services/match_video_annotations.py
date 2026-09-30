"""Tags, notes and clips editors place on a match video.

Annotations use the recording's own timeline in video seconds. Viewers see
the ones marked for viewers once the video is published; editors see all of
them, also on an unpublished draft.
"""

from collections.abc import Callable, Mapping
import math
import re
from typing import Any
import uuid

from django.contrib.auth.models import AbstractBaseUser, AnonymousUser

from apps.schedule.models import Match
from apps.video_analysis.models import (
    MatchVideoAnnotation,
    MatchVideoPublication,
    Recording,
)
from apps.video_analysis.services.match_video import select_recording


KINDS = frozenset({"tag", "note", "clip"})
VISIBILITIES = frozenset({"editors", "viewers"})
MAX_LABEL = 80
MAX_BODY = 2000
MAX_CLIP_SECONDS = 600.0
MAX_PLAYERS = 16
MAX_SHAPES = 40
MAX_POINTS = 400
SHAPES = frozenset({"line", "arrow", "circle", "free"})
COLOR = re.compile(r"^#[0-9a-fA-F]{6}$")
POINT_AXES = 2
MIN_SHAPE_POINTS = 2


class AnnotationError(ValueError):
    """An annotation's input is invalid."""


def visible_recording(match: Match, *, can_edit: bool) -> Recording | None:
    """Return the match's recording when this viewer may watch it.

    Returns:
        The recording, or None.

    """
    recording = select_recording(match)
    if recording is None:
        return None
    try:
        published = recording.publication.published
    except MatchVideoPublication.DoesNotExist:
        published = False
    return recording if published or can_edit else None


def serialize(annotation: MatchVideoAnnotation) -> dict[str, Any]:
    """Return the API shape of one annotation.

    Returns:
        The annotation as JSON-ready data.

    """
    author = annotation.author
    return {
        "id": str(annotation.id),
        "kind": annotation.kind,
        "label": annotation.label,
        "body": annotation.body,
        "start_seconds": annotation.start_seconds,
        "end_seconds": annotation.end_seconds,
        "player_ids": list(annotation.player_ids),
        "visibility": annotation.visibility,
        "drawing": annotation.drawing,
        "author": (author.get_full_name() or author.get_username()) if author else None,
        "created_at": annotation.created_at.isoformat(),
    }


def list_annotations(match: Match, *, can_edit: bool) -> list[dict[str, Any]]:
    """Annotations this viewer may see, in video order.

    Returns:
        The visible annotations; none when the viewer may not watch the video.

    """
    recording = visible_recording(match, can_edit=can_edit)
    if recording is None:
        return []
    annotations = MatchVideoAnnotation.objects.filter(
        recording=recording
    ).select_related("author")
    if not can_edit:
        annotations = annotations.filter(visibility="viewers")
    return [serialize(annotation) for annotation in annotations]


def _seconds(value: object, field: str, duration: float) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise AnnotationError(f"{field} must be a number.")
    seconds = float(value)
    if not (math.isfinite(seconds) and 0 <= seconds <= duration):
        raise AnnotationError(f"{field} must lie within the video.")
    return round(seconds, 3)


def _text(value: object, field: str, limit: int) -> str:
    if not isinstance(value, str):
        raise AnnotationError(f"{field} must be text.")
    text = value.strip()
    if len(text) > limit:
        raise AnnotationError(f"{field} is too long.")
    return text


def _player_ids(value: object) -> list[str]:
    if not isinstance(value, list) or len(value) > MAX_PLAYERS:
        raise AnnotationError("player_ids must be a short list.")
    ids: list[str] = []
    for item in value:
        try:
            ids.append(str(uuid.UUID(str(item))))
        except ValueError as error:
            raise AnnotationError("player_ids must be player IDs.") from error
    return ids


def _point(value: object) -> list[float]:
    if (
        not isinstance(value, list)
        or len(value) != POINT_AXES
        or not all(
            isinstance(axis, int | float)
            and not isinstance(axis, bool)
            and 0 <= axis <= 1
            for axis in value
        )
    ):
        raise AnnotationError("Drawing points are x/y fractions of the frame.")
    return [round(float(value[0]), 4), round(float(value[1]), 4)]


def _drawing(value: object) -> list[dict[str, Any]] | None:
    """Validate shapes drawn on a paused frame, in frame fractions.

    Returns:
        The normalized shapes, or None for no drawing.

    Raises:
        AnnotationError: The drawing is not a short list of known shapes.

    """
    if value is None:
        return None
    if not isinstance(value, list) or len(value) > MAX_SHAPES:
        raise AnnotationError("drawing must be a short list of shapes.")
    shapes: list[dict[str, Any]] = []
    for shape in value:
        if (
            not isinstance(shape, Mapping)
            or not isinstance(shape.get("type"), str)
            or shape.get("type") not in SHAPES
        ):
            raise AnnotationError("Unknown drawing shape.")
        points = shape.get("points")
        if not isinstance(points, list) or not (
            MIN_SHAPE_POINTS <= len(points) <= MAX_POINTS
        ):
            raise AnnotationError("A drawing shape needs its points.")
        color = shape.get("color", "#f59e0b")
        if not isinstance(color, str) or not COLOR.fullmatch(color):
            raise AnnotationError("A drawing colour is a #rrggbb value.")
        shapes.append({
            "type": shape["type"],
            "points": [_point(point) for point in points],
            "color": color.lower(),
        })
    return shapes


def _choice(value: object, field: str, options: frozenset[str]) -> str:
    if not isinstance(value, str) or value not in options:
        raise AnnotationError(f"{field} must be one of: {', '.join(sorted(options))}.")
    return str(value)


def _parsers(duration: float) -> dict[str, Callable[[object], object]]:
    """Validate and normalize each writable field.

    Returns:
        One parser per field.

    """
    return {
        "kind": lambda value: _choice(value, "kind", KINDS),
        "label": lambda value: _text(value, "label", MAX_LABEL),
        "body": lambda value: _text(value, "body", MAX_BODY),
        "start_seconds": lambda value: _seconds(value, "start_seconds", duration),
        "end_seconds": lambda value: (
            None if value is None else _seconds(value, "end_seconds", duration)
        ),
        "player_ids": _player_ids,
        "visibility": lambda value: _choice(value, "visibility", VISIBILITIES),
        "drawing": _drawing,
    }


def _apply(
    annotation: MatchVideoAnnotation, data: Mapping[str, object], duration: float
) -> None:
    parsers = _parsers(duration)
    unknown = set(data) - set(parsers)
    if unknown:
        raise AnnotationError(f"Unknown fields: {', '.join(sorted(unknown))}.")
    for field, value in data.items():
        setattr(annotation, field, parsers[field](value))
    _check(annotation)


def _check(annotation: MatchVideoAnnotation) -> None:
    if annotation.kind == "clip":
        end = annotation.end_seconds
        if end is None or end <= annotation.start_seconds:
            raise AnnotationError("A clip ends after it starts.")
        if end - annotation.start_seconds > MAX_CLIP_SECONDS:
            raise AnnotationError("A clip lasts at most ten minutes.")
    elif annotation.end_seconds is not None:
        raise AnnotationError("Only clips have an end.")
    if annotation.kind == "tag" and not annotation.label:
        raise AnnotationError("A tag needs a label.")
    if annotation.kind == "note" and not (annotation.body or annotation.drawing):
        raise AnnotationError("A note needs text or a drawing.")


def editable_recording(match: Match) -> tuple[Recording, float]:
    """Return the match's recording and its duration for an editor's write.

    Returns:
        The recording and its length in seconds.

    Raises:
        LookupError: The match has no stored video.

    """
    recording = select_recording(match)
    if recording is None:
        raise LookupError("This match has no video.")
    return recording, float(recording.metadata.get("duration_seconds") or 0)


def create(
    match: Match,
    user: AbstractBaseUser | AnonymousUser,
    data: Mapping[str, object],
) -> dict[str, Any]:
    """Add an annotation to the match's video.

    Returns:
        The created annotation.

    Raises:
        AnnotationError: The input is invalid.

    """
    recording, duration = editable_recording(match)
    if "kind" not in data or "start_seconds" not in data:
        raise AnnotationError("kind and start_seconds are required.")
    annotation = MatchVideoAnnotation(
        recording=recording,
        author=user if user.is_authenticated else None,
    )
    _apply(annotation, data, duration)
    annotation.save()
    return serialize(annotation)


def _annotation(match: Match, annotation_id: str) -> tuple[MatchVideoAnnotation, float]:
    recording, duration = editable_recording(match)
    try:
        annotation = MatchVideoAnnotation.objects.select_related("author").get(
            recording=recording, id=annotation_id
        )
    except (MatchVideoAnnotation.DoesNotExist, ValueError) as error:
        raise LookupError("This annotation does not exist.") from error
    return annotation, duration


def update(
    match: Match, annotation_id: str, data: Mapping[str, object]
) -> dict[str, Any]:
    """Change an annotation; omitted fields keep their values.

    Returns:
        The updated annotation.

    """
    annotation, duration = _annotation(match, annotation_id)
    _apply(annotation, data, duration)
    annotation.save()
    return serialize(annotation)


def delete(match: Match, annotation_id: str) -> None:
    """Remove an annotation."""
    annotation, _ = _annotation(match, annotation_id)
    annotation.delete()
