"""Team overview, pool fixtures and impact reads."""

from __future__ import annotations

from typing import Any

from drf_spectacular.utils import extend_schema
from rest_framework import viewsets
from rest_framework.decorators import action
from rest_framework.request import Request
from rest_framework.response import Response

from apps.game_tracker.queries.match_summaries import build_match_summaries
from apps.game_tracker.services.match_impact import (
    LATEST_MATCH_IMPACT_ALGORITHM_VERSION,
    round_js_1dp,
)
from apps.kwt_common.api.params import bool_query_param, uuid_query_value
from apps.player.models import Player
from apps.schedule.queries.seasons import unavailable_seasons
from apps.team.api.permissions import (
    viewer_can_manage_team,
    viewer_player,
)
from apps.team.queries.overview import (
    player_impact_matches,
    resolve_team_season,
    team_pool_matches,
    team_seasons,
)
from apps.team.services.goal_song_reads import (
    fallback_goal_song_audio_urls,
)
from apps.team.services.impact_breakdowns import aggregate_player_impact_breakdowns
from apps.team.services.overview import (
    TeamOverviewOptions,
    build_team_overview_payload,
)

from .serializers import (
    TeamPoolMatchesPageSerializer,
    TeamPoolMatchesQuerySerializer,
)


class TeamOverviewActions(viewsets.GenericViewSet):
    """Read team overview, poule fixtures and player impact breakdowns."""

    @extend_schema(
        parameters=[TeamPoolMatchesQuerySerializer],
        responses=TeamPoolMatchesPageSerializer,
    )
    @action(detail=True, methods=("GET",), url_path="pool-matches", filter_backends=[])
    def pool_matches(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        """Return a page of matches from the team's season-specific pools."""
        team = self.get_object()
        query = TeamPoolMatchesQuerySerializer(data=request.query_params)
        query.is_valid(raise_exception=True)
        matches = team_pool_matches(
            team,
            query.validated_data["season"],
            finished=query.validated_data["status"] == "finished",
        )
        page = self.paginate_queryset(matches)
        return self.get_paginated_response(build_match_summaries(page or []))

    @action(detail=True, methods=("GET",), url_path="overview")
    def overview(
        self,
        request: Request,
        *args: Any,
        **kwargs: Any,
    ) -> Response:
        """Return match summaries, stats, roster data, and season options.

        Returns:
            Response: Aggregated team overview data.

        """
        team = self.get_object()
        seasons_qs = list(team_seasons(team))
        gaps = unavailable_seasons(seasons_qs)
        season = resolve_team_season(
            request.query_params.get("season"), seasons_qs, gaps
        )

        include_stats = bool_query_param(
            request,
            "include_stats",
            default=True,
        )
        include_roster = bool_query_param(
            request,
            "include_roster",
            default=True,
        )

        payload = build_team_overview_payload(
            team=team,
            season=season,
            seasons=seasons_qs,
            unavailable=gaps,
            options=TeamOverviewOptions(
                include_stats=include_stats,
                include_roster=include_roster,
                viewer_player=viewer_player(request),
                viewer_can_manage_goal_songs=viewer_can_manage_team(
                    request=request,
                    team=team,
                    season=season,
                ),
                fallback_goal_song_audio_urls=fallback_goal_song_audio_urls(
                    team=team,
                    season=season,
                ),
                team_payload=self.get_serializer(team).data,
            ),
        )
        return Response(payload)

    @action(
        detail=True,
        methods=("GET",),
        url_path="impact-breakdown",
    )
    def impact_breakdown(
        self,
        request: Request,
        *args: Any,
        **kwargs: Any,
    ) -> Response:
        """Return match-impact category breakdown for a single player.

        Query params:
            - season: optional season id_uuid (same as /overview)
            - player: required player id_uuid

        Notes:
            This endpoint primarily reads breakdowns from the database
            (`PlayerMatchImpactBreakdown`). If a breakdown row is missing for a
            match, it may compute + persist it as a best-effort self-heal.

        """
        team = self.get_object()
        seasons_qs = list(team_seasons(team))
        season = resolve_team_season(
            request.query_params.get("season"),
            seasons_qs,
            unavailable_seasons(seasons_qs),
        )

        player_param = (request.query_params.get("player") or "").strip()
        if not player_param:
            return Response(
                {"detail": "Missing required query param: player"},
                status=400,
            )
        player_id = uuid_query_value(player_param, parameter="player")

        player = (
            Player.objects
            .select_related("user")
            .only(
                "id_uuid",
                "name",
                "user__username",
                "knkv_person_id",
                "knkv_privacy",
                "archived_at",
                "knkv_observed_at",
            )
            .filter(id_uuid=player_id)
            .first()
        )
        if not player:
            return Response({"detail": "Player not found"}, status=404)

        match_data_qs = player_impact_matches(
            team=team,
            season=season,
            player=player,
            algorithm_version=LATEST_MATCH_IMPACT_ALGORITHM_VERSION,
        )

        matches_considered, impact_total_raw, aggregated = (
            aggregate_player_impact_breakdowns(
                team=team,
                player=player,
                match_data_qs=match_data_qs,
            )
        )

        categories_payload = [
            {
                "key": key,
                "points": float(round_js_1dp(float(data["points"]))),
                "count": int(data["count"]),
            }
            for key, data in aggregated.items()
        ]
        categories_payload.sort(key=lambda c: abs(float(c["points"])), reverse=True)

        payload = {
            "team_id": str(team.id_uuid),
            "season_id": str(season.id_uuid) if season else None,
            "player_id": str(player.id_uuid),
            "player_username": player.display_name,
            "algorithm_version": LATEST_MATCH_IMPACT_ALGORITHM_VERSION,
            "matches_considered": matches_considered,
            "impact_total": float(round_js_1dp(impact_total_raw)),
            "categories": categories_payload,
        }
        return Response(payload)
