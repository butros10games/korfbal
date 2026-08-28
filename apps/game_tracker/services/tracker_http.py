"""HTTP-friendly match tracker helpers and mutation commands."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from typing import Any
from uuid import UUID

from bg_uuidv7 import uuidv7

from apps.game_tracker.application.ports import TrackerRuntime
from apps.game_tracker.domain.command_time import command_time_from_payload
from apps.game_tracker.models import MatchData, TrackerCommand
from apps.game_tracker.realtime.contracts import LiveResource
from apps.game_tracker.services.live_update_signal_control import (
    suppress_live_update_signals,
)
from apps.game_tracker.services.live_updates import (
    record_match_change,
)
from apps.game_tracker.services.match_event_context import (
    MatchEventClient,
    match_event_context,
)
from apps.game_tracker.services.match_mutations import locked_match_mutation
from apps.game_tracker.services.match_timeline_payload import (
    build_match_events,
    build_match_shots,
    build_match_timeline_payloads,
)
from apps.game_tracker.services.tracker_commands import (
    CommandDefinition,
    TrackerCommandContext,
    TrackerCommandError,
    command_definition,
)
from apps.game_tracker.services.tracker_commands.base import other_team
from apps.game_tracker.services.tracker_state import (
    MATCH_TRACKER_DATA_NOT_FOUND,
    compact_tracker_state,
    get_tracker_state,
    poll_tracker_state,
)
from apps.schedule.models import Match
from apps.team.models.team import Team


__all__ = (
    "MATCH_TRACKER_DATA_NOT_FOUND",
    "TrackerCommandError",
    "compact_tracker_state",
    "execute_tracker_command",
    "get_tracker_state",
    "poll_tracker_state",
)


_CLIENT_ID_MAX_LENGTH = 128
_CLIENT_SOURCE_MAX_LENGTH = 32


def _timeline_resource_payloads(
    match_data: MatchData,
    resources: frozenset[LiveResource],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Build only requested timeline resources, sharing context when possible."""
    include_events = LiveResource.EVENTS in resources
    include_shots = LiveResource.SHOTS in resources
    if include_events and include_shots:
        return build_match_timeline_payloads(match_data)
    return (
        build_match_events(match_data) if include_events else [],
        build_match_shots(match_data) if include_shots else [],
    )


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
    definition: CommandDefinition,
    metadata: _CommandMetadata,
    actor: object | None,
) -> tuple[TrackerCommand | None, bool]:
    """Register a transition and identify an exact idempotent replay.

    Raises:
        TrackerCommandError: If the idempotency key conflicts or state is stale.

    """
    if not definition.mutating:
        return None, False

    if metadata.command_id is not None:
        previous = TrackerCommand.objects.filter(command_id=metadata.command_id).first()
        if previous is not None:
            if (
                previous.match_data_id != match_data.pk
                or previous.team_id != team.id_uuid
                or previous.command != definition.name
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
        command=definition.name,
        payload_hash=metadata.payload_hash,
        expected_revision=metadata.expected_revision,
        source=metadata.source,
        device_id=metadata.device_id,
        session_id=metadata.session_id,
        client_sequence=metadata.client_sequence,
    )
    return receipt, False


def execute_tracker_command(
    match: Match,
    *,
    team: Team,
    payload: dict[str, Any],
    actor: object | None = None,
    runtime: TrackerRuntime,
) -> dict[str, Any]:
    """Apply a tracker command and return the updated state.

    Raises:
        TrackerCommandError: If the command is invalid or cannot be applied.

    """
    definition = command_definition(payload)
    parsed_command = definition.parse(payload)
    metadata = _command_metadata(payload)

    other_team(match, team)

    match_data = MatchData.objects.filter(match_link=match).first()
    if not match_data:
        raise TrackerCommandError(MATCH_TRACKER_DATA_NOT_FOUND, code="not_found")

    with locked_match_mutation(match_data.id_uuid) as match_data:
        receipt, replay = _register_tracker_command(
            match_data=match_data,
            team=team,
            definition=definition,
            metadata=metadata,
            actor=actor,
        )
        if replay:
            if receipt is not None and receipt.response_payload:
                return json.loads(json.dumps(receipt.response_payload))
            return get_tracker_state(match, team=team)
        event_time = (
            runtime.now()
            if definition.server_timed
            else command_time_from_payload(payload, server_now=runtime.now())
        )
        affected_resources = definition.resources
        before_event_rows, before_shot_rows = _timeline_resource_payloads(
            match_data,
            affected_resources,
        )
        before_events = {event["event_id"]: event for event in before_event_rows}
        before_shots = {shot["event_id"]: shot for shot in before_shot_rows}

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
                TrackerCommandContext(
                    match=match,
                    match_data=match_data,
                    team=team,
                    event_time=event_time,
                    jobs=runtime.jobs,
                ),
            )
        if definition.mutating:
            changed_ids: dict[LiveResource, set[str]] = {}
            after_event_rows, after_shot_rows = _timeline_resource_payloads(
                match_data,
                affected_resources,
            )
            if LiveResource.EVENTS in affected_resources:
                after_events = {event["event_id"]: event for event in after_event_rows}
                changed_ids[LiveResource.EVENTS] = {
                    event_id
                    for event_id in before_events.keys() | after_events.keys()
                    if before_events.get(event_id) != after_events.get(event_id)
                }
            if LiveResource.SHOTS in affected_resources:
                after_shots = {shot["event_id"]: shot for shot in after_shot_rows}
                changed_ids[LiveResource.SHOTS] = {
                    event_id
                    for event_id in before_shots.keys() | after_shots.keys()
                    if before_shots.get(event_id) != after_shots.get(event_id)
                }
            record_match_change(
                match_data,
                resources=affected_resources,
                changed_ids=changed_ids,
                publisher=runtime.publisher,
            )
        result = get_tracker_state(match, team=team)
        result["resources"] = sorted(resource.value for resource in affected_resources)
        if receipt is not None:
            committed_revision = result.get("live_revision")
            receipt.committed_revision = (
                int(committed_revision) if committed_revision is not None else None
            )
            receipt.response_payload = result
            receipt.save(update_fields=["committed_revision", "response_payload"])

    return result
