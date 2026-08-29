"""Typed application commands for correcting match timeline events."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum
from functools import singledispatch
from typing import NoReturn
from uuid import UUID

from django.db.models import Q
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from apps.game_tracker.application.ports import MatchChangePublisher
from apps.game_tracker.models import (
    GoalType,
    MatchData,
    MatchPart,
    MatchPlayer,
    Pause,
    PlayerChange,
    PlayerGroup,
    Shot,
    Timeout,
)
from apps.game_tracker.services.match_mutations import (
    EditorMutationContext,
    apply_editor_mutation,
)
from apps.player.models.player import Player
from apps.team.models.team import Team


type EntityId = UUID | str


class UnsetValue(Enum):
    """Marker distinguishing an omitted PATCH field from a false-like value."""

    UNSET = "unset"


UNSET = UnsetValue.UNSET
type PatchValue[T] = T | UnsetValue


@dataclass(frozen=True, slots=True)
class CreateGoalEvent:
    """Create one scored or missed shot on the editor timeline."""

    player_id: EntityId
    team_id: EntityId
    shot_type_id: EntityId
    match_part_id: EntityId
    time: str | None
    minute: int | None
    scored: bool
    for_team: bool = True


@dataclass(frozen=True, slots=True)
class UpdateGoalEvent:
    """Apply a partial correction to one shot."""

    event_id: str
    player_id: PatchValue[EntityId] = UNSET
    team_id: PatchValue[EntityId] = UNSET
    shot_type_id: PatchValue[EntityId] = UNSET
    match_part_id: PatchValue[EntityId] = UNSET
    time: PatchValue[str] = UNSET
    minute: PatchValue[int] = UNSET
    scored: PatchValue[bool] = UNSET
    for_team: PatchValue[bool] = UNSET


@dataclass(frozen=True, slots=True)
class DeleteGoalEvent:
    """Delete one shot from the editor timeline."""

    event_id: str


@dataclass(frozen=True, slots=True)
class CreateSubstitutionEvent:
    """Create one player substitution."""

    player_in_id: EntityId
    player_out_id: EntityId
    player_group_id: EntityId
    match_part_id: EntityId
    time: str | None
    minute: int | None


@dataclass(frozen=True, slots=True)
class UpdateSubstitutionEvent:
    """Apply a partial correction to one substitution."""

    event_id: str
    player_in_id: PatchValue[EntityId] = UNSET
    player_out_id: PatchValue[EntityId] = UNSET
    player_group_id: PatchValue[EntityId] = UNSET
    match_part_id: PatchValue[EntityId] = UNSET
    time: PatchValue[str] = UNSET
    minute: PatchValue[int] = UNSET


@dataclass(frozen=True, slots=True)
class DeleteSubstitutionEvent:
    """Delete one substitution from the editor timeline."""

    event_id: str


@dataclass(frozen=True, slots=True)
class CreatePauseEvent:
    """Create one generic match pause."""

    match_part_id: EntityId
    start_time: str | None
    minute: int | None
    length_seconds: int
    active: bool


@dataclass(frozen=True, slots=True)
class UpdatePauseEvent:
    """Apply a partial correction to one generic pause."""

    event_id: str
    match_part_id: PatchValue[EntityId] = UNSET
    start_time: PatchValue[str] = UNSET
    minute: PatchValue[int] = UNSET
    length_seconds: PatchValue[int] = UNSET
    active: PatchValue[bool] = UNSET


@dataclass(frozen=True, slots=True)
class DeletePauseEvent:
    """Delete one generic pause and any attached timeout."""

    event_id: str


@dataclass(frozen=True, slots=True)
class CreateTimeoutEvent:
    """Create a team timeout backed by a pause."""

    team_id: EntityId
    match_part_id: EntityId
    start_time: str | None
    minute: int | None
    length_seconds: int


@dataclass(frozen=True, slots=True)
class UpdateTimeoutEvent:
    """Apply a partial correction to one team timeout."""

    event_id: str
    team_id: PatchValue[EntityId] = UNSET
    match_part_id: PatchValue[EntityId] = UNSET
    start_time: PatchValue[str] = UNSET
    minute: PatchValue[int] = UNSET
    length_seconds: PatchValue[int] = UNSET


@dataclass(frozen=True, slots=True)
class DeleteTimeoutEvent:
    """Delete one timeout and its backing pause."""

    event_id: str


type EventEditorCommand = (
    CreateGoalEvent
    | UpdateGoalEvent
    | DeleteGoalEvent
    | CreateSubstitutionEvent
    | UpdateSubstitutionEvent
    | DeleteSubstitutionEvent
    | CreatePauseEvent
    | UpdatePauseEvent
    | DeletePauseEvent
    | CreateTimeoutEvent
    | UpdateTimeoutEvent
    | DeleteTimeoutEvent
)
type EditedEvent = Shot | PlayerChange | Pause | Timeout


@dataclass(frozen=True, slots=True)
class EventEditorResult:
    """Outcome of one serialized editor command."""

    match_data: MatchData
    event: EditedEvent | None
    revision: int
    found: bool = True


@dataclass(slots=True)
class EventEditorValidationError(Exception):
    """Provider-neutral validation details for the inbound API adapter."""

    errors: dict[str, object]


class _CommandOutcome(Enum):
    NOT_FOUND = "not_found"


type _AppliedCommand = EditedEvent | _CommandOutcome | None


def _validation_error(field: str, detail: str) -> NoReturn:
    raise EventEditorValidationError({field: detail})


def _ensure_aware(value: datetime) -> datetime:
    if timezone.is_aware(value):
        return value
    return timezone.make_aware(value, timezone.get_current_timezone())


def _validate_event_time_in_part(match_part: MatchPart, event_time: datetime) -> None:
    start = _ensure_aware(match_part.start_time)
    if event_time < start:
        _validation_error("time", "Event time is before the selected match part.")
    if match_part.end_time is not None and event_time > _ensure_aware(
        match_part.end_time
    ):
        _validation_error("time", "Event time is after the selected match part.")


def _resolve_event_time(
    *,
    match_part: MatchPart,
    time: str | None,
    minute: int | None,
    exclude_pause_id: object | None = None,
) -> datetime:
    if time:
        parsed = parse_datetime(time)
        if parsed is None:
            _validation_error("time", "Invalid datetime.")
        resolved = _ensure_aware(parsed)
        _validate_event_time_in_part(match_part, resolved)
        return resolved

    if minute is None:
        _validation_error("time", "Provide either 'time' (ISO datetime) or 'minute'.")
    if minute < 0:
        _validation_error("minute", "Minute must be >= 0.")

    period_offset = (match_part.part_number - 1) * match_part.match_data.part_length
    elapsed_seconds = (minute * 60) - period_offset
    if elapsed_seconds < 0:
        _validation_error("minute", "Minute is before the selected match part.")

    resolved = _ensure_aware(match_part.start_time) + timedelta(seconds=elapsed_seconds)
    pauses = Pause.objects.filter(
        match_part=match_part,
        start_time__isnull=False,
        end_time__isnull=False,
    ).order_by("start_time", "id_uuid")
    if exclude_pause_id is not None:
        pauses = pauses.exclude(pk=exclude_pause_id)
    for pause_start, pause_end in pauses.values_list("start_time", "end_time"):
        if pause_start <= resolved and pause_end > pause_start:
            resolved += pause_end - pause_start

    _validate_event_time_in_part(match_part, resolved)
    return resolved


def _match_part(match_data: MatchData, match_part_id: EntityId) -> MatchPart:
    match_part = MatchPart.objects.filter(
        id_uuid=match_part_id,
        match_data=match_data,
    ).first()
    if match_part is None:
        _validation_error("match_part_id", "Invalid match part.")
    return match_part


def _match_team(match_data: MatchData, team_id: EntityId) -> Team:
    match = match_data.match_link
    if str(team_id) == str(match.home_team_id):
        return match.home_team
    if str(team_id) == str(match.away_team_id):
        return match.away_team
    _validation_error("team_id", "Team is not part of this match.")


def _player(player_id: EntityId, *, field: str = "player_id") -> Player:
    player = Player.objects.filter(id_uuid=player_id).first()
    if player is None:
        _validation_error(field, "Unknown player.")
    return player


def _goal_type(shot_type_id: EntityId) -> GoalType:
    goal_type = GoalType.objects.filter(id_uuid=shot_type_id).first()
    if goal_type is None:
        _validation_error("shot_type_id", "Unknown goal type.")
    return goal_type


def _player_group(match_data: MatchData, player_group_id: EntityId) -> PlayerGroup:
    group = PlayerGroup.objects.filter(
        id_uuid=player_group_id,
        match_data=match_data,
    ).first()
    if group is None:
        _validation_error("player_group_id", "Invalid player group.")
    return group


def _validate_player_team(
    *,
    match_data: MatchData,
    player: Player,
    team_id: EntityId,
    field: str = "player_id",
) -> None:
    roster_team_id = (
        MatchPlayer.objects
        .filter(match_data=match_data, player=player)
        .values_list("team_id", flat=True)
        .first()
    )
    if roster_team_id is None:
        _validation_error(field, "Player is not on this match roster.")
    if str(roster_team_id) != str(team_id):
        _validation_error(field, "Player does not belong to the selected match team.")


def _responsible_player_team(
    *, match_data: MatchData, shooting_team: Team, for_team: bool
) -> Team:
    """Return the roster team expected for an editor-selected shot player."""
    if for_team:
        return shooting_team

    match = match_data.match_link
    if str(shooting_team.id_uuid) == str(match.home_team_id):
        return match.away_team
    if str(shooting_team.id_uuid) == str(match.away_team_id):
        return match.home_team
    _validation_error("team_id", "Team is not part of this match.")


def _validate_substitution_players(
    *,
    match_data: MatchData,
    player_in: Player,
    player_out: Player,
    team_id: EntityId,
) -> None:
    if player_in == player_out:
        _validation_error("player_in_id", "Incoming and outgoing player must differ.")
    _validate_player_team(
        match_data=match_data,
        player=player_in,
        team_id=team_id,
        field="player_in_id",
    )
    _validate_player_team(
        match_data=match_data,
        player=player_out,
        team_id=team_id,
        field="player_out_id",
    )


def _patch_value[T](value: PatchValue[T]) -> T | None:
    return None if value is UNSET else value


def _timing_requested(*values: object) -> bool:
    return any(value is not UNSET for value in values)


@singledispatch
def _apply_command(
    command: object,
    match_data: MatchData,
) -> _AppliedCommand:
    raise TypeError(f"Unsupported editor command: {type(command).__name__}")


@_apply_command.register
def _create_goal(command: CreateGoalEvent, match_data: MatchData) -> Shot:
    match_part = _match_part(match_data, command.match_part_id)
    team = _match_team(match_data, command.team_id)
    player = _player(command.player_id)
    responsible_team = _responsible_player_team(
        match_data=match_data,
        shooting_team=team,
        for_team=command.for_team,
    )
    _validate_player_team(
        match_data=match_data,
        player=player,
        team_id=responsible_team.id_uuid,
    )
    return Shot.objects.create(
        match_data=match_data,
        match_part=match_part,
        player=player,
        team=team,
        shot_type=_goal_type(command.shot_type_id),
        for_team=command.for_team,
        scored=command.scored,
        time=_resolve_event_time(
            match_part=match_part,
            time=command.time,
            minute=command.minute,
        ),
    )


@_apply_command.register
def _update_goal(
    command: UpdateGoalEvent,
    match_data: MatchData,
) -> Shot | _CommandOutcome:
    shot = (
        Shot.objects
        .select_related("match_part", "player", "team", "shot_type")
        .filter(id_uuid=command.event_id, match_data=match_data)
        .first()
    )
    if shot is None:
        return _CommandOutcome.NOT_FOUND

    match_part = (
        _match_part(match_data, command.match_part_id)
        if command.match_part_id is not UNSET
        else shot.match_part
    )
    if match_part is None:
        _validation_error("match_part_id", "Invalid match part.")
    if _timing_requested(command.match_part_id, command.time, command.minute):
        shot.match_part = match_part
        shot.time = _resolve_event_time(
            match_part=match_part,
            time=_patch_value(command.time),
            minute=_patch_value(command.minute),
        )

    team = (
        _match_team(match_data, command.team_id)
        if command.team_id is not UNSET
        else shot.team
    )
    if team is None:
        _validation_error("team_id", "Team is not part of this match.")
    player = (
        _player(command.player_id) if command.player_id is not UNSET else shot.player
    )
    for_team = (
        bool(command.for_team) if command.for_team is not UNSET else bool(shot.for_team)
    )
    responsible_team = _responsible_player_team(
        match_data=match_data,
        shooting_team=team,
        for_team=for_team,
    )
    _validate_player_team(
        match_data=match_data,
        player=player,
        team_id=responsible_team.id_uuid,
    )
    shot.player = player
    shot.team = team
    shot.for_team = for_team

    if command.shot_type_id is not UNSET:
        shot.shot_type = _goal_type(command.shot_type_id)
    if command.scored is not UNSET:
        shot.scored = command.scored
    shot.save()
    return shot


@_apply_command.register
def _delete_goal(
    command: DeleteGoalEvent,
    match_data: MatchData,
) -> _CommandOutcome | None:
    shot = Shot.objects.filter(id_uuid=command.event_id, match_data=match_data).first()
    if shot is None:
        return _CommandOutcome.NOT_FOUND
    shot.delete()
    return None


@_apply_command.register
def _create_substitution(
    command: CreateSubstitutionEvent,
    match_data: MatchData,
) -> PlayerChange:
    match_part = _match_part(match_data, command.match_part_id)
    group = _player_group(match_data, command.player_group_id)
    player_in = _player(command.player_in_id, field="player_in_id")
    player_out = _player(command.player_out_id, field="player_out_id")
    _validate_substitution_players(
        match_data=match_data,
        player_in=player_in,
        player_out=player_out,
        team_id=group.team_id,
    )
    return PlayerChange.objects.create(
        match_data=match_data,
        match_part=match_part,
        player_group=group,
        player_in=player_in,
        player_out=player_out,
        time=_resolve_event_time(
            match_part=match_part,
            time=command.time,
            minute=command.minute,
        ),
    )


@_apply_command.register
def _update_substitution(
    command: UpdateSubstitutionEvent,
    match_data: MatchData,
) -> PlayerChange | _CommandOutcome:
    change = (
        PlayerChange.objects
        .select_related(
            "match_part",
            "player_group",
            "player_in",
            "player_out",
        )
        .filter(id_uuid=command.event_id, player_group__match_data=match_data)
        .first()
    )
    if change is None:
        return _CommandOutcome.NOT_FOUND

    if _timing_requested(command.match_part_id, command.time, command.minute):
        match_part = (
            _match_part(match_data, command.match_part_id)
            if command.match_part_id is not UNSET
            else change.match_part
        )
        if match_part is None:
            _validation_error("match_part_id", "Invalid match part.")
        change.match_part = match_part
        change.time = _resolve_event_time(
            match_part=match_part,
            time=_patch_value(command.time),
            minute=_patch_value(command.minute),
        )

    group = (
        _player_group(match_data, command.player_group_id)
        if command.player_group_id is not UNSET
        else change.player_group
    )
    player_in = (
        _player(command.player_in_id, field="player_in_id")
        if command.player_in_id is not UNSET
        else change.player_in
    )
    player_out = (
        _player(command.player_out_id, field="player_out_id")
        if command.player_out_id is not UNSET
        else change.player_out
    )
    if player_in is None or player_out is None:
        _validation_error(
            "detail",
            "Substitution requires incoming and outgoing players.",
        )
    _validate_substitution_players(
        match_data=match_data,
        player_in=player_in,
        player_out=player_out,
        team_id=group.team_id,
    )
    change.player_group = group
    change.player_in = player_in
    change.player_out = player_out
    change.save()
    return change


@_apply_command.register
def _delete_substitution(
    command: DeleteSubstitutionEvent,
    match_data: MatchData,
) -> _CommandOutcome | None:
    change = PlayerChange.objects.filter(
        id_uuid=command.event_id,
        player_group__match_data=match_data,
    ).first()
    if change is None:
        return _CommandOutcome.NOT_FOUND
    change.delete()
    return None


def _pause_times(
    *,
    match_part: MatchPart,
    start_time: str | None,
    minute: int | None,
    length_seconds: int,
    exclude_pause_id: object | None = None,
) -> tuple[datetime, datetime | None]:
    start = _resolve_event_time(
        match_part=match_part,
        time=start_time,
        minute=minute,
        exclude_pause_id=exclude_pause_id,
    )
    end = start + timedelta(seconds=length_seconds) if length_seconds else None
    if end is not None:
        _validate_event_time_in_part(match_part, end)
    return start, end


@_apply_command.register
def _create_pause(command: CreatePauseEvent, match_data: MatchData) -> Pause:
    match_part = _match_part(match_data, command.match_part_id)
    start, end = _pause_times(
        match_part=match_part,
        start_time=command.start_time,
        minute=command.minute,
        length_seconds=command.length_seconds,
    )
    return Pause.objects.create(
        match_data=match_data,
        match_part=match_part,
        start_time=start,
        end_time=end,
        active=command.active,
    )


def _apply_pause_patch(
    *,
    pause: Pause,
    match_data: MatchData,
    command: UpdatePauseEvent | UpdateTimeoutEvent,
) -> None:
    if command.match_part_id is not UNSET:
        pause.match_part = _match_part(match_data, command.match_part_id)

    if not _timing_requested(
        command.start_time,
        command.minute,
        command.length_seconds,
    ):
        return
    match_part = pause.match_part
    if match_part is None:
        _validation_error("match_part_id", "Pause has no match part.")

    if _timing_requested(command.start_time, command.minute):
        start = _resolve_event_time(
            match_part=match_part,
            time=_patch_value(command.start_time),
            minute=_patch_value(command.minute),
            exclude_pause_id=pause.pk,
        )
    else:
        start = pause.start_time
    if start is None:
        _validation_error("start_time", "Pause has no start.")

    duration = (
        command.length_seconds
        if command.length_seconds is not UNSET
        else int(pause.length().total_seconds())
    )
    pause.start_time = start
    pause.end_time = start + timedelta(seconds=duration) if duration else None
    if pause.end_time is not None:
        _validate_event_time_in_part(match_part, pause.end_time)


@_apply_command.register
def _update_pause(
    command: UpdatePauseEvent,
    match_data: MatchData,
) -> Pause | _CommandOutcome:
    pause = (
        Pause.objects
        .select_related("match_part")
        .filter(id_uuid=command.event_id, match_data=match_data)
        .first()
    )
    if pause is None:
        return _CommandOutcome.NOT_FOUND
    _apply_pause_patch(
        pause=pause,
        match_data=match_data,
        command=command,
    )
    if command.active is not UNSET:
        pause.active = command.active
    pause.save()
    return pause


@_apply_command.register
def _delete_pause(
    command: DeletePauseEvent,
    match_data: MatchData,
) -> _CommandOutcome | None:
    pause = Pause.objects.filter(
        id_uuid=command.event_id, match_data=match_data
    ).first()
    if pause is None:
        return _CommandOutcome.NOT_FOUND
    Timeout.objects.filter(pause=pause).delete()
    pause.delete()
    return None


@_apply_command.register
def _create_timeout(command: CreateTimeoutEvent, match_data: MatchData) -> Timeout:
    match_part = _match_part(match_data, command.match_part_id)
    start, end = _pause_times(
        match_part=match_part,
        start_time=command.start_time,
        minute=command.minute,
        length_seconds=command.length_seconds,
    )
    pause = Pause.objects.create(
        match_data=match_data,
        match_part=match_part,
        start_time=start,
        end_time=end,
        active=False,
    )
    return Timeout.objects.create(
        match_data=match_data,
        match_part=match_part,
        team=_match_team(match_data, command.team_id),
        pause=pause,
    )


@_apply_command.register
def _update_timeout(
    command: UpdateTimeoutEvent,
    match_data: MatchData,
) -> Timeout | _CommandOutcome:
    timeout = (
        Timeout.objects
        .select_related("pause", "pause__match_part", "team")
        .filter(
            Q(id_uuid=command.event_id) | Q(pause_id=command.event_id),
            match_data=match_data,
        )
        .first()
    )
    if timeout is None:
        return _CommandOutcome.NOT_FOUND
    if timeout.pause is None:
        _validation_error("pause", "Timeout has no pause.")

    if command.team_id is not UNSET:
        timeout.team = _match_team(match_data, command.team_id)
    _apply_pause_patch(
        pause=timeout.pause,
        match_data=match_data,
        command=command,
    )
    if command.match_part_id is not UNSET:
        timeout.match_part = timeout.pause.match_part
    timeout.pause.save()
    timeout.save()
    return timeout


@_apply_command.register
def _delete_timeout(
    command: DeleteTimeoutEvent,
    match_data: MatchData,
) -> _CommandOutcome | None:
    timeout = (
        Timeout.objects
        .select_related("pause")
        .filter(
            Q(id_uuid=command.event_id) | Q(pause_id=command.event_id),
            match_data=match_data,
        )
        .first()
    )
    if timeout is None:
        return _CommandOutcome.NOT_FOUND
    pause = timeout.pause
    timeout.delete()
    if pause is not None:
        pause.delete()
    return None


def apply_event_editor_command(
    *,
    match_data_id: object,
    expected_revision: int,
    actor: object | None,
    command: EventEditorCommand,
    publisher: MatchChangePublisher,
) -> EventEditorResult:
    """Validate and apply one event correction under the aggregate lock."""
    match_data, applied = apply_editor_mutation(
        context=EditorMutationContext(
            match_data_id=match_data_id,
            expected_revision=expected_revision,
            actor=actor,
            publisher=publisher,
        ),
        mutate=lambda locked: _apply_command(command, locked),
        no_op_result=_CommandOutcome.NOT_FOUND,
    )
    if applied is _CommandOutcome.NOT_FOUND:
        return EventEditorResult(
            match_data=match_data,
            event=None,
            revision=match_data.live_revision,
            found=False,
        )
    return EventEditorResult(
        match_data=match_data,
        revision=match_data.live_revision,
        event=(
            applied
            if isinstance(applied, (Shot, PlayerChange, Pause, Timeout))
            else None
        ),
    )
