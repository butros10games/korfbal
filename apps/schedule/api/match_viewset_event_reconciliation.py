"""Match event reconciliation HTTP actions."""

from __future__ import annotations

from rest_framework import serializers, status
from rest_framework.decorators import action
from rest_framework.request import Request
from rest_framework.response import Response

from apps.game_tracker.composition import change_publisher
from apps.game_tracker.services.event_reconciliation import (
    EventReconciliationError,
    EventReconciliationNotFoundError,
    EventReconciliationValidationError,
    ReconciliationResolution,
    pending_reconciliations,
    resolve_reconciliation,
)
from apps.schedule.api.validation import UUID_URL_REGEX
from apps.schedule.models import Match

from .constants import MATCH_TRACKER_DATA_NOT_FOUND
from .event_editor_commands import request_payload
from .match_viewset_contracts import MatchViewSetContext
from .permissions import IsCoachOrAdmin


class ReconciliationDecisionSerializer(serializers.Serializer):
    """Validate client input separately from conflicts with current match state."""

    decision = serializers.ChoiceField(choices=("merge", "separate"))
    canonical_event_id = serializers.UUIDField(required=False, allow_null=True)
    reason = serializers.CharField(
        required=False, allow_blank=True, default="", max_length=255
    )


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
        url_path=rf"events/reconciliations/(?P<reconciliation_id>{UUID_URL_REGEX})/resolve",
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
        serializer = ReconciliationDecisionSerializer(data=request_payload(request))
        serializer.is_valid(raise_exception=True)
        payload = serializer.validated_data
        decision = payload["decision"]
        canonical_event_id = payload.get("canonical_event_id")
        reason = payload["reason"]
        try:
            resolved = resolve_reconciliation(
                ReconciliationResolution(
                    match_data=match_data,
                    reconciliation_id=reconciliation_id,
                    decision=decision,
                    canonical_event_id=str(canonical_event_id)
                    if canonical_event_id
                    else None,
                    actor=request.user,
                    reason=reason,
                ),
                publisher=change_publisher,
            )
        except EventReconciliationNotFoundError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_404_NOT_FOUND)
        except EventReconciliationValidationError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
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
