"""MatchViewSet actions for stats."""

from __future__ import annotations

from rest_framework import status
from rest_framework.decorators import action
from rest_framework.request import Request
from rest_framework.response import Response

from apps.competition.services.match_prediction import match_prediction
from apps.game_tracker.services.match_impacts_payload import build_match_impacts_payload
from apps.game_tracker.services.match_stats_payload import build_match_stats_payload
from apps.kwt_common.utils.match_summary import build_match_summaries
from apps.schedule.models import Match

from .match_viewset_contracts import MatchViewSetContext


class MatchStatsActionsMixin:
    """Schedule actions for stats."""

    @action(detail=True, methods=("GET",), url_path="summary")
    def summary(
        self: MatchViewSetContext,
        request: Request,
        *args: object,
        **kwargs: object,
    ) -> Response:
        """Return a match summary payload for a single match.

        This is used by the korfbal-web Match page hero header to show
        score/status/time/parts in the same layout as other match elements.

        Returns:
            Response: Match summary dictionary or None.

        """
        match: Match = self.get_object()
        match_data = self._match_data(match)
        if not match_data:
            return Response(None, status=status.HTTP_200_OK)

        summary = build_match_summaries([match_data])[0]
        summary["prediction"] = match_prediction(match)
        return Response(summary)

    @action(detail=True, methods=("GET",), url_path="stats")
    def stats(
        self: MatchViewSetContext,
        request: Request,
        *args: object,
        **kwargs: object,
    ) -> Response:
        """Return match-level statistics for a single match.

        Payload shape mirrors the existing Team overview stats so the
        korfbal-web Match page can reuse the same UI patterns.

        Notes:
            We treat "for" as home-team and "against" as away-team.

        Returns:
            Response: JSON payload with a `general` stats object (or null).

        """
        match: Match = self.get_object()
        match_data = self._match_data(match)
        if not match_data:
            return Response(
                {
                    "general": None,
                    "players": {"home": [], "away": []},
                    "meta": {
                        "home_team_id": str(match.home_team.id_uuid),
                        "away_team_id": str(match.away_team.id_uuid),
                    },
                },
                status=status.HTTP_200_OK,
            )

        payload = build_match_stats_payload(match=match, match_data=match_data)
        return Response(payload, status=status.HTTP_200_OK)

    @action(detail=True, methods=("GET",), url_path="impacts")
    def impacts(
        self: MatchViewSetContext,
        request: Request,
        *args: object,
        **kwargs: object,
    ) -> Response:
        """Return per-player impact scores for a match (latest algorithm).

        Notes:
            Historically we relied on an async Celery task to persist impacts shortly
            after timeline changes.

            To keep the UI consistent (and avoid heuristic fallbacks), we now
            opportunistically self-heal: for finished matches, if latest-version rows
            are missing we recompute + persist them in-request (best effort, guarded
            by cache locks).

        """
        match = self.get_object()
        return Response(
            build_match_impacts_payload(
                match=match, match_data=self._match_data(match)
            ),
            status=status.HTTP_200_OK,
        )
