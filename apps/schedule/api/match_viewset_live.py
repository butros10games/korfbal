"""MatchViewSet actions for live."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import permissions, status
from rest_framework.decorators import action
from rest_framework.request import Request
from rest_framework.response import Response

from apps.game_tracker.composition import apply_tracker_command
from apps.game_tracker.models import MatchData
from apps.game_tracker.services.tracker_commands import TrackerCommandError
from apps.game_tracker.services.tracker_state import (
    get_tracker_state,
    poll_tracker_state,
)
from apps.schedule.models import Match
from apps.team.models.team import Team

from .match_viewset_contracts import MatchViewSetContext
from .permissions import HasTrackerAccess
from .validation import UUID_URL_REGEX


def _tracker_read_error(exc: TrackerCommandError) -> Response:
    """Translate tracker read failures without exposing command-only details."""
    code = getattr(exc, "code", "error")
    http_status = (
        status.HTTP_404_NOT_FOUND
        if code == "not_found"
        else status.HTTP_400_BAD_REQUEST
    )
    return Response({"detail": str(exc), "code": code}, status=http_status)


def _parse_since_revision(raw: str | None) -> int | None:
    """Parse a non-negative live revision, allowing -1 for an initial snapshot."""
    try:
        revision = int(raw) if raw else -1
    except (TypeError, ValueError):
        return None
    return revision if revision >= -1 else None


def _public_live_state(
    *,
    match: Match,
    match_data: MatchData,
    home_tracker_state: dict[str, Any],
) -> dict[str, Any]:
    """Return a safe, match-level live payload.

    Notes:
        We intentionally only expose match-wide fields here (timer/score/etc)
        so the Match page can live-update without requiring coach permissions
        and without exposing the full tracker roster state.

    """
    score = home_tracker_state.get("score")
    home = 0
    away = 0
    if isinstance(score, dict):
        home = int(score.get("for") or 0)
        away = int(score.get("against") or 0)

    return {
        "match_id": str(match.id_uuid),
        "match_data_id": str(match_data.id_uuid),
        "status": match_data.status,
        "current_part": int(home_tracker_state.get("current_part") or 0),
        "parts": int(home_tracker_state.get("parts") or 0),
        "paused": bool(home_tracker_state.get("paused")),
        "timer": home_tracker_state.get("timer"),
        "score": {"home": home, "away": away},
        "last_changed_at": home_tracker_state.get("last_changed_at"),
        "live_revision": match_data.live_revision,
        **(
            {"resources": home_tracker_state["resources"]}
            if isinstance(home_tracker_state.get("resources"), list)
            else {}
        ),
    }


class MatchLiveActionsMixin:
    """Schedule actions for live."""

    @action(
        detail=True,
        methods=("GET",),
        url_path=rf"tracker/(?P<team_id>{UUID_URL_REGEX})/state",
        permission_classes=[HasTrackerAccess],
    )
    def tracker_state(
        self: MatchViewSetContext,
        request: Request,
        team_id: str,
        *args: object,
        **kwargs: object,
    ) -> Response:
        """Return the live match tracker state for a given team perspective."""
        match: Match = self.get_object()
        team = get_object_or_404(Team.objects.select_related("club"), id_uuid=team_id)
        try:
            return Response(
                get_tracker_state(match, team=team),
                status=status.HTTP_200_OK,
            )
        except TrackerCommandError as exc:
            return _tracker_read_error(exc)

    @action(
        detail=True,
        methods=("POST",),
        url_path=rf"tracker/(?P<team_id>{UUID_URL_REGEX})/commands",
        permission_classes=[HasTrackerAccess],
    )
    def tracker_command(
        self: MatchViewSetContext,
        request: Request,
        team_id: str,
        *args: object,
        **kwargs: object,
    ) -> Response:
        """Apply a match tracker command and return updated state."""
        match: Match = self.get_object()
        team = get_object_or_404(Team.objects.select_related("club"), id_uuid=team_id)
        if not isinstance(request.data, Mapping):
            return Response(
                {"detail": "Invalid JSON body."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        try:
            return Response(
                apply_tracker_command(
                    match,
                    team=team,
                    payload=dict(request.data),
                    actor=request.user,
                ),
                status=status.HTTP_200_OK,
            )
        except TrackerCommandError as exc:
            code = getattr(exc, "code", "error")
            if code in {
                "client_sequence_conflict",
                "match_paused",
                "revision_conflict",
                "idempotency_conflict",
            }:
                return Response(
                    {"detail": str(exc), "code": code, **exc.details},
                    status=status.HTTP_409_CONFLICT,
                )
            http_status = (
                status.HTTP_404_NOT_FOUND
                if code == "not_found"
                else status.HTTP_400_BAD_REQUEST
            )
            return Response(
                {"detail": str(exc), "code": code, **exc.details},
                status=http_status,
            )

    @action(
        detail=True,
        methods=("GET",),
        url_path=rf"tracker/(?P<team_id>{UUID_URL_REGEX})/poll",
        permission_classes=[HasTrackerAccess],
    )
    def tracker_poll(
        self: MatchViewSetContext,
        request: Request,
        team_id: str,
        *args: object,
        **kwargs: object,
    ) -> Response:
        """Poll once for a match tracker revision update."""
        match: Match = self.get_object()
        team = get_object_or_404(Team.objects.select_related("club"), id_uuid=team_id)

        since_revision_raw = request.query_params.get("since_revision")
        timeout_raw = request.query_params.get("timeout")

        since_revision = _parse_since_revision(since_revision_raw)
        if since_revision is None:
            return Response(
                {"detail": "Invalid 'since_revision'."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            timeout_seconds = int(timeout_raw) if timeout_raw else 25
        except ValueError:
            timeout_seconds = 25

        try:
            return Response(
                poll_tracker_state(
                    match,
                    team=team,
                    since_revision=since_revision,
                    timeout_seconds=timeout_seconds,
                    compact=request.query_params.get("compact") == "1",
                ),
                status=status.HTTP_200_OK,
            )
        except TrackerCommandError as exc:
            return _tracker_read_error(exc)

    @action(
        detail=True,
        methods=("GET",),
        url_path="live",
        permission_classes=[permissions.AllowAny],
    )
    def live_state(
        self: MatchViewSetContext,
        request: Request,
        *args: object,
        **kwargs: object,
    ) -> Response:
        """Return a match-level live snapshot (timer + score).

        This endpoint is designed for read-only UIs like the korfbal-web Match
        page. It intentionally does not include player groups or other
        coach-only tracker details.

        """
        match: Match = self.get_object()
        match_data = self._match_data(match)
        if not match_data:
            return Response(None, status=status.HTTP_200_OK)

        try:
            home_tracker_state = get_tracker_state(match, team=match.home_team)
        except TrackerCommandError as exc:
            return _tracker_read_error(exc)

        return Response(
            _public_live_state(
                match=match,
                match_data=match_data,
                home_tracker_state=home_tracker_state,
            ),
            status=status.HTTP_200_OK,
        )

    @action(
        detail=True,
        methods=("GET",),
        url_path="live/poll",
        permission_classes=[permissions.AllowAny],
    )
    def live_poll(
        self: MatchViewSetContext,
        request: Request,
        *args: object,
        **kwargs: object,
    ) -> Response:
        """Poll once for match-level live updates (timer + score).

        Response shape mirrors tracker polling:
        - unchanged: {changed: false, server_time, last_changed_at}
        - on change: live_state payload

        """
        match: Match = self.get_object()
        match_data = self._match_data(match)
        if not match_data:
            return Response(
                {
                    "changed": False,
                    "server_time": timezone.now().isoformat(),
                    "last_changed_at": timezone.now().isoformat(),
                    "live_revision": 0,
                },
                status=status.HTTP_200_OK,
            )

        since_revision_raw = request.query_params.get("since_revision")
        timeout_raw = request.query_params.get("timeout")

        since_revision = _parse_since_revision(since_revision_raw)
        if since_revision is None:
            return Response(
                {"detail": "Invalid 'since_revision'."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            timeout_seconds = int(timeout_raw) if timeout_raw else 25
        except ValueError:
            timeout_seconds = 25

        try:
            payload = poll_tracker_state(
                match,
                team=match.home_team,
                since_revision=since_revision,
                timeout_seconds=timeout_seconds,
            )
        except TrackerCommandError as exc:
            return _tracker_read_error(exc)

        if isinstance(payload, dict) and payload.get("changed") is False:
            return Response(payload, status=status.HTTP_200_OK)

        # Changed: payload is a full tracker state (home team perspective)
        if not isinstance(payload, dict):
            return Response(
                {"detail": "Invalid live poll payload."},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

        return Response(
            _public_live_state(
                match=match,
                match_data=match_data,
                home_tracker_state=payload,
            ),
            status=status.HTTP_200_OK,
        )
