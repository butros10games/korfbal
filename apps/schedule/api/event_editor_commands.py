"""HTTP validation and error translation for event editor commands."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from rest_framework import status
from rest_framework.exceptions import APIException, ParseError, ValidationError
from rest_framework.request import Request

from apps.game_tracker.composition import apply_event_editor_command
from apps.game_tracker.models import (
    MatchData,
)
from apps.game_tracker.services.event_editor import (
    EventEditorCommand,
    EventEditorResult,
    EventEditorValidationError,
)
from apps.game_tracker.services.match_mutations import MatchRevisionConflictError


class MatchRevisionConflictApiError(APIException):
    """Translate an aggregate revision conflict to a structured HTTP 409."""

    status_code = status.HTTP_409_CONFLICT
    default_code = "revision_conflict"

    def __init__(self, conflict: MatchRevisionConflictError) -> None:
        """Build the API error from the provider-neutral conflict."""
        Exception.__init__(self, str(conflict))
        self.detail = {
            "code": self.default_code,
            "detail": str(conflict),
            "expected_revision": conflict.expected_revision,
            "live_revision": conflict.live_revision,
        }


def request_payload(request: Request) -> Mapping[str, Any]:
    """Return an object-shaped request body.

    Raises:
        ParseError: If the request body is not a JSON object.

    """
    payload = request.data
    if not isinstance(payload, Mapping):
        raise ParseError("Request body must be a JSON object.")
    return payload


def apply_command(
    *,
    match_data: MatchData,
    request: Request,
    command: EventEditorCommand,
) -> EventEditorResult:
    """Execute a revision-checked command and translate domain errors to HTTP.

    Raises:
        MatchRevisionConflictApiError: The submitted revision is stale.
        ValidationError: Command fields or the expected revision are invalid.

    """
    try:
        return apply_event_editor_command(
            match_data_id=match_data.pk,
            expected_revision=_expected_revision(request),
            actor=request.user,
            command=command,
        )
    except MatchRevisionConflictError as exc:
        raise MatchRevisionConflictApiError(exc) from exc
    except EventEditorValidationError as exc:
        raise ValidationError(exc.errors) from exc


def _expected_revision(request: Request) -> int:
    payload = request_payload(request)
    value = payload.get("expected_revision")
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValidationError({
            "expected_revision": "A non-negative integer is required."
        })
    return value


def mutation_payload(
    result: EventEditorResult,
    event: dict[str, object] | None,
) -> dict[str, object]:
    """Pair the editor result with its committed aggregate revision."""
    return {"event": event, "live_revision": result.revision}
