"""HTTP-friendly match tracker helpers and mutation commands."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
import hashlib
import json
import logging
from typing import Any, Protocol, cast
from uuid import UUID

from bg_uuidv7 import uuidv7
from django.core.exceptions import ValidationError
from django.db import models, transaction
from django.utils import timezone
from django.utils.module_loading import import_string

from apps.game_tracker.models import (
    Attack,
    GoalType,
    MatchData,
    MatchEvent,
    MatchPart,
    Pause,
    PlayerChange,
    PlayerGroup,
    Shot,
    ShotEventDetail,
    Timeout,
    TrackerCommand,
)
from apps.game_tracker.realtime.contracts import ALL_LIVE_RESOURCES, LiveResource
from apps.game_tracker.services.event_reconciliation import (
    ShotObservation,
    create_reconciliation_candidates,
    plan_shot_reconciliation,
    record_matched_observation,
)
from apps.game_tracker.services.lineup_projections import (
    capture_starting_lineup,
    rebuild_current_lineup,
    rebuild_group_roles,
)
from apps.game_tracker.services.live_update_signal_control import (
    suppress_live_update_signals,
)
from apps.game_tracker.services.live_updates import (
    record_match_change,
    summarize_match_changes,
)
from apps.game_tracker.services.match_event_context import (
    MatchEventClient,
    match_event_context,
)
from apps.game_tracker.services.match_mutations import locked_match_mutation
from apps.game_tracker.services.match_scores import compute_scores_for_matchdata_ids
from apps.game_tracker.services.match_timeline_payload import (
    build_match_events,
    build_match_shots,
)
from apps.game_tracker.services.player_groups import (
    RESERVE_GROUP_NAME,
    get_reserve_group,
)
from apps.player.models import Player
from apps.player.services.goal_song_manifest import build_goal_song_manifest
from apps.schedule.models import Match
from apps.team.models.team import Team


logger = logging.getLogger(__name__)


MATCH_TRACKER_DATA_NOT_FOUND = "Match tracker data not found."
MATCH_IS_PAUSED_MESSAGE = "match is paused"
NO_ACTIVE_MATCH_PART_MESSAGE = "No active match part."


_CLIENT_TIME_MAX_SKEW_SECONDS = 5 * 60
_CLIENT_ID_MAX_LENGTH = 128
_CLIENT_SOURCE_MAX_LENGTH = 32
_MAX_TIMEOUTS_PER_TEAM = 2
_SERVER_TIMED_COMMANDS = frozenset({"start/pause", "part_end", "timeout"})
_MUTATING_COMMANDS = frozenset({
    "start/pause",
    "part_end",
    "timeout",
    "new_attack",
    "shot_reg",
    "goal_reg",
    "substitute_reg",
    "substitute_against_reg",
    "remove_last_event",
})
_COMMAND_RESOURCES: dict[str, frozenset[LiveResource]] = {
    "start/pause": frozenset({
        LiveResource.LIVE,
        LiveResource.TRACKER,
        LiveResource.EVENTS,
    }),
    "part_end": frozenset(ALL_LIVE_RESOURCES),
    "timeout": frozenset({
        LiveResource.LIVE,
        LiveResource.TRACKER,
        LiveResource.EVENTS,
    }),
    "new_attack": frozenset({LiveResource.TRACKER, LiveResource.EVENTS}),
    "shot_reg": frozenset({
        LiveResource.TRACKER,
        LiveResource.EVENTS,
        LiveResource.SHOTS,
        LiveResource.STATS,
        LiveResource.IMPACTS,
    }),
    "goal_reg": frozenset({
        LiveResource.LIVE,
        LiveResource.TRACKER,
        LiveResource.SUMMARY,
        LiveResource.EVENTS,
        LiveResource.SHOTS,
        LiveResource.STATS,
        LiveResource.IMPACTS,
        LiveResource.MVP,
    }),
    "substitute_reg": frozenset({
        LiveResource.TRACKER,
        LiveResource.EVENTS,
        LiveResource.PLAYER_GROUPS,
        LiveResource.STATS,
        LiveResource.IMPACTS,
    }),
    "substitute_against_reg": frozenset({
        LiveResource.TRACKER,
        LiveResource.EVENTS,
        LiveResource.STATS,
        LiveResource.IMPACTS,
    }),
    "remove_last_event": frozenset(ALL_LIVE_RESOURCES),
}


def _parse_client_time_iso(value: str) -> datetime | None:
    """Parse a client-supplied ISO timestamp.

    Notes:
        - Accepts both offset timestamps and `Z` suffix.
        - If timezone is omitted, assume UTC.

    """
    try:
        normalized = value.strip().replace("Z", "+00:00")
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None

    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _command_time_from_payload(payload: dict[str, Any]) -> datetime:
    """Best-effort event time for a command.

    We prefer a client timestamp (so UI actions are ordered consistently), but
    fall back to server time if missing/invalid or wildly skewed.
    """
    server_now = datetime.now(UTC)

    client_time: datetime | None = None

    client_time_ms = payload.get("client_time_ms")
    if isinstance(client_time_ms, int):
        try:
            client_time = datetime.fromtimestamp(client_time_ms / 1000, tz=UTC)
        except (OSError, OverflowError, ValueError):
            client_time = None

    client_time_iso = payload.get("client_time_iso")
    if client_time is None and isinstance(client_time_iso, str):
        client_time = _parse_client_time_iso(client_time_iso)

    if client_time is None:
        return server_now

    skew_seconds = abs((client_time - server_now).total_seconds())
    if skew_seconds > _CLIENT_TIME_MAX_SKEW_SECONDS:
        return server_now

    return client_time


class TrackerCommandError(RuntimeError):
    """Raised when a tracker command cannot be applied."""

    def __init__(
        self,
        message: str,
        *,
        code: str = "error",
        details: dict[str, object] | None = None,
    ) -> None:
        """Create an error that can be mapped to an API response."""
        super().__init__(message)
        self.code = code
        self.details = details or {}


def _other_team(match: Match, team: Team) -> Team:
    home_team_id = cast(Any, match).home_team_id
    if home_team_id == team.id_uuid:
        return match.away_team
    if cast(Any, match).away_team_id == team.id_uuid:
        return match.home_team
    raise TrackerCommandError(
        "Team is not participating in this match.",
        code="invalid_team",
    )


def _current_part(match_data: MatchData) -> MatchPart | None:
    return (
        MatchPart.objects
        .filter(match_data=match_data, active=True)
        .order_by("-start_time", "-id_uuid")
        .first()
    )


def _is_paused(match_data: MatchData, current_part: MatchPart | None) -> bool:
    if match_data.status != "active":
        return True
    if not current_part:
        # Match marked active but no active part => treat as paused like the WS
        # consumer does (button shows Start).
        return True
    return Pause.objects.filter(
        match_data=match_data,
        active=True,
        match_part=current_part,
    ).exists()


def _timer_data(
    match_data: MatchData,
    current_part: MatchPart | None,
) -> dict[str, Any]:
    if not current_part:
        return {
            "type": "deactivated",
            "match_data_id": str(match_data.id_uuid),
        }

    active_pause = Pause.objects.filter(
        match_data=match_data,
        active=True,
        match_part=current_part,
    ).first()

    pauses = Pause.objects.filter(
        match_data=match_data,
        active=False,
        match_part=current_part,
    )
    pause_time = sum(pause.length().total_seconds() for pause in pauses)

    base: dict[str, Any] = {
        "match_data_id": str(match_data.id_uuid),
        "time": current_part.start_time.isoformat(),
        "length": match_data.part_length,
        "pause_length": pause_time,
        "server_time": datetime.now(UTC).isoformat(),
    }

    if active_pause and active_pause.start_time:
        return {
            **base,
            "type": "pause",
            "calc_to": active_pause.start_time.isoformat(),
        }

    return {
        **base,
        "type": "active",
    }


def _score(match_data: MatchData, *, team: Team, opponent: Team) -> tuple[int, int]:
    totals = (
        Shot.objects
        .filter(match_data=match_data, scored=True, team__in=[team, opponent])
        .values("team")
        .annotate(count=models.Count("id_uuid"))
    )
    goals_by_team = {row["team"]: row["count"] for row in totals}
    return goals_by_team.get(team.id_uuid, 0), goals_by_team.get(opponent.id_uuid, 0)


def _player_stats_by_team(
    match_data: MatchData,
    *,
    team: Team,
    opponent: Team,
) -> dict[str, dict[str, int]]:
    player_stats: dict[str, dict[str, int]] = {}
    shot_totals = (
        Shot.objects
        .filter(match_data=match_data, team__in=[team, opponent])
        .values("player_id", "team_id")
        .annotate(
            shots=models.Count("id_uuid"),
            goals=models.Count("id_uuid", filter=models.Q(scored=True)),
        )
    )

    for row in shot_totals:
        player_id = str(row["player_id"])
        team_id = row["team_id"]
        stats = player_stats.setdefault(
            player_id,
            {
                "shots_for": 0,
                "shots_against": 0,
                "goals_for": 0,
                "goals_against": 0,
            },
        )
        if team_id == team.id_uuid:
            stats["shots_for"] = row["shots"]
            stats["goals_for"] = row["goals"]
            continue

        stats["shots_against"] = row["shots"]
        stats["goals_against"] = row["goals"]

    return player_stats


def _player_groups_payload(
    match_data: MatchData,
    *,
    team: Team,
    opponent: Team,
) -> list[dict[str, Any]]:
    player_stats = _player_stats_by_team(match_data, team=team, opponent=opponent)
    player_groups = (
        PlayerGroup.objects
        .select_related("starting_type", "current_type")
        .prefetch_related("players__user")
        .filter(match_data=match_data, team=team)
        .exclude(starting_type__name=RESERVE_GROUP_NAME)
        .order_by(
            # Put the currently attacking group first.
            # (Same idea as the WS consumer ordering.)
            "current_type__name",
            "starting_type__name",
        )
    )

    # We want Aanval first, then Verdediging. Ordering by name isn't stable in
    # all locales, so we reorder in Python.
    ordered: list[PlayerGroup] = []
    aanval = [pg for pg in player_groups if pg.current_type.name == "Aanval"]
    verdediging = [pg for pg in player_groups if pg.current_type.name == "Verdediging"]
    ordered.extend(aanval)
    ordered.extend(verdediging)

    result: list[dict[str, Any]] = []
    for pg in ordered:
        players_payload: list[dict[str, Any]] = []
        for p in pg.players.all():
            stats = player_stats.get(
                str(p.id_uuid),
                {
                    "shots_for": 0,
                    "shots_against": 0,
                    "goals_for": 0,
                    "goals_against": 0,
                },
            )

            players_payload.append({
                "id": str(p.id_uuid),
                "name": p.user.username,
                **stats,
            })

        result.append({
            "id": str(pg.id_uuid),
            "starting_type": pg.starting_type.name,
            "current_type": pg.current_type.name,
            "players": players_payload,
        })
    return result


def _reserve_players_payload(
    match_data: MatchData,
    *,
    team: Team,
) -> list[dict[str, Any]]:
    reserve_group = (
        PlayerGroup.objects
        .prefetch_related("players__user")
        .filter(
            match_data=match_data,
            team=team,
            starting_type__name=RESERVE_GROUP_NAME,
        )
        .first()
    )
    if not reserve_group:
        return []
    return [
        {"id": str(p.id_uuid), "name": p.user.username}
        for p in reserve_group.players.all()
    ]


def _get_last_event_model(match_data: MatchData) -> object | None:
    """Resolve the newest undoable fact from its committed event order."""
    events = (
        MatchEvent.objects
        .filter(
            match_data=match_data,
            status=MatchEvent.STATUS_ACTIVE,
            source_type__in={"shot", "player_change", "pause", "attack"},
        )
        .order_by("-sequence")
        .values_list("source_type", "source_id")
    )
    for source_type, source_id in events:
        if source_type == "shot":
            event = (
                Shot.objects
                .select_related(
                    "player",
                    "player__user",
                    "shot_type",
                    "match_part",
                    "team",
                )
                .filter(match_data=match_data, pk=source_id)
                .first()
            )
        elif source_type == "player_change":
            event = (
                PlayerChange.objects
                .select_related(
                    "player_in",
                    "player_in__user",
                    "player_out",
                    "player_out__user",
                    "player_group",
                    "match_part",
                )
                .filter(match_data=match_data, pk=source_id)
                .first()
            )
        elif source_type == "pause":
            event = (
                Pause.objects
                .select_related("match_part")
                .filter(match_data=match_data, pk=source_id)
                .first()
            )
        else:
            event = (
                Attack.objects
                .select_related("team")
                .filter(match_data=match_data, pk=source_id)
                .first()
            )
        if event is not None:
            return event
    return None


def _last_event_payload(
    match_data: MatchData,
    *,
    team: Team,
    opponent: Team,
) -> dict[str, Any]:
    event = _get_last_event_model(match_data)
    if not event:
        return {"type": "no_event"}

    goals_for, goals_against = _score(match_data, team=team, opponent=opponent)

    if isinstance(event, Shot):
        return _serialize_last_event_shot(
            event,
            team=team,
            goals_for=goals_for,
            goals_against=goals_against,
        )
    if isinstance(event, PlayerChange):
        return _serialize_last_event_player_change(event)
    if isinstance(event, Pause):
        return _serialize_last_event_pause(event)
    if isinstance(event, Attack):
        return _serialize_last_event_attack(event)
    return {"type": "no_event"}


def _serialize_last_event_shot(
    event: Shot,
    *,
    team: Team,
    goals_for: int,
    goals_against: int,
) -> dict[str, Any]:
    if not event.time:
        return {"type": "no_event"}

    team_id = cast(Any, event).team_id
    common: dict[str, Any] = {
        "id": str(event.id_uuid),
        "player": event.player.user.username,
        "player_id": str(event.player.id_uuid),
        "for_team": bool(team_id == team.id_uuid),
        "team_id": str(team_id) if team_id else None,
        "time_iso": event.time.isoformat(),
        "time": event.time.isoformat(),
    }

    if event.scored and event.shot_type:
        return {
            **common,
            "type": "goal",
            "name": "Gescoord",
            "shot_type": event.shot_type.name,
            "shot_type_id": str(event.shot_type.id_uuid),
            "goals_for": goals_for,
            "goals_against": goals_against,
        }

    return {
        **common,
        "type": "shot",
        "name": "Schot",
    }


def _serialize_last_event_player_change(event: PlayerChange) -> dict[str, Any]:
    if not event.time:
        return {"type": "no_event"}
    if not event.player_in or not event.player_out:
        return {
            "type": "substitute",
            "id": str(event.id_uuid),
            "name": "Wissel tegenstander",
            "player_in": None,
            "player_in_id": None,
            "player_out": None,
            "player_out_id": None,
            "player_group_id": str(event.player_group.id_uuid),
            "time_iso": event.time.isoformat(),
            "time": event.time.isoformat(),
        }
    return {
        "type": "substitute",
        "id": str(event.id_uuid),
        "name": "Wissel",
        "player_in": event.player_in.user.username,
        "player_in_id": str(event.player_in.id_uuid),
        "player_out": event.player_out.user.username,
        "player_out_id": str(event.player_out.id_uuid),
        "player_group_id": str(event.player_group.id_uuid),
        "time_iso": event.time.isoformat(),
        "time": event.time.isoformat(),
    }


def _serialize_last_event_pause(event: Pause) -> dict[str, Any]:
    if not event.start_time:
        return {"type": "no_event"}
    timeout = Timeout.objects.select_related("team").filter(pause=event).first()
    return {
        "type": "pause",
        "id": str(event.id_uuid),
        "pause_id": str(event.id_uuid),
        "name": "Time-out" if timeout else "Pauze",
        "event_kind": "timeout" if timeout else "pause",
        "team_id": str(timeout.team_id) if timeout and timeout.team_id else None,
        "start_time": event.start_time.isoformat(),
        "end_time": event.end_time.isoformat() if event.end_time else None,
        "active": event.active,
    }


def _serialize_last_event_attack(event: Attack) -> dict[str, Any]:
    if not event.time:
        return {"type": "no_event"}

    team_id = cast(Any, event).team_id
    return {
        "type": "attack",
        "id": str(event.id_uuid),
        "name": "Aanval",
        "team": event.team.name if event.team else None,
        "team_id": str(team_id) if team_id else None,
        "time_iso": event.time.isoformat(),
        "time": event.time.isoformat(),
    }


def _last_changed_at(match_data: MatchData) -> datetime:
    """Return the durable marker shared by polling and SSE recovery."""
    return match_data.live_changed_at


def get_tracker_state(match: Match, *, team: Team) -> dict[str, Any]:
    """Return a snapshot of the current tracker state.

    Raises:
        TrackerCommandError: If the tracker data for the match does not exist.

    """
    opponent = _other_team(match, team)
    match_data = MatchData.objects.filter(match_link=match).first()
    if not match_data:
        raise TrackerCommandError(MATCH_TRACKER_DATA_NOT_FOUND, code="not_found")

    current_part = _current_part(match_data)

    goals_for, goals_against = _score(match_data, team=team, opponent=opponent)
    paused = _is_paused(match_data, current_part)

    start_stop_label = "Start"
    if match_data.status == "active" and not paused:
        start_stop_label = "Pauze"

    goal_types = list(GoalType.objects.order_by("name"))

    substitutions_max = 8
    substitutions_counts = (
        PlayerChange.objects
        .filter(match_data=match_data, player_group__team__in=[team, opponent])
        .values("player_group__team")
        .annotate(count=models.Count("id_uuid"))
    )
    substitutions_by_team = {
        row["player_group__team"]: row["count"] for row in substitutions_counts
    }
    substitutions_for = substitutions_by_team.get(team.id_uuid, 0)
    substitutions_against = substitutions_by_team.get(opponent.id_uuid, 0)
    substitutions_total = substitutions_for + substitutions_against

    timeouts_max = 2
    timeouts_counts = (
        Timeout.objects
        .filter(match_data=match_data, team__in=[team, opponent])
        .values("team")
        .annotate(count=models.Count("id_uuid"))
    )
    timeouts_by_team = {row["team"]: row["count"] for row in timeouts_counts}
    timeouts_for = timeouts_by_team.get(team.id_uuid, 0)
    timeouts_against = timeouts_by_team.get(opponent.id_uuid, 0)

    player_groups = _player_groups_payload(
        match_data,
        team=team,
        opponent=opponent,
    )
    reserve_players = _reserve_players_payload(match_data, team=team)
    player_ids = [
        player["id"] for group in player_groups for player in group["players"]
    ]
    player_ids.extend(player["id"] for player in reserve_players)

    state: dict[str, Any] = {
        "match_id": str(match.id_uuid),
        "match_data_id": str(match_data.id_uuid),
        "status": match_data.status,
        "parts": match_data.parts,
        "current_part": match_data.current_part,
        "part_length": match_data.part_length,
        "team": {
            "id": str(team.id_uuid),
            "name": team.name,
            "club": team.club.name,
        },
        "opponent": {
            "id": str(opponent.id_uuid),
            "name": opponent.name,
            "club": opponent.club.name,
        },
        "score": {
            "for": goals_for,
            "against": goals_against,
        },
        "substitutions": {
            "for": substitutions_for,
            "against": substitutions_against,
            "max": substitutions_max,
        },
        "timeouts": {
            "for": timeouts_for,
            "against": timeouts_against,
            "max": timeouts_max,
        },
        "substitutions_total": substitutions_total,
        "paused": paused,
        "start_stop_label": start_stop_label,
        "timer": _timer_data(match_data, current_part),
        "player_groups": player_groups,
        "reserve_players": reserve_players,
        "goal_audio": build_goal_song_manifest(
            player_ids=player_ids,
            team=team,
            season=match.season,
        ),
        "goal_types": [{"id": str(gt.id_uuid), "name": gt.name} for gt in goal_types],
        "last_event": _last_event_payload(match_data, team=team, opponent=opponent),
        "last_changed_at": _last_changed_at(match_data).isoformat(),
        "live_revision": match_data.live_revision,
        "command_sequence": match_data.command_sequence,
    }

    return state


_TRACKER_CONFIGURATION_KEYS = frozenset({
    "match_id",
    "match_data_id",
    "team",
    "opponent",
    "goal_types",
    "goal_audio",
    "live_revision",
    "last_changed_at",
})


def compact_tracker_state(
    state: dict[str, Any],
    *,
    resources: list[str],
) -> dict[str, Any]:
    """Return a patch that reuses configuration from the initial snapshot."""
    return {
        "changed": True,
        "live_revision": state["live_revision"],
        "last_changed_at": state["last_changed_at"],
        "resources": resources,
        "patch": {
            key: value
            for key, value in state.items()
            if key not in _TRACKER_CONFIGURATION_KEYS and key != "resources"
        },
    }


def _require_not_paused(
    match_data: MatchData,
    team: Team,
    match: Match,
) -> tuple[MatchPart, Team]:
    current_part = _current_part(match_data)
    opponent = _other_team(match, team)
    if _is_paused(match_data, current_part):
        raise TrackerCommandError(MATCH_IS_PAUSED_MESSAGE, code="match_paused")
    if not current_part:
        raise TrackerCommandError(
            NO_ACTIVE_MATCH_PART_MESSAGE,
            code="no_active_part",
        )
    return current_part, opponent


@dataclass(frozen=True, slots=True)
class _TrackerCommandContext:
    match: Match
    match_data: MatchData
    team: Team
    event_time: datetime


class _TrackerCommand(Protocol):
    def apply(self, context: _TrackerCommandContext) -> None:
        """Apply the command against the locked tracker state."""


@dataclass(frozen=True, slots=True)
class _CommandMetadata:
    command_id: UUID | None
    expected_revision: int | None
    payload_hash: str
    source: str
    device_id: str
    session_id: str
    client_sequence: int | None


def _client_command_metadata(
    payload: dict[str, Any],
) -> tuple[str, str, str, int | None]:
    def client_string(field: str, *, default: str = "") -> str:
        value = payload.get(field, default)
        if not isinstance(value, str) or len(value) > _CLIENT_ID_MAX_LENGTH:
            raise TrackerCommandError(f"Invalid {field}.", code="bad_request")
        return value.strip()

    source = client_string("client_source", default="tracker")
    if not source or len(source) > _CLIENT_SOURCE_MAX_LENGTH:
        raise TrackerCommandError("Invalid client_source.", code="bad_request")
    device_id = client_string("device_id")
    session_id = client_string("session_id")
    client_sequence_raw = payload.get("client_sequence")
    if client_sequence_raw is None:
        return source, device_id, session_id, None
    if (
        isinstance(client_sequence_raw, bool)
        or not isinstance(client_sequence_raw, int)
        or client_sequence_raw < 0
        or not device_id
    ):
        raise TrackerCommandError("Invalid client_sequence.", code="bad_request")
    return source, device_id, session_id, client_sequence_raw


def _command_metadata(payload: dict[str, Any]) -> _CommandMetadata:
    """Parse idempotency metadata and hash the command's business payload.

    Raises:
        TrackerCommandError: If command metadata is malformed.

    """
    command_id_raw = payload.get("command_id")
    command_id: UUID | None = None
    if command_id_raw is not None:
        if not isinstance(command_id_raw, str):
            raise TrackerCommandError("Invalid command_id.", code="bad_request")
        try:
            command_id = UUID(command_id_raw)
        except ValueError as exc:
            raise TrackerCommandError(
                "Invalid command_id.", code="bad_request"
            ) from exc

    expected_revision_raw = payload.get("expected_revision")
    expected_revision: int | None = None
    if expected_revision_raw is not None:
        if (
            isinstance(expected_revision_raw, bool)
            or not isinstance(expected_revision_raw, int)
            or expected_revision_raw < 0
        ):
            raise TrackerCommandError("Invalid expected_revision.", code="bad_request")
        expected_revision = expected_revision_raw

    source, device_id, session_id, client_sequence = _client_command_metadata(payload)

    business_payload = {
        key: value
        for key, value in payload.items()
        if key
        not in {
            "client_sequence",
            "client_source",
            "command_id",
            "device_id",
            "expected_revision",
            "client_time_ms",
            "session_id",
        }
    }
    encoded = json.dumps(
        business_payload,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode()
    return _CommandMetadata(
        command_id=command_id,
        expected_revision=expected_revision,
        payload_hash=hashlib.sha256(encoded).hexdigest(),
        source=source,
        device_id=device_id,
        session_id=session_id,
        client_sequence=client_sequence,
    )


def _register_tracker_command(
    *,
    match_data: MatchData,
    team: Team,
    command: str,
    metadata: _CommandMetadata,
    actor: object | None,
) -> tuple[TrackerCommand | None, bool]:
    """Register a transition and identify an exact idempotent replay.

    Raises:
        TrackerCommandError: If the idempotency key conflicts or state is stale.

    """
    if command not in _MUTATING_COMMANDS:
        return None, False

    if metadata.command_id is not None:
        previous = TrackerCommand.objects.filter(command_id=metadata.command_id).first()
        if previous is not None:
            if (
                previous.match_data_id != match_data.pk
                or previous.team_id != team.id_uuid
                or previous.command != command
                or previous.payload_hash != metadata.payload_hash
            ):
                raise TrackerCommandError(
                    "command_id was already used for a different command.",
                    code="idempotency_conflict",
                    details={"command_id": str(metadata.command_id)},
                )
            return previous, True

    if metadata.device_id and metadata.client_sequence is not None:
        prior_sequence = TrackerCommand.objects.filter(
            match_data=match_data,
            device_id=metadata.device_id,
            client_sequence=metadata.client_sequence,
        ).first()
        if prior_sequence is not None:
            raise TrackerCommandError(
                "client_sequence was already used by another command.",
                code="client_sequence_conflict",
                details={
                    "client_sequence": metadata.client_sequence,
                    "command_id": str(prior_sequence.command_id),
                    "committed_revision": prior_sequence.committed_revision,
                },
            )

    if (
        metadata.expected_revision is not None
        and metadata.expected_revision != match_data.live_revision
    ):
        raise TrackerCommandError(
            "Tracker state changed; refresh before retrying the command.",
            code="revision_conflict",
            details={
                "expected_revision": metadata.expected_revision,
                "current_revision": match_data.live_revision,
            },
        )

    match_data.command_sequence += 1
    with suppress_live_update_signals():
        match_data.save(update_fields=["command_sequence"])
    receipt = TrackerCommand.objects.create(
        command_id=metadata.command_id or uuidv7(),
        match_data=match_data,
        team=team,
        actor=actor if getattr(actor, "is_authenticated", False) else None,
        sequence=match_data.command_sequence,
        command=command,
        payload_hash=metadata.payload_hash,
        expected_revision=metadata.expected_revision,
        source=metadata.source,
        device_id=metadata.device_id,
        session_id=metadata.session_id,
        client_sequence=metadata.client_sequence,
    )
    return receipt, False


def apply_tracker_command(
    match: Match,
    *,
    team: Team,
    payload: dict[str, Any],
    actor: object | None = None,
) -> dict[str, Any]:
    """Apply a tracker command and return the updated state.

    Raises:
        TrackerCommandError: If the command is invalid or cannot be applied.

    """
    command = payload.get("command")
    if not isinstance(command, str):
        raise TrackerCommandError("Missing command.", code="bad_request")
    parsed_command = _parse_command(payload)
    metadata = _command_metadata(payload)

    _other_team(match, team)

    match_data = MatchData.objects.filter(match_link=match).first()
    if not match_data:
        raise TrackerCommandError(MATCH_TRACKER_DATA_NOT_FOUND, code="not_found")

    with locked_match_mutation(match_data.id_uuid) as match_data:
        receipt, replay = _register_tracker_command(
            match_data=match_data,
            team=team,
            command=command,
            metadata=metadata,
            actor=actor,
        )
        if replay:
            if receipt is not None and receipt.response_payload:
                return json.loads(json.dumps(receipt.response_payload))
            return get_tracker_state(match, team=team)
        event_time = (
            timezone.now()
            if command in _SERVER_TIMED_COMMANDS
            else _command_time_from_payload(payload)
        )
        affected_resources = _COMMAND_RESOURCES.get(command, frozenset())
        before_events = (
            {event["event_id"]: event for event in build_match_events(match_data)}
            if LiveResource.EVENTS in affected_resources
            else {}
        )
        before_shots = (
            {shot["event_id"]: shot for shot in build_match_shots(match_data)}
            if LiveResource.SHOTS in affected_resources
            else {}
        )

        with (
            match_event_context(
                actor=actor,
                source_team=team,
                command_id=(receipt.command_id if receipt else metadata.command_id),
                source=metadata.source,
                client=MatchEventClient(
                    device_id=metadata.device_id,
                    session_id=metadata.session_id,
                    client_sequence=metadata.client_sequence,
                ),
            ),
            suppress_live_update_signals(),
        ):
            parsed_command.apply(
                _TrackerCommandContext(
                    match=match,
                    match_data=match_data,
                    team=team,
                    event_time=event_time,
                ),
            )
        if command in _MUTATING_COMMANDS:
            changed_ids: dict[LiveResource, set[str]] = {}
            if LiveResource.EVENTS in affected_resources:
                after_events = {
                    event["event_id"]: event for event in build_match_events(match_data)
                }
                changed_ids[LiveResource.EVENTS] = {
                    event_id
                    for event_id in before_events.keys() | after_events.keys()
                    if before_events.get(event_id) != after_events.get(event_id)
                }
            if LiveResource.SHOTS in affected_resources:
                after_shots = {
                    shot["event_id"]: shot for shot in build_match_shots(match_data)
                }
                changed_ids[LiveResource.SHOTS] = {
                    event_id
                    for event_id in before_shots.keys() | after_shots.keys()
                    if before_shots.get(event_id) != after_shots.get(event_id)
                }
            record_match_change(
                match_data,
                resources=affected_resources,
                changed_ids=changed_ids,
            )
        result = get_tracker_state(match, team=team)
        if receipt is not None:
            receipt.committed_revision = result.get("live_revision")
            receipt.response_payload = result
            receipt.save(update_fields=["committed_revision", "response_payload"])

    return result


def poll_tracker_state(
    match: Match,
    *,
    team: Team,
    since_revision: int,
    timeout_seconds: int = 25,
    compact: bool = False,
) -> dict[str, Any]:
    """Return changed tracker state without occupying a request worker.

    The timeout argument remains in the signature for wire compatibility with
    older clients. Realtime clients reconnect through SSE and polling fallbacks
    issue ordinary interval requests, so waiting here only ties up server
    workers without improving delivery.

    Raises:
        TrackerCommandError: If the tracker data for the match does not exist.

    """
    del timeout_seconds

    _other_team(match, team)

    match_data = MatchData.objects.filter(match_link=match).first()
    if not match_data:
        raise TrackerCommandError(MATCH_TRACKER_DATA_NOT_FOUND, code="not_found")

    if match_data.live_revision > since_revision:
        state = get_tracker_state(match, team=team)
        summary = summarize_match_changes(
            MatchData.objects.get(pk=match_data.pk),
            since_revision=since_revision,
        )
        resources = sorted(resource.value for resource in summary.resources)
        if compact:
            return compact_tracker_state(state, resources=resources)
        state["resources"] = resources
        return state

    return {
        "changed": False,
        "server_time": timezone.now().isoformat(),
        "last_changed_at": _last_changed_at(match_data).isoformat(),
        "live_revision": match_data.live_revision,
    }


def _prepare_new_part(match_data: MatchData) -> None:
    """Validate and advance state before creating a new active period.

    Raises:
        TrackerCommandError: If the persisted match state cannot start a part.

    """
    if match_data.status == "finished":
        raise TrackerCommandError(
            "Finished matches cannot be restarted.",
            code="match_finished",
        )

    if match_data.status == "upcoming":
        if (
            match_data.current_part != 1
            or MatchPart.objects.filter(
                match_data=match_data,
            ).exists()
        ):
            raise TrackerCommandError(
                "Match has an invalid initial state.",
                code="invalid_match_state",
            )
        match_data.status = "active"
        match_data.save(update_fields=["status"])
        return

    if match_data.status != "active":
        raise TrackerCommandError(
            "Match cannot be started from its current state.",
            code="invalid_match_state",
        )

    previous_part_number = match_data.current_part - 1
    previous_part_finished = MatchPart.objects.filter(
        match_data=match_data,
        part_number=previous_part_number,
        active=False,
        end_time__isnull=False,
    ).exists()
    if previous_part_number < 1 or not previous_part_finished:
        raise TrackerCommandError(
            "The previous match part has not been completed.",
            code="invalid_match_state",
        )
    if MatchPart.objects.filter(
        match_data=match_data,
        part_number=match_data.current_part,
    ).exists():
        raise TrackerCommandError(
            "This match part has already been started.",
            code="invalid_match_state",
        )


def _cmd_start_pause(*, match_data: MatchData, event_time: datetime) -> None:
    current_part = _current_part(match_data)
    if not current_part:
        if not MatchPart.objects.filter(match_data=match_data).exists():
            capture_starting_lineup(match_data)
        _prepare_new_part(match_data)

        MatchPart.objects.create(
            match_data=match_data,
            active=True,
            start_time=event_time,
            part_number=match_data.current_part,
        )

        return

    if (
        match_data.status != "active"
        or current_part.part_number != match_data.current_part
    ):
        raise TrackerCommandError(
            "Active match part does not match the match state.",
            code="invalid_match_state",
        )

    # Toggle pause.
    active_pause = Pause.objects.filter(
        match_data=match_data,
        active=True,
        match_part=current_part,
    ).first()

    if not active_pause:
        Pause.objects.create(
            match_data=match_data,
            active=True,
            start_time=event_time,
            match_part=current_part,
        )
        return

    active_pause.active = False

    end_time = event_time
    if (
        isinstance(active_pause.start_time, datetime)
        and end_time < active_pause.start_time
    ):
        end_time = active_pause.start_time
    active_pause.end_time = end_time
    active_pause.save(update_fields=["active", "end_time"])


def _cmd_part_end(
    match: Match,
    *,
    match_data: MatchData,
    event_time: datetime,
) -> None:
    current_part = _current_part(match_data)
    if match_data.status != "active":
        raise TrackerCommandError(
            "Only an active match can end a part.",
            code="match_not_active",
        )
    if current_part is None:
        raise TrackerCommandError(
            NO_ACTIVE_MATCH_PART_MESSAGE,
            code="no_active_part",
        )
    if current_part.part_number != match_data.current_part:
        raise TrackerCommandError(
            "Active match part does not match the match state.",
            code="invalid_match_state",
        )

    for active_pause in Pause.objects.filter(
        match_data=match_data,
        match_part=current_part,
        active=True,
    ):
        active_pause.active = False
        active_pause.end_time = max(
            event_time,
            active_pause.start_time or event_time,
        )
        active_pause.save(update_fields=["active", "end_time"])

    end_time = max(event_time, current_part.start_time)
    current_part.active = False
    current_part.end_time = end_time
    current_part.save(update_fields=["active", "end_time"])

    if match_data.current_part < match_data.parts:
        match_data.current_part += 1
        match_data.save(update_fields=["current_part"])
        return

    # End match.
    match_data_uuid = match_data.id_uuid
    match_data_id = (
        match_data_uuid
        if isinstance(match_data_uuid, UUID)
        else UUID(str(match_data_uuid))
    )
    scores = compute_scores_for_matchdata_ids([match_data_id]).get(
        match_data_id, (0, 0)
    )

    match_data.status = "finished"
    match_data.home_score, match_data.away_score = scores
    match_data.save(update_fields=["status", "home_score", "away_score"])

    def enqueue_match_finished() -> None:
        try:
            handle_match_finished = import_string(
                "apps.player.tasks.handle_match_finished"
            )
            handle_match_finished.delay(
                match_id=str(match.id_uuid),
                match_data_id=str(match_data.id_uuid),
            )
        except Exception:
            logger.warning(
                "Failed to enqueue match finished push task (http)",
                exc_info=True,
            )

    transaction.on_commit(enqueue_match_finished)


def _cmd_timeout(
    match: Match,
    *,
    match_data: MatchData,
    team: Team,
    event_time: datetime,
) -> None:
    current_part, _ = _require_not_paused(match_data, team, match)

    if (
        Timeout.objects.filter(match_data=match_data, team=team).count()
        >= _MAX_TIMEOUTS_PER_TEAM
    ):
        raise TrackerCommandError(
            "Maximum number of timeouts reached.",
            code="max_timeouts",
        )

    # A timeout is essentially: pause + timeout record.
    pause = Pause.objects.create(
        match_data=match_data,
        active=True,
        start_time=event_time,
        match_part=current_part,
    )
    Timeout.objects.create(
        match_data=match_data,
        match_part=current_part,
        team=team,
        pause=pause,
    )


def _cmd_new_attack(
    match: Match,
    *,
    match_data: MatchData,
    team: Team,
    event_time: datetime,
) -> None:
    current_part, _ = _require_not_paused(match_data, team, match)
    Attack.objects.create(
        match_data=match_data,
        match_part=current_part,
        team=team,
        time=event_time,
    )


@dataclass(frozen=True, slots=True)
class _ShotRegParams:
    player_id: str
    for_team: bool
    shot_type_id: str | None = None


def _match_player(
    *,
    match_data: MatchData,
    team: Team,
    player_id: str,
) -> Player:
    """Return a player registered in this match for the expected team.

    Raises:
        TrackerCommandError: If the identifier is invalid or outside the roster.

    """
    try:
        player = (
            Player.objects
            .select_related("user")
            .filter(id_uuid=player_id)
            .filter(
                models.Q(
                    player_groups__match_data=match_data,
                    player_groups__team=team,
                )
                | models.Q(
                    match_players__match_data=match_data,
                    match_players__team=team,
                )
            )
            .distinct()
            .first()
        )
    except (ValidationError, ValueError) as exc:
        raise TrackerCommandError("Invalid player.", code="bad_request") from exc
    if player is None:
        raise TrackerCommandError(
            "Player is not registered for this team.", code="bad_request"
        )
    return player


def _cmd_shot_reg(
    match: Match,
    *,
    match_data: MatchData,
    team: Team,
    params: _ShotRegParams,
    event_time: datetime,
) -> None:
    current_part, opponent = _require_not_paused(match_data, team, match)

    shot_team = team if params.for_team else opponent
    player = _match_player(
        match_data=match_data,
        team=team,
        player_id=params.player_id,
    )

    shot_type: GoalType | None = None
    if params.shot_type_id:
        try:
            shot_type = GoalType.objects.get(id_uuid=params.shot_type_id)
        except (GoalType.DoesNotExist, ValidationError, ValueError) as exc:
            raise TrackerCommandError(
                "Invalid shot type.",
                code="bad_request",
            ) from exc

    plan = plan_shot_reconciliation(
        ShotObservation(
            match_data=match_data,
            match_part=current_part,
            reporting_team_id=team.pk,
            shooting_team_id=shot_team.pk,
            outcome=ShotEventDetail.OUTCOME_MISS,
            shot_type=shot_type,
            effective_at=event_time,
        )
    )
    observation_payload = {
        "kind": "shot",
        "shooting_team_id": str(shot_team.pk),
        "reporting_team_id": str(team.pk),
        "reported_player_id": str(player.pk),
        "reported_player_role": "shooter" if params.for_team else "defender",
        "shot_type_id": str(shot_type.pk) if shot_type else None,
        "outcome": ShotEventDetail.OUTCOME_MISS,
    }
    if plan.matched_event is not None:
        record_matched_observation(
            event=plan.matched_event,
            effective_at=event_time,
            payload=observation_payload,
        )
        return

    shot = Shot.objects.create(
        player=player,
        match_data=match_data,
        match_part=current_part,
        time=event_time,
        for_team=params.for_team,
        team=shot_team,
        shot_type=shot_type,
        scored=False,
    )
    event = MatchEvent.objects.get(source_type="shot", source_id=shot.pk)
    create_reconciliation_candidates(
        event=event,
        possible_duplicates=plan.review_events,
    )


@dataclass(frozen=True, slots=True)
class _GoalRegParams:
    player_id: str
    goal_type_id: str
    for_team: bool


def _cmd_goal_reg(
    match: Match,
    *,
    match_data: MatchData,
    team: Team,
    params: _GoalRegParams,
    event_time: datetime,
) -> None:
    current_part, opponent = _require_not_paused(match_data, team, match)

    shot_team = team if params.for_team else opponent
    player = _match_player(
        match_data=match_data,
        team=team,
        player_id=params.player_id,
    )
    try:
        goal_type = GoalType.objects.get(id_uuid=params.goal_type_id)
    except (GoalType.DoesNotExist, ValidationError, ValueError) as exc:
        raise TrackerCommandError("Invalid goal type.", code="bad_request") from exc

    plan = plan_shot_reconciliation(
        ShotObservation(
            match_data=match_data,
            match_part=current_part,
            reporting_team_id=team.pk,
            shooting_team_id=shot_team.pk,
            outcome=ShotEventDetail.OUTCOME_GOAL,
            shot_type=goal_type,
            effective_at=event_time,
        )
    )
    observation_payload = {
        "kind": "shot",
        "shooting_team_id": str(shot_team.pk),
        "reporting_team_id": str(team.pk),
        "reported_player_id": str(player.pk),
        "reported_player_role": "shooter" if params.for_team else "defender",
        "shot_type_id": str(goal_type.pk),
        "outcome": ShotEventDetail.OUTCOME_GOAL,
    }
    if plan.matched_event is not None:
        record_matched_observation(
            event=plan.matched_event,
            effective_at=event_time,
            payload=observation_payload,
        )
        return

    shot = Shot.objects.create(
        player=player,
        match_data=match_data,
        match_part=current_part,
        time=event_time,
        for_team=params.for_team,
        team=shot_team,
        shot_type=goal_type,
        scored=True,
    )
    event = MatchEvent.objects.get(source_type="shot", source_id=shot.pk)
    create_reconciliation_candidates(
        event=event,
        possible_duplicates=plan.review_events,
    )

    rebuild_group_roles(match_data)


def _cmd_substitute_reg(
    match: Match,
    *,
    match_data: MatchData,
    team: Team,
    params: _SubstituteRegParams,
    event_time: datetime,
) -> None:
    del match
    current_part = _current_part(match_data)

    # Allow substitutions during pauses; only block when the match is not active.
    if match_data.status != "active":
        raise TrackerCommandError("Match is not active.", code="match_not_active")

    # Allow substitutions when there is no active part (between parts).
    # In that situation, persist the substitution without a match part so the
    # event timeline can show it as an intermission (e.g. half-time) event.
    part_for_event = current_part
    if not current_part:
        if match_data.current_part <= 1:
            raise TrackerCommandError(
                NO_ACTIVE_MATCH_PART_MESSAGE,
                code="no_active_part",
            )
        part_for_event = None

    substitutions_max = 8
    substitutions_for = PlayerChange.objects.filter(
        match_data=match_data,
        player_group__team=team,
    ).count()
    if substitutions_for >= substitutions_max:
        raise TrackerCommandError(
            "Max wissels bereikt.",
            code="max_substitutions",
        )

    player_in = Player.objects.select_related("user").get(id_uuid=params.new_player_id)
    player_out = Player.objects.select_related("user").get(id_uuid=params.old_player_id)

    active_group = PlayerGroup.objects.exclude(
        starting_type__name=RESERVE_GROUP_NAME,
    ).get(
        team=team,
        match_data=match_data,
        players__in=[player_out],
    )

    PlayerChange.objects.create(
        player_in=player_in,
        player_out=player_out,
        player_group=active_group,
        match_data=match_data,
        match_part=part_for_event,
        time=event_time,
    )
    rebuild_current_lineup(match_data)


@dataclass(frozen=True, slots=True)
class _SubstituteRegParams:
    new_player_id: str
    old_player_id: str


def _cmd_substitute_against_reg(
    match: Match,
    *,
    match_data: MatchData,
    team: Team,
    event_time: datetime,
) -> None:
    """Register an opponent substitution without specifying players.

    Raises:
        TrackerCommandError: If the match is not active or the opponent has reached
            the maximum number of substitutions.

    """
    current_part = _current_part(match_data)

    # Allow substitutions during pauses; only block when the match is not active.
    if match_data.status != "active":
        raise TrackerCommandError("Match is not active.", code="match_not_active")

    opponent = _other_team(match, team)

    part_for_event = current_part
    if not current_part:
        if match_data.current_part <= 1:
            raise TrackerCommandError(
                NO_ACTIVE_MATCH_PART_MESSAGE,
                code="no_active_part",
            )
        part_for_event = None

    substitutions_max = 8
    substitutions_against = PlayerChange.objects.filter(
        match_data=match_data,
        player_group__team=opponent,
    ).count()
    if substitutions_against >= substitutions_max:
        raise TrackerCommandError(
            "Max wissels bereikt.",
            code="max_substitutions",
        )

    opponent_reserve_group = get_reserve_group(match_data=match_data, team=opponent)

    PlayerChange.objects.create(
        player_in=None,
        player_out=None,
        player_group=opponent_reserve_group,
        match_data=match_data,
        match_part=part_for_event,
        time=event_time,
    )


def _remove_last_shot(
    event: Shot,
    *,
    match_data: MatchData,
) -> None:
    scored = event.scored
    event.__dict__.setdefault("match_data_id", match_data.pk)
    event.delete()

    if not scored:
        return

    rebuild_group_roles(match_data)


def _remove_last_player_change(event: PlayerChange, *, match_data: MatchData) -> None:
    # Opponent substitution markers do not have concrete players.
    if not event.player_in or not event.player_out:
        event.delete()
        return

    event.delete()
    rebuild_current_lineup(match_data)


def _remove_last_pause(event: Pause) -> None:
    timeout = Timeout.objects.filter(pause=event).first()
    if timeout:
        timeout.delete()
    if event.active:
        event.delete()
        return

    event.active = True
    cast(Any, event).end_time = None
    event.save(update_fields=["active", "end_time"])


def _remove_last_attack(event: Attack) -> None:
    event.delete()


def _cmd_remove_last_event(match: Match, *, match_data: MatchData, team: Team) -> None:
    _other_team(match, team)
    event = _get_last_event_model(match_data)
    if not event:
        return

    if isinstance(event, Shot):
        _remove_last_shot(event, match_data=match_data)
        return

    if isinstance(event, PlayerChange):
        _remove_last_player_change(event, match_data=match_data)
        return

    if isinstance(event, Pause):
        _remove_last_pause(event)
        return

    if isinstance(event, Attack):
        _remove_last_attack(event)


@dataclass(frozen=True, slots=True)
class _StartPauseCommand:
    def apply(self, context: _TrackerCommandContext) -> None:
        _cmd_start_pause(
            match_data=context.match_data,
            event_time=context.event_time,
        )


@dataclass(frozen=True, slots=True)
class _PartEndCommand:
    def apply(self, context: _TrackerCommandContext) -> None:
        _cmd_part_end(
            context.match,
            match_data=context.match_data,
            event_time=context.event_time,
        )


@dataclass(frozen=True, slots=True)
class _TimeoutCommand:
    for_team: bool

    def apply(self, context: _TrackerCommandContext) -> None:
        timeout_team = (
            context.team if self.for_team else _other_team(context.match, context.team)
        )
        _cmd_timeout(
            context.match,
            match_data=context.match_data,
            team=timeout_team,
            event_time=context.event_time,
        )


@dataclass(frozen=True, slots=True)
class _NewAttackCommand:
    def apply(self, context: _TrackerCommandContext) -> None:
        _cmd_new_attack(
            context.match,
            match_data=context.match_data,
            team=context.team,
            event_time=context.event_time,
        )


@dataclass(frozen=True, slots=True)
class _ShotRegCommand:
    params: _ShotRegParams

    def apply(self, context: _TrackerCommandContext) -> None:
        _cmd_shot_reg(
            context.match,
            match_data=context.match_data,
            team=context.team,
            params=self.params,
            event_time=context.event_time,
        )


@dataclass(frozen=True, slots=True)
class _GoalRegCommand:
    params: _GoalRegParams

    def apply(self, context: _TrackerCommandContext) -> None:
        _cmd_goal_reg(
            context.match,
            match_data=context.match_data,
            team=context.team,
            params=self.params,
            event_time=context.event_time,
        )


@dataclass(frozen=True, slots=True)
class _GetNonActivePlayersCommand:
    def apply(self, context: _TrackerCommandContext) -> None:
        del context
        # No-op for HTTP; reserve players are included in the state snapshot.


@dataclass(frozen=True, slots=True)
class _SubstituteRegCommand:
    params: _SubstituteRegParams

    def apply(self, context: _TrackerCommandContext) -> None:
        _cmd_substitute_reg(
            context.match,
            match_data=context.match_data,
            team=context.team,
            params=self.params,
            event_time=context.event_time,
        )


@dataclass(frozen=True, slots=True)
class _SubstituteAgainstRegCommand:
    def apply(self, context: _TrackerCommandContext) -> None:
        _cmd_substitute_against_reg(
            context.match,
            match_data=context.match_data,
            team=context.team,
            event_time=context.event_time,
        )


@dataclass(frozen=True, slots=True)
class _RemoveLastEventCommand:
    def apply(self, context: _TrackerCommandContext) -> None:
        _cmd_remove_last_event(
            context.match,
            match_data=context.match_data,
            team=context.team,
        )


def _parse_timeout_command(payload: dict[str, Any]) -> _TrackerCommand:
    for_team = payload.get("for_team")
    if not isinstance(for_team, bool):
        raise TrackerCommandError("Invalid timeout payload.", code="bad_request")
    return _TimeoutCommand(for_team=for_team)


def _parse_shot_reg_command(payload: dict[str, Any]) -> _TrackerCommand:
    player_id = payload.get("player_id")
    for_team = payload.get("for_team")
    shot_type = payload.get("shot_type")
    if shot_type is None:
        shot_type = payload.get("goal_type")

    if not isinstance(player_id, str) or not isinstance(for_team, bool):
        raise TrackerCommandError("Invalid shot_reg payload.", code="bad_request")
    if shot_type is not None and not isinstance(shot_type, str):
        raise TrackerCommandError("Invalid shot type.", code="bad_request")

    return _ShotRegCommand(
        params=_ShotRegParams(
            player_id=player_id,
            for_team=for_team,
            shot_type_id=shot_type,
        ),
    )


def _parse_goal_reg_command(payload: dict[str, Any]) -> _TrackerCommand:
    player_id = payload.get("player_id")
    goal_type = payload.get("goal_type")
    for_team = payload.get("for_team")
    if (
        not isinstance(player_id, str)
        or not isinstance(goal_type, str)
        or not isinstance(for_team, bool)
    ):
        raise TrackerCommandError("Invalid goal_reg payload.", code="bad_request")

    return _GoalRegCommand(
        params=_GoalRegParams(
            player_id=player_id,
            goal_type_id=goal_type,
            for_team=for_team,
        ),
    )


def _parse_substitute_reg_command(payload: dict[str, Any]) -> _TrackerCommand:
    new_player_id = payload.get("new_player_id")
    old_player_id = payload.get("old_player_id")
    if not isinstance(new_player_id, str) or not isinstance(old_player_id, str):
        raise TrackerCommandError("Invalid substitute_reg payload.", code="bad_request")

    return _SubstituteRegCommand(
        params=_SubstituteRegParams(
            new_player_id=new_player_id,
            old_player_id=old_player_id,
        ),
    )


_CommandParser = Callable[[dict[str, Any]], _TrackerCommand]


_COMMAND_PARSERS: dict[str, _CommandParser] = {
    "start/pause": lambda _payload: _StartPauseCommand(),
    "part_end": lambda _payload: _PartEndCommand(),
    "timeout": _parse_timeout_command,
    "new_attack": lambda _payload: _NewAttackCommand(),
    "shot_reg": _parse_shot_reg_command,
    "goal_reg": _parse_goal_reg_command,
    "get_non_active_players": lambda _payload: _GetNonActivePlayersCommand(),
    "substitute_reg": _parse_substitute_reg_command,
    "substitute_against_reg": lambda _payload: _SubstituteAgainstRegCommand(),
    "remove_last_event": lambda _payload: _RemoveLastEventCommand(),
}


def _parse_command(payload: dict[str, Any]) -> _TrackerCommand:
    command = payload.get("command")
    if not isinstance(command, str):
        raise TrackerCommandError("Missing command.", code="bad_request")

    parser = _COMMAND_PARSERS.get(command)
    if not parser:
        raise TrackerCommandError(f"Unknown command: {command}", code="bad_request")

    return parser(payload)
