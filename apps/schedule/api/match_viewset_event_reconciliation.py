"""Match event reconciliation HTTP actions."""

from __future__ import annotations

from rest_framework import status
from rest_framework.decorators import action
from rest_framework.request import Request
from rest_framework.response import Response

from apps.game_tracker.composition import change_publisher
from apps.game_tracker.services.event_reconciliation import (
    EventReconciliationError,
    ReconciliationResolution,
    pending_reconciliations,
    resolve_reconciliation,
)
from apps.schedule.models import Match

from .constants import MATCH_TRACKER_DATA_NOT_FOUND
from .event_editor_commands import request_payload
from .match_viewset_contracts import MatchViewSetContext
from .permissions import IsCoachOrAdmin


RECONCILIATION_REASON_MAX_LENGTH = 255


class MatchEventReconciliationActionsMixin:
    """Provide the match event reconciliation actions."""

    @action(
        detail=True,
        methods=("GET",),
        url_path="events/reconciliations",
        permission_classes=[IsCoachOrAdmin],
    )
    def event_reconciliations(
        self: MatchViewSetContext,
        request: Request,
        *args: object,
        **kwargs: object,
    ) -> Response:
        """Return ambiguous cross-team reports requiring a decision."""
        del request, args, kwargs
        match: Match = self.get_object()
        match_data = self._match_data(match)
        if match_data is None:
            return Response(
                {"detail": MATCH_TRACKER_DATA_NOT_FOUND},
                status=status.HTTP_404_NOT_FOUND,
            )
        return Response({"reconciliations": pending_reconciliations(match_data)})

    @action(
        detail=True,
        methods=("POST",),
        url_path=r"events/reconciliations/(?P<reconciliation_id>[^/.]+)/resolve",
        permission_classes=[IsCoachOrAdmin],
    )
    def resolve_event_reconciliation(
        self: MatchViewSetContext,
        request: Request,
        reconciliation_id: str,
        *args: object,
        **kwargs: object,
    ) -> Response:
        """Merge a duplicate pair or confirm that both events are real."""
        del args, kwargs
        match: Match = self.get_object()
        match_data = self._match_data(match)
        if match_data is None:
            return Response(
                {"detail": MATCH_TRACKER_DATA_NOT_FOUND},
                status=status.HTTP_404_NOT_FOUND,
            )
        payload = request_payload(request)
        decision = payload.get("decision")
        canonical_event_id = payload.get("canonical_event_id")
        reason = payload.get("reason", "")
        if not isinstance(decision, str) or not isinstance(reason, str):
            return Response(
                {"detail": "Invalid reconciliation decision."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if canonical_event_id is not None and not isinstance(canonical_event_id, str):
            return Response(
                {"detail": "Invalid canonical_event_id."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if len(reason) > RECONCILIATION_REASON_MAX_LENGTH:
            return Response(
                {"detail": "Reason must contain at most 255 characters."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        try:
            resolved = resolve_reconciliation(
                ReconciliationResolution(
                    match_data=match_data,
                    reconciliation_id=reconciliation_id,
                    decision=decision,
                    canonical_event_id=canonical_event_id,
                    actor=request.user,
                    reason=reason,
                ),
                publisher=change_publisher,
            )
        except EventReconciliationError as exc:
            return Response(
                {"detail": str(exc)},
                status=status.HTTP_409_CONFLICT,
            )
        return Response({
            "id_uuid": str(resolved.pk),
            "decision": resolved.decision,
            "canonical_event_id": (
                str(resolved.canonical_event_id)
                if resolved.canonical_event_id
                else None
            ),
            "resolution_event_id": str(resolved.resolution_event_id),
        })
