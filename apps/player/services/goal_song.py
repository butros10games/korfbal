"""Goal-song parsing and persistence helpers."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
import math

from django.db import transaction

from apps.player.models.player import Player
from apps.player.models.player_song import PlayerSong, PlayerSongStatus
from apps.player.services.player_song_queries import player_songs_by_ids


# Legacy start times translate into the first selected PlayerSong, whose clip
# must start inside the first fifteen minutes of its source (see player_songs).
MAX_GOAL_SONG_START_SECONDS = 899


LEGACY_GOAL_SONG_URI_DETAIL = (
    "goal_song_uri follows goal_song_song_ids; select an uploaded song instead"
)


@dataclass(frozen=True, slots=True)
class ParsedGoalSongPatchPayload:
    """Normalized PATCH payload for the goal-song endpoint."""

    goal_song_uri_provided: bool
    goal_song_uri: str | None
    song_start_time_provided: bool
    song_start_time: int | None
    goal_song_ids_provided: bool
    goal_song_song_ids: list[str] | None


@dataclass(slots=True)
class GoalSongPayloadError(Exception):
    """Raised when the goal-song PATCH payload is malformed."""

    detail: str


@dataclass(slots=True)
class GoalSongSelectionError(Exception):
    """Raised when requested goal-song ids are invalid."""

    detail: str
    missing: list[str] | None = None
    not_ready: list[str] | None = None


def sanitize_uploaded_filename(
    filename: str,
    *,
    fallback: str = "goal_song",
) -> str:
    """Return a storage-safe filename while preserving simple extensions."""
    safe_name = "".join(
        ch for ch in filename.strip() if ch.isalnum() or ch in {".", "-", "_"}
    )
    return safe_name or fallback


def _parse_optional_string(
    payload: Mapping[str, object],
    key: str,
) -> tuple[bool, str | None, str | None]:
    if key not in payload:
        return False, None, None

    raw = payload.get(key)
    if raw is None:
        return True, "", None
    if isinstance(raw, str):
        return True, raw.strip(), None
    return True, None, f"{key} must be a string or null"


def _whole_seconds(raw: float | str, key: str) -> int | str:
    """Return truncated, zero-clamped seconds, or an error for non-finite input."""
    if isinstance(raw, int):
        return max(0, raw)
    try:
        number = float(raw)
    except ValueError:
        return f"{key} must be a number or null"
    if not math.isfinite(number):
        return f"{key} must be a finite number"
    return max(0, int(number))


def _parse_optional_non_negative_int(
    payload: Mapping[str, object],
    key: str,
    *,
    maximum: int,
) -> tuple[bool, int | None, str | None]:
    """Parse a whole-second value: numbers truncate and negatives clamp to zero.

    Only scalars reach numeric conversion, so arrays and objects cannot raise
    ``TypeError`` from hashing and non-finite strings cannot raise
    ``OverflowError`` from integer conversion.
    """
    if key not in payload:
        return False, None, None

    raw = payload.get(key)
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return True, None, None
    if isinstance(raw, bool) or not isinstance(raw, (int, float, str)):
        return True, None, f"{key} must be a number or null"

    value = _whole_seconds(raw, key)
    if isinstance(value, str):
        return True, None, value
    if value > maximum:
        return True, None, f"{key} must be at most {maximum}"
    return True, value, None


def _parse_optional_uuid_list(
    payload: Mapping[str, object],
    key: str,
) -> tuple[bool, list[str] | None, str | None]:
    if key not in payload:
        return False, None, None

    raw = payload.get(key)
    if raw is None:
        return True, [], None

    if not isinstance(raw, list):
        return True, None, f"{key} must be a list of strings or null"

    items: list[str] = []
    for entry in raw:
        if not isinstance(entry, str):
            return True, None, f"{key} must be a list of strings"
        value = entry.strip()
        if value:
            items.append(value)

    seen: set[str] = set()
    deduped: list[str] = []
    for item in items:
        if item in seen:
            continue
        seen.add(item)
        deduped.append(item)

    return True, deduped, None


def parse_goal_song_patch_payload(
    data: Mapping[str, object],
) -> ParsedGoalSongPatchPayload:
    """Parse and validate the goal-song PATCH payload.

    Raises:
        GoalSongPayloadError: When a field has an invalid type or shape.

    """
    goal_song_uri_provided, goal_song_uri, goal_song_uri_error = _parse_optional_string(
        data,
        "goal_song_uri",
    )
    if goal_song_uri_error:
        raise GoalSongPayloadError(goal_song_uri_error)

    (
        song_start_time_provided,
        song_start_time,
        song_start_time_error,
    ) = _parse_optional_non_negative_int(
        data, "song_start_time", maximum=MAX_GOAL_SONG_START_SECONDS
    )
    if song_start_time_error:
        raise GoalSongPayloadError(song_start_time_error)

    (
        goal_song_ids_provided,
        goal_song_song_ids,
        goal_song_ids_error,
    ) = _parse_optional_uuid_list(data, "goal_song_song_ids")
    if goal_song_ids_error:
        raise GoalSongPayloadError(goal_song_ids_error)

    return ParsedGoalSongPatchPayload(
        goal_song_uri_provided=goal_song_uri_provided,
        goal_song_uri=goal_song_uri,
        song_start_time_provided=song_start_time_provided,
        song_start_time=song_start_time,
        goal_song_ids_provided=goal_song_ids_provided,
        goal_song_song_ids=goal_song_song_ids,
    )


def validate_goal_song_ids(
    *,
    player: Player,
    ids: list[str],
) -> list[PlayerSong]:
    """Validate that goal-song ids belong to the player and are ready."""
    if not ids:
        return []

    return validate_ready_goal_songs(
        ids=ids,
        songs=player_songs_by_ids(song_ids=ids, player=player),
    )


def validate_ready_goal_songs(
    *,
    ids: list[str],
    songs: Iterable[PlayerSong],
) -> list[PlayerSong]:
    """Validate an ordered selection against caller-scoped available songs.

    Raises:
        GoalSongSelectionError: When ids are missing or refer to unready songs.

    """
    by_id = {str(song.id_uuid): song for song in songs}

    missing = [song_id for song_id in ids if song_id not in by_id]
    if missing:
        raise GoalSongSelectionError(
            "Unknown song id(s)",
            missing=missing,
        )

    ordered = [by_id[song_id] for song_id in ids]
    not_ready: list[str] = []
    for song in ordered:
        if (
            song.effective_status != PlayerSongStatus.READY
            or not song.effective_audio_file
        ):
            not_ready.append(str(song.id_uuid))

    if not_ready:
        raise GoalSongSelectionError(
            "Song(s) not ready",
            not_ready=not_ready,
        )

    return ordered


def apply_goal_song_song_ids(
    *,
    player: Player,
    ids: list[str],
) -> list[str]:
    """Apply goal-song selection ids to the player in memory."""
    ordered = validate_goal_song_ids(player=player, ids=ids)
    return apply_goal_song_selection(player=player, ids=ids, ordered=ordered)


def apply_goal_song_selection(
    *,
    player: Player,
    ids: list[str],
    ordered: list[PlayerSong],
) -> list[str]:
    """Apply an already validated selection; the first song drives legacy playback.

    Every route that edits a player's selection (the player's own settings and
    team moderation) shares this rule after validating ``ordered``.

    Returns:
        The model fields to save.

    """
    update_fields: list[str] = ["goal_song_song_ids"]
    player.goal_song_song_ids = ids

    if ordered:
        first = ordered[0]
        audio_file = first.effective_audio_file
        if audio_file:
            player.goal_song_uri = audio_file.url
            update_fields.append("goal_song_uri")
        player.song_start_time = first.start_time_seconds
        update_fields.append("song_start_time")
        return update_fields

    player.goal_song_uri = ""
    player.song_start_time = None
    update_fields.extend(["goal_song_uri", "song_start_time"])
    return update_fields


@transaction.atomic
def update_goal_song_settings(
    *,
    player: Player,
    settings: ParsedGoalSongPatchPayload,
) -> None:
    """Persist the selection part of a validated goal-song settings command.

    ``goal_song_song_ids`` is the canonical configuration; ``goal_song_uri`` and
    ``song_start_time`` only mirror its first song for older clients. A legacy
    URI is accepted when it clears or repeats the mirrored value and rejected
    otherwise, so it can never point playback somewhere the selection does not.
    Legacy start times are applied to the selected song by the caller.

    Raises:
        GoalSongPayloadError: A legacy URI names audio outside the selection.

    """
    if settings.goal_song_ids_provided:
        update_fields = apply_goal_song_song_ids(
            player=player,
            ids=settings.goal_song_song_ids or [],
        )
        player.save(update_fields=list(dict.fromkeys(update_fields)))

    if settings.goal_song_uri and settings.goal_song_uri != player.goal_song_uri:
        raise GoalSongPayloadError(LEGACY_GOAL_SONG_URI_DETAIL)


def remove_deleted_song_from_goal_song_selection(
    *,
    player: Player,
    deleted_song_id: str,
) -> None:
    """Remove a deleted song from a player's goal-song selection."""
    current_ids = [song_id for song_id in (player.goal_song_song_ids or []) if song_id]
    next_ids = [song_id for song_id in current_ids if song_id != deleted_song_id]
    if next_ids == current_ids:
        return

    player.goal_song_song_ids = next_ids
    update_fields = [
        "goal_song_song_ids",
        "goal_song_uri",
        "song_start_time",
    ]

    if not next_ids:
        player.goal_song_uri = ""
        player.song_start_time = None
        player.save(update_fields=update_fields)
        return

    first = player_songs_by_ids(song_ids=next_ids[:1], player=player).first()
    audio_file = first.effective_audio_file if first is not None else None
    player.goal_song_uri = audio_file.url if audio_file else ""
    player.song_start_time = first.start_time_seconds if first is not None else None
    player.save(update_fields=update_fields)
