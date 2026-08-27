"""MatchViewSet mixin: event timeline + event editor endpoints."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Protocol

from django.db import transaction
from django.db.models import Q
from rest_framework import permissions, status
from rest_framework.decorators import action
from rest_framework.exceptions import ParseError
from rest_framework.request import Request
from rest_framework.response import Response

from apps.game_tracker.models import (
    GoalType,
    MatchData,
    MatchPart,
    Pause,
    PlayerChange,
    Shot,
    Timeout,
)
from apps.game_tracker.realtime.contracts import LiveResource
from apps.game_tracker.services.event_reconciliation import (
    EventReconciliationError,
    ReconciliationResolution,
    pending_reconciliations,
    resolve_reconciliation,
)
from apps.game_tracker.services.live_updates import summarize_match_changes
from apps.game_tracker.services.match_events import build_match_event_history
from apps.game_tracker.services.match_mutations import apply_editor_mutation
from apps.game_tracker.services.match_timeline_payload import (
    build_match_events,
    build_match_shots,
    serialize_goal_event,
    serialize_pause_event,
    serialize_substitute_event,
)
from apps.schedule.models import Match

from .constants import MATCH_TRACKER_DATA_NOT_FOUND
from .permissions import IsCoachOrAdmin
from .serializers import (
    PauseWriteSerializer,
    PlayerChangeWriteSerializer,
    ShotWriteSerializer,
    TimeoutWriteSerializer,
)


MATCH_TIMELINE_IDENTITY_VERSION = 3
RECONCILIATION_REASON_MAX_LENGTH = 255


class _MatchViewSetLike(Protocol):
    def get_object(self) -> Match: ...

    def _match_data(self, match: Match) -> MatchData | None: ...


def _request_payload(request: Request) -> Mapping[str, Any]:
    """Return an object-shaped request body.

    Raises:
        ParseError: If the request body is not a JSON object.

    """
    payload = request.data
    if not isinstance(payload, Mapping):
        raise ParseError("Request body must be a JSON object.")
    return payload


def _find_timeout(match_data: MatchData, timeout_id: str) -> Timeout | None:
    return (
        Timeout.objects
        .select_related("pause")
        .filter(
            Q(id_uuid=timeout_id) | Q(pause_id=timeout_id),
            match_data=match_data,
        )
        .first()
    )


def _delete_timeout(match_data: MatchData, timeout_id: str) -> bool:
    timeout = _find_timeout(match_data, timeout_id)
    if timeout is None:
        return False
    pause = timeout.pause
    timeout.delete()
    if pause:
        pause.delete()
    return True


def _update_timeout(
    *,
    match_data: MatchData,
    match: Match,
    timeout_id: str,
    data: Any,
) -> Timeout | None:
    timeout = _find_timeout(match_data, timeout_id)
    if timeout is None:
        return None
    serializer = TimeoutWriteSerializer(
        instance=timeout,
        data=data,
        partial=True,
        context={"match": match, "match_data": match_data},
    )
    serializer.is_valid(raise_exception=True)
    return serializer.save()


def _parse_since_revision(request: Request) -> int | None:
    """Parse an optional non-negative timeline revision.

    Raises:
        ValueError: The supplied revision is not a non-negative integer.

    """
    raw_revision = request.query_params.get("since_revision")
    if raw_revision is None:
        return None

    try:
        revision = int(raw_revision)
    except ValueError as error:
        raise ValueError("Invalid 'since_revision'.") from error

    if revision < 0:
        raise ValueError("Invalid 'since_revision'.")
    return revision


class MatchEventsActionsMixin:
    """Adds match event timeline + event editor actions to `MatchViewSet`."""

    @action(detail=True, methods=("GET",), url_path="events")
    def events(
        self: _MatchViewSetLike,
        request: Request,
        *args: Any,
        **kwargs: Any,
    ) -> Response:
        """Return match tracker events for a single match.

        This powers the korfbal-web Match page "Events" tab.

        Returns:
            Response: JSON payload with an ordered events list.

        """
        match: Match = self.get_object()
        match_data = self._match_data(match)
        identity_version_raw = request.query_params.get("identity_version")
        try:
            since_revision = _parse_since_revision(request)
        except ValueError as error:
            return Response(
                {"detail": str(error)},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if not match_data or match_data.status == "upcoming":
            return Response(
                {
                    "mode": "full",
                    "identity_version": MATCH_TIMELINE_IDENTITY_VERSION,
                    "live_revision": match_data.live_revision if match_data else 0,
                    "home_team_id": str(match.home_team.id_uuid),
                    "match_parts": [],
                    "events": [],
                    "status": match_data.status if match_data else "unknown",
                },
                status=status.HTTP_200_OK,
            )

        with transaction.atomic():
            match_data = MatchData.objects.select_for_update().get(pk=match_data.pk)
            match_parts_payload = [
                {
                    "id_uuid": str(part.id_uuid),
                    "part_number": part.part_number,
                    "start_time": (
                        part.start_time.isoformat() if part.start_time else None
                    ),
                    "end_time": part.end_time.isoformat() if part.end_time else None,
                    "active": bool(part.active),
                }
                for part in MatchPart.objects
                .filter(match_data=match_data)
                .order_by("part_number", "start_time")
                .all()
            ]
            events_payload = build_match_events(match_data)
            base = {
                "identity_version": MATCH_TIMELINE_IDENTITY_VERSION,
                "home_team_id": str(match.home_team.id_uuid),
                "match_parts": match_parts_payload,
                "status": match_data.status,
                "live_revision": match_data.live_revision,
            }
            if since_revision is None or identity_version_raw != str(
                MATCH_TIMELINE_IDENTITY_VERSION
            ):
                return Response(
                    {**base, "mode": "full", "events": events_payload},
                    status=status.HTTP_200_OK,
                )

            summary = summarize_match_changes(
                match_data,
                since_revision=since_revision,
            )
            can_send_delta = summary.history_complete and (
                LiveResource.EVENTS not in summary.resources
                or LiveResource.EVENTS in summary.complete_id_resources
            )
            if not can_send_delta:
                return Response(
                    {**base, "mode": "full", "events": events_payload},
                    status=status.HTTP_200_OK,
                )

            changed_ids = summary.changed_ids.get(LiveResource.EVENTS, frozenset())
            current_ids = {event["event_id"] for event in events_payload}
            return Response(
                {
                    **base,
                    "mode": "delta",
                    "base_revision": since_revision,
                    "upsert": [
                        event
                        for event in events_payload
                        if event["event_id"] in changed_ids
                    ],
                    "deleted_ids": sorted(changed_ids - current_ids),
                    "order": [event["event_id"] for event in events_payload],
                },
                status=status.HTTP_200_OK,
            )

    @action(detail=True, methods=("GET",), url_path="shots")
    def shots(
        self: _MatchViewSetLike,
        request: Request,
        *args: Any,
        **kwargs: Any,
    ) -> Response:
        """Return shot attempts (scored + missed) for a single match."""
        match: Match = self.get_object()
        match_data = self._match_data(match)
        identity_version_raw = request.query_params.get("identity_version")
        try:
            since_revision = _parse_since_revision(request)
        except ValueError as error:
            return Response(
                {"detail": str(error)},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if not match_data or match_data.status == "upcoming":
            return Response(
                {
                    "mode": "full",
                    "identity_version": MATCH_TIMELINE_IDENTITY_VERSION,
                    "live_revision": match_data.live_revision if match_data else 0,
                    "home_team_id": str(match.home_team.id_uuid),
                    "away_team_id": str(match.away_team.id_uuid),
                    "shots": [],
                    "status": match_data.status if match_data else "unknown",
                },
                status=status.HTTP_200_OK,
            )

        with transaction.atomic():
            match_data = MatchData.objects.select_for_update().get(pk=match_data.pk)
            shots_payload = build_match_shots(match_data)
            base = {
                "identity_version": MATCH_TIMELINE_IDENTITY_VERSION,
                "home_team_id": str(match.home_team.id_uuid),
                "away_team_id": str(match.away_team.id_uuid),
                "status": match_data.status,
                "live_revision": match_data.live_revision,
            }
            if since_revision is None or identity_version_raw != str(
                MATCH_TIMELINE_IDENTITY_VERSION
            ):
                return Response(
                    {**base, "mode": "full", "shots": shots_payload},
                    status=status.HTTP_200_OK,
                )

            summary = summarize_match_changes(
                match_data,
                since_revision=since_revision,
            )
            can_send_delta = summary.history_complete and (
                LiveResource.SHOTS not in summary.resources
                or LiveResource.SHOTS in summary.complete_id_resources
            )
            if not can_send_delta:
                return Response(
                    {**base, "mode": "full", "shots": shots_payload},
                    status=status.HTTP_200_OK,
                )

            changed_ids = summary.changed_ids.get(LiveResource.SHOTS, frozenset())
            current_ids = {shot["event_id"] for shot in shots_payload}
            return Response(
                {
                    **base,
                    "mode": "delta",
                    "base_revision": since_revision,
                    "upsert": [
                        shot
                        for shot in shots_payload
                        if shot["event_id"] in changed_ids
                    ],
                    "deleted_ids": sorted(changed_ids - current_ids),
                    "order": [shot["event_id"] for shot in shots_payload],
                },
                status=status.HTTP_200_OK,
            )

    @action(
        detail=True,
        methods=("GET",),
        url_path="events/can-edit",
        permission_classes=[permissions.AllowAny],
    )
    def can_edit_events(
        self: _MatchViewSetLike,
        request: Request,
        *args: Any,
        **kwargs: Any,
    ) -> Response:
        """Return whether the current user can edit match events."""
        return Response({"can_edit": IsCoachOrAdmin().has_permission(request, self)})

    @action(
        detail=True,
        methods=("GET",),
        url_path="events/history",
        permission_classes=[IsCoachOrAdmin],
    )
    def event_history(
        self: _MatchViewSetLike,
        request: Request,
        *args: Any,
        **kwargs: Any,
    ) -> Response:
        """Return the complete append-only audit stream for authorized editors."""
        del request, args, kwargs
        match: Match = self.get_object()
        match_data = self._match_data(match)
        if match_data is None:
            return Response(
                {"detail": MATCH_TRACKER_DATA_NOT_FOUND},
                status=status.HTTP_404_NOT_FOUND,
            )
        with transaction.atomic():
            locked = MatchData.objects.select_for_update().get(pk=match_data.pk)
            return Response(
                {"events": build_match_event_history(locked)},
                status=status.HTTP_200_OK,
            )

    @action(
        detail=True,
        methods=("GET",),
        url_path="events/reconciliations",
        permission_classes=[IsCoachOrAdmin],
    )
    def event_reconciliations(
        self: _MatchViewSetLike,
        request: Request,
        *args: Any,
        **kwargs: Any,
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
        self: _MatchViewSetLike,
        request: Request,
        reconciliation_id: str,
        *args: Any,
        **kwargs: Any,
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
        payload = _request_payload(request)
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
                )
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

    @action(
        detail=True,
        methods=("POST",),
        url_path="events/goals",
        permission_classes=[IsCoachOrAdmin],
    )
    def create_goal(
        self: _MatchViewSetLike,
        request: Request,
        *args: Any,
        **kwargs: Any,
    ) -> Response:
        """Create a goal (Shot) event for this match."""
        match: Match = self.get_object()
        match_data = self._match_data(match)
        if not match_data:
            return Response(
                {"detail": MATCH_TRACKER_DATA_NOT_FOUND},
                status=status.HTTP_404_NOT_FOUND,
            )

        def create(locked: MatchData) -> Shot:
            serializer = ShotWriteSerializer(
                data=request.data,
                context={"match": match, "match_data": locked},
            )
            serializer.is_valid(raise_exception=True)
            return serializer.save()

        match_data, shot = apply_editor_mutation(
            match_data_id=match_data.pk,
            actor=request.user,
            mutate=create,
        )
        return Response(
            serialize_goal_event(match_data, shot),
            status=status.HTTP_201_CREATED,
        )

    @action(
        detail=True,
        methods=("PATCH", "DELETE"),
        url_path=r"events/goals/(?P<shot_id>[^/.]+)",
        permission_classes=[IsCoachOrAdmin],
    )
    def goal_detail(
        self: _MatchViewSetLike,
        request: Request,
        shot_id: str,
        *args: Any,
        **kwargs: Any,
    ) -> Response:
        """Update or delete an existing goal (Shot) event."""
        match: Match = self.get_object()
        match_data = self._match_data(match)
        if not match_data:
            return Response(
                {"detail": MATCH_TRACKER_DATA_NOT_FOUND},
                status=status.HTTP_404_NOT_FOUND,
            )

        if request.method == "DELETE":

            def delete(locked: MatchData) -> bool:
                shot = Shot.objects.filter(
                    id_uuid=shot_id,
                    match_data=locked,
                ).first()
                if shot is None:
                    return False
                shot.delete()
                return True

            _match_data, deleted = apply_editor_mutation(
                match_data_id=match_data.pk,
                actor=request.user,
                mutate=delete,
                no_op_result=False,
            )
            if not deleted:
                return Response(
                    {"detail": "Goal event not found."},
                    status=status.HTTP_404_NOT_FOUND,
                )
            return Response(status=status.HTTP_204_NO_CONTENT)

        def update(locked: MatchData) -> Shot | None:
            shot = Shot.objects.filter(id_uuid=shot_id, match_data=locked).first()
            if shot is None:
                return None
            serializer = ShotWriteSerializer(
                instance=shot,
                data=request.data,
                partial=True,
                context={"match": match, "match_data": locked},
            )
            serializer.is_valid(raise_exception=True)
            return serializer.save()

        match_data, shot = apply_editor_mutation(
            match_data_id=match_data.pk,
            actor=request.user,
            mutate=update,
            no_op_result=None,
        )
        if shot is None:
            return Response(
                {"detail": "Goal event not found."},
                status=status.HTTP_404_NOT_FOUND,
            )
        return Response(
            serialize_goal_event(match_data, shot),
            status=status.HTTP_200_OK,
        )

    @action(
        detail=True,
        methods=("POST",),
        url_path="events/substitutes",
        permission_classes=[IsCoachOrAdmin],
    )
    def create_substitute(
        self: _MatchViewSetLike,
        request: Request,
        *args: Any,
        **kwargs: Any,
    ) -> Response:
        """Create a substitution (PlayerChange) event for this match."""
        match: Match = self.get_object()
        match_data = self._match_data(match)
        if not match_data:
            return Response(
                {"detail": MATCH_TRACKER_DATA_NOT_FOUND},
                status=status.HTTP_404_NOT_FOUND,
            )

        def create(locked: MatchData) -> PlayerChange:
            serializer = PlayerChangeWriteSerializer(
                data=request.data,
                context={"match": match, "match_data": locked},
            )
            serializer.is_valid(raise_exception=True)
            return serializer.save()

        match_data, change = apply_editor_mutation(
            match_data_id=match_data.pk,
            actor=request.user,
            mutate=create,
        )
        return Response(
            serialize_substitute_event(match_data, change),
            status=status.HTTP_201_CREATED,
        )

    @action(
        detail=True,
        methods=("PATCH", "DELETE"),
        url_path=r"events/substitutes/(?P<change_id>[^/.]+)",
        permission_classes=[IsCoachOrAdmin],
    )
    def substitute_detail(
        self: _MatchViewSetLike,
        request: Request,
        change_id: str,
        *args: Any,
        **kwargs: Any,
    ) -> Response:
        """Update or delete a substitution (PlayerChange) event."""
        match: Match = self.get_object()
        match_data = self._match_data(match)
        if not match_data:
            return Response(
                {"detail": MATCH_TRACKER_DATA_NOT_FOUND},
                status=status.HTTP_404_NOT_FOUND,
            )

        if request.method == "DELETE":

            def delete(locked: MatchData) -> bool:
                change = PlayerChange.objects.filter(
                    id_uuid=change_id,
                    player_group__match_data=locked,
                ).first()
                if change is None:
                    return False
                change.delete()
                return True

            _match_data, deleted = apply_editor_mutation(
                match_data_id=match_data.pk,
                actor=request.user,
                mutate=delete,
                no_op_result=False,
            )
            if not deleted:
                return Response(
                    {"detail": "Substitution event not found."},
                    status=status.HTTP_404_NOT_FOUND,
                )
            return Response(status=status.HTTP_204_NO_CONTENT)

        def update(locked: MatchData) -> PlayerChange | None:
            change = PlayerChange.objects.filter(
                id_uuid=change_id,
                player_group__match_data=locked,
            ).first()
            if change is None:
                return None
            serializer = PlayerChangeWriteSerializer(
                instance=change,
                data=request.data,
                partial=True,
                context={"match": match, "match_data": locked},
            )
            serializer.is_valid(raise_exception=True)
            return serializer.save()

        match_data, change = apply_editor_mutation(
            match_data_id=match_data.pk,
            actor=request.user,
            mutate=update,
            no_op_result=None,
        )
        if change is None:
            return Response(
                {"detail": "Substitution event not found."},
                status=status.HTTP_404_NOT_FOUND,
            )
        return Response(
            serialize_substitute_event(match_data, change),
            status=status.HTTP_200_OK,
        )

    @action(
        detail=True,
        methods=("POST",),
        url_path="events/pauses",
        permission_classes=[IsCoachOrAdmin],
    )
    def create_pause(
        self: _MatchViewSetLike,
        request: Request,
        *args: Any,
        **kwargs: Any,
    ) -> Response:
        """Create a pause (Pause) event for this match."""
        match: Match = self.get_object()
        match_data = self._match_data(match)
        if not match_data:
            return Response(
                {"detail": MATCH_TRACKER_DATA_NOT_FOUND},
                status=status.HTTP_404_NOT_FOUND,
            )

        def create(locked: MatchData) -> Pause:
            serializer = PauseWriteSerializer(
                data=request.data,
                context={"match": match, "match_data": locked},
            )
            serializer.is_valid(raise_exception=True)
            return serializer.save()

        match_data, pause = apply_editor_mutation(
            match_data_id=match_data.pk,
            actor=request.user,
            mutate=create,
        )
        return Response(
            serialize_pause_event(match_data, pause),
            status=status.HTTP_201_CREATED,
        )

    @action(
        detail=True,
        methods=("PATCH", "DELETE"),
        url_path=r"events/pauses/(?P<pause_id>[^/.]+)",
        permission_classes=[IsCoachOrAdmin],
    )
    def pause_detail(
        self: _MatchViewSetLike,
        request: Request,
        pause_id: str,
        *args: Any,
        **kwargs: Any,
    ) -> Response:
        """Update or delete a pause (Pause) event."""
        match: Match = self.get_object()
        match_data = self._match_data(match)
        if not match_data:
            return Response(
                {"detail": MATCH_TRACKER_DATA_NOT_FOUND},
                status=status.HTTP_404_NOT_FOUND,
            )

        if request.method == "DELETE":

            def delete(locked: MatchData) -> bool:
                pause = Pause.objects.filter(
                    id_uuid=pause_id,
                    match_data=locked,
                ).first()
                if pause is None:
                    return False
                Timeout.objects.filter(pause=pause).delete()
                pause.delete()
                return True

            _match_data, deleted = apply_editor_mutation(
                match_data_id=match_data.pk,
                actor=request.user,
                mutate=delete,
                no_op_result=False,
            )
            if not deleted:
                return Response(
                    {"detail": "Pause event not found."},
                    status=status.HTTP_404_NOT_FOUND,
                )
            return Response(status=status.HTTP_204_NO_CONTENT)

        def update(locked: MatchData) -> Pause | None:
            pause = Pause.objects.filter(
                id_uuid=pause_id,
                match_data=locked,
            ).first()
            if pause is None:
                return None
            serializer = PauseWriteSerializer(
                instance=pause,
                data=request.data,
                partial=True,
                context={"match": match, "match_data": locked},
            )
            serializer.is_valid(raise_exception=True)
            return serializer.save()

        match_data, pause = apply_editor_mutation(
            match_data_id=match_data.pk,
            actor=request.user,
            mutate=update,
            no_op_result=None,
        )
        if pause is None:
            return Response(
                {"detail": "Pause event not found."},
                status=status.HTTP_404_NOT_FOUND,
            )
        return Response(
            serialize_pause_event(match_data, pause),
            status=status.HTTP_200_OK,
        )

    @action(
        detail=True,
        methods=("POST",),
        url_path="events/timeouts",
        permission_classes=[IsCoachOrAdmin],
    )
    def create_timeout(
        self: _MatchViewSetLike,
        request: Request,
        *args: Any,
        **kwargs: Any,
    ) -> Response:
        """Create a timeout (Timeout + Pause) event for this match."""
        match: Match = self.get_object()
        match_data = self._match_data(match)
        if not match_data:
            return Response(
                {"detail": MATCH_TRACKER_DATA_NOT_FOUND},
                status=status.HTTP_404_NOT_FOUND,
            )

        def create(locked: MatchData) -> Timeout:
            serializer = TimeoutWriteSerializer(
                data=request.data,
                context={"match": match, "match_data": locked},
            )
            serializer.is_valid(raise_exception=True)
            return serializer.save()

        match_data, timeout = apply_editor_mutation(
            match_data_id=match_data.pk,
            actor=request.user,
            mutate=create,
        )
        if not timeout.pause:
            return Response(
                {"detail": "Timeout was created without a pause."},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )
        return Response(
            serialize_pause_event(match_data, timeout.pause),
            status=status.HTTP_201_CREATED,
        )

    @action(
        detail=True,
        methods=("GET",),
        url_path="events/options",
        permission_classes=[IsCoachOrAdmin],
    )
    def event_options(
        self: _MatchViewSetLike,
        request: Request,
        *args: Any,
        **kwargs: Any,
    ) -> Response:
        """Return option lists needed to create/update match tracker events."""
        match: Match = self.get_object()
        match_data = self._match_data(match)
        if not match_data:
            return Response(
                {"detail": MATCH_TRACKER_DATA_NOT_FOUND},
                status=status.HTTP_404_NOT_FOUND,
            )

        match_parts = list(
            MatchPart.objects.filter(match_data=match_data).order_by("part_number")
        )
        player_groups = list(
            match_data.player_groups.select_related(
                "team",
                "starting_type",
                "current_type",
            ).prefetch_related(
                "players__user",
            )
        )
        goal_types = list(GoalType.objects.order_by("name"))

        players_by_id: dict[str, dict[str, str]] = {}
        for group in player_groups:
            for player in group.players.all():
                players_by_id[str(player.id_uuid)] = {
                    "id_uuid": str(player.id_uuid),
                    "username": player.user.username,
                }

        home_label = f"{match.home_team.club.name} {match.home_team.name}".strip()
        away_label = f"{match.away_team.club.name} {match.away_team.name}".strip()

        return Response(
            {
                "teams": [
                    {
                        "id_uuid": str(match.home_team.id_uuid),
                        "label": home_label,
                        "side": "home",
                    },
                    {
                        "id_uuid": str(match.away_team.id_uuid),
                        "label": away_label,
                        "side": "away",
                    },
                ],
                "match_parts": [
                    {
                        "id_uuid": str(part.id_uuid),
                        "part_number": part.part_number,
                        "start_time": part.start_time.isoformat(),
                        "end_time": (
                            part.end_time.isoformat() if part.end_time else None
                        ),
                        "active": part.active,
                    }
                    for part in match_parts
                ],
                "goal_types": [
                    {"id_uuid": str(goal_type.id_uuid), "name": goal_type.name}
                    for goal_type in goal_types
                ],
                "players": sorted(
                    players_by_id.values(),
                    key=lambda row: row["username"].lower(),
                ),
                "player_groups": [
                    {
                        "id_uuid": str(group.id_uuid),
                        "team_id": str(group.team_id),
                        "starting_type": group.starting_type.name,
                        "current_type": group.current_type.name,
                        "label": f"{group.team.name} - {group.starting_type.name}",
                    }
                    for group in player_groups
                ],
            },
            status=status.HTTP_200_OK,
        )

    @action(
        detail=True,
        methods=("PATCH", "DELETE"),
        url_path=r"events/timeouts/(?P<timeout_id>[^/.]+)",
        permission_classes=[IsCoachOrAdmin],
    )
    def timeout_detail(
        self: _MatchViewSetLike,
        request: Request,
        timeout_id: str,
        *args: Any,
        **kwargs: Any,
    ) -> Response:
        """Update or delete a timeout (Timeout + Pause) event."""
        match: Match = self.get_object()
        match_data = self._match_data(match)
        if not match_data:
            return Response(
                {"detail": MATCH_TRACKER_DATA_NOT_FOUND},
                status=status.HTTP_404_NOT_FOUND,
            )

        if request.method == "DELETE":
            _match_data, deleted = apply_editor_mutation(
                match_data_id=match_data.pk,
                actor=request.user,
                mutate=lambda locked: _delete_timeout(locked, timeout_id),
                no_op_result=False,
            )
            if not deleted:
                return Response(
                    {"detail": "Timeout event not found."},
                    status=status.HTTP_404_NOT_FOUND,
                )
            return Response(status=status.HTTP_204_NO_CONTENT)

        match_data, timeout = apply_editor_mutation(
            match_data_id=match_data.pk,
            actor=request.user,
            mutate=lambda locked: _update_timeout(
                match_data=locked,
                match=match,
                timeout_id=timeout_id,
                data=request.data,
            ),
            no_op_result=None,
        )
        if timeout is None:
            return Response(
                {"detail": "Timeout event not found."},
                status=status.HTTP_404_NOT_FOUND,
            )
        if not timeout.pause:
            return Response(
                {"detail": "Timeout has no pause."},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )
        return Response(
            serialize_pause_event(match_data, timeout.pause),
            status=status.HTTP_200_OK,
        )
