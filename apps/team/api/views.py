"""ViewSets for team-related API endpoints."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from django.db import models
from django.db.models import Q, QuerySet
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, extend_schema, extend_schema_view
from rest_framework import filters, permissions, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import NotFound, PermissionDenied, ValidationError
from rest_framework.request import Request
from rest_framework.response import Response

from apps.game_tracker.models import (
    MatchData,
    PlayerMatchImpact,
    PlayerMatchImpactBreakdown,
)
from apps.game_tracker.services.match_impact import (
    LATEST_MATCH_IMPACT_ALGORITHM_VERSION,
    persist_match_impact_rows_with_breakdowns,
    round_js_1dp,
)
from apps.kwt_common.api.pagination import StandardResultsSetPagination
from apps.kwt_common.api.permissions import IsStaffOrReadOnly
from apps.player.api.serializers import PlayerSongSerializer, PlayerSongUpdateSerializer
from apps.player.composition import update_owned_player_song_settings
from apps.player.models import Player
from apps.player.models.player_song import PlayerSong, PlayerSongStatus
from apps.player.services.player_song_queries import (
    owned_player_song_or_none,
    player_songs_by_ids,
    player_songs_for_players,
)
from apps.player.services.player_songs import (
    PlayerSongNotFoundError,
    PlayerSongSettingsPatch,
)
from apps.schedule.models import Season
from apps.team.models.team import Team
from apps.team.models.team_data import TeamData
from apps.team.queries.overview import (
    resolve_team_season,
    team_matches,
    team_players,
    team_seasons,
)
from apps.team.services.goal_songs import delete_team_player_song
from apps.team.services.overview import (
    TeamOverviewOptions,
    build_team_overview_payload,
)
from apps.team.services.roster import change_team_membership

from .serializers import TeamRosterMutationSerializer, TeamSerializer


_ROSTER_SEARCH_MIN_LENGTH = 2
_ROSTER_SEARCH_LIMIT = 20

_PLAYER_ID_PARAMETER = OpenApiParameter(
    "player_id", OpenApiTypes.UUID, OpenApiParameter.PATH
)
_SONG_ID_PARAMETER = OpenApiParameter(
    "song_id", OpenApiTypes.UUID, OpenApiParameter.PATH
)


def _uuid_query_value(value: str, *, parameter: str) -> UUID:
    """Parse a UUID query value or raise a controlled API error.

    Raises:
        ValidationError: If the supplied value is not a UUID.

    """
    try:
        return UUID(value)
    except (AttributeError, ValueError):
        raise ValidationError({parameter: "Must be a valid UUID."}) from None


@extend_schema_view(
    update_player_goal_song_selection=extend_schema(parameters=[_PLAYER_ID_PARAMETER]),
    remove_player_song=extend_schema(
        parameters=[_PLAYER_ID_PARAMETER, _SONG_ID_PARAMETER]
    ),
    update_player_song_settings=extend_schema(
        parameters=[_PLAYER_ID_PARAMETER, _SONG_ID_PARAMETER]
    ),
)
class TeamViewSet(viewsets.ModelViewSet):
    """Expose team CRUD endpoints with lightweight search support."""

    queryset = (
        Team.objects
        .select_related("club")
        .order_by("club__name", "name", "id_uuid")
        .fetch_mode(models.FETCH_RAISE)
    )
    serializer_class = TeamSerializer
    pagination_class = StandardResultsSetPagination
    permission_classes = (IsStaffOrReadOnly,)
    lookup_field = "id_uuid"
    filter_backends = (filters.SearchFilter,)
    search_fields = ("name", "club__name")

    def get_queryset(self) -> QuerySet[Team]:
        """Optionally scope the paginated catalog to one club."""
        queryset = super().get_queryset()
        club_id = self.request.query_params.get("club")
        if not club_id:
            return queryset
        return queryset.filter(
            club__id_uuid=_uuid_query_value(club_id, parameter="club")
        )

    @action(
        detail=True,
        methods=("GET", "PATCH"),
        url_path="roster",
        permission_classes=[permissions.AllowAny],
    )
    def roster(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        """Read season membership or add/remove one player without replacing the roster.

        Raises:
            ValidationError: If the season or mutation is invalid.
            PermissionDenied: If the viewer cannot manage this season's team.
            NotFound: If the season does not exist.

        """
        team = self.get_object()
        season_id = request.query_params.get("season")
        if not season_id:
            raise ValidationError({"season": "Select a season."})
        season = Season.objects.filter(
            id_uuid=_uuid_query_value(season_id, parameter="season")
        ).first()
        if season is None:
            raise NotFound("Season not found.")
        can_manage = self._viewer_can_manage_team(
            request=request, team=team, season=season
        )
        if request.method == "PATCH":
            if not can_manage:
                raise PermissionDenied("You cannot manage this team's players.")
            serializer = TeamRosterMutationSerializer(data=request.data)
            serializer.is_valid(raise_exception=True)
            change_team_membership(
                team=team, season=season, **serializer.validated_data
            )
        players = (
            Player.objects
            .filter(team_data_as_player__team=team, team_data_as_player__season=season)
            .select_related("user")
            .distinct()
            .order_by("user__username", "id_uuid")
        )
        return Response({
            "can_manage": can_manage,
            "players": [
                {"id_uuid": str(player.id_uuid), "username": player.user.username}
                for player in players
            ],
        })

    @action(
        detail=True,
        methods=("GET",),
        url_path="roster-candidates",
        permission_classes=[permissions.IsAuthenticated],
        filter_backends=[],
    )
    def roster_candidates(
        self, request: Request, *args: Any, **kwargs: Any
    ) -> Response:
        """Search existing profiles for managers of the selected season.

        Raises:
            ValidationError: If no valid season is selected.
            PermissionDenied: If the viewer cannot manage this season's team.
            NotFound: If the season does not exist.

        """
        team = self.get_object()
        season_id = request.query_params.get("season")
        if not season_id:
            raise ValidationError({"season": "Select a season."})
        season = Season.objects.filter(
            id_uuid=_uuid_query_value(season_id, parameter="season")
        ).first()
        if season is None:
            raise NotFound("Season not found.")
        if not self._viewer_can_manage_team(request=request, team=team, season=season):
            raise PermissionDenied("You cannot manage this team's players.")
        search = request.query_params.get("search", "").strip()
        if len(search) < _ROSTER_SEARCH_MIN_LENGTH:
            return Response({"players": [], "has_more": False})
        linked_ids = TeamData.players.through.objects.filter(
            teamdata__team=team, teamdata__season=season
        ).values_list("player_id", flat=True)
        candidates = list(
            Player.objects
            .select_related("user")
            .filter(user__username__icontains=search)
            .exclude(id_uuid__in=linked_ids)
            .order_by("user__username", "id_uuid")[: _ROSTER_SEARCH_LIMIT + 1]
        )
        return Response({
            "players": [
                {"id_uuid": str(player.id_uuid), "username": player.user.username}
                for player in candidates[:_ROSTER_SEARCH_LIMIT]
            ],
            "has_more": len(candidates) > _ROSTER_SEARCH_LIMIT,
        })

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
        season = resolve_team_season(request.query_params.get("season"), seasons_qs)

        include_stats = self._parse_bool_query_param(
            request,
            "include_stats",
            default=True,
        )
        include_roster = self._parse_bool_query_param(
            request,
            "include_roster",
            default=True,
        )

        viewer_player = (
            Player.objects.filter(user=request.user).first()
            if request.user.is_authenticated
            else None
        )

        payload = build_team_overview_payload(
            team=team,
            season=season,
            seasons=seasons_qs,
            options=TeamOverviewOptions(
                include_stats=include_stats,
                include_roster=include_roster,
                viewer_player=viewer_player,
                viewer_can_manage_goal_songs=self._viewer_can_manage_team(
                    request=request,
                    team=team,
                    season=season,
                ),
                fallback_goal_song_audio_urls=self._fallback_goal_song_audio_urls(
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
        season = resolve_team_season(request.query_params.get("season"), seasons_qs)

        player_param = (request.query_params.get("player") or "").strip()
        if not player_param:
            return Response(
                {"detail": "Missing required query param: player"},
                status=400,
            )
        player_id = _uuid_query_value(player_param, parameter="player")

        player = (
            Player.objects
            .select_related("user")
            .only("id_uuid", "user__username")
            .filter(id_uuid=player_id)
            .first()
        )
        if not player:
            return Response({"detail": "Player not found"}, status=404)

        match_data_qs = self._impact_breakdown_match_queryset(
            team=team,
            season=season,
            player=player,
        )

        matches_considered, impact_total_raw, aggregated = (
            self._aggregate_player_impact_breakdowns(
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
            "player_username": player.user.username,
            "algorithm_version": LATEST_MATCH_IMPACT_ALGORITHM_VERSION,
            "matches_considered": matches_considered,
            "impact_total": float(round_js_1dp(impact_total_raw)),
            "categories": categories_payload,
        }
        return Response(payload)

    @action(
        detail=True,
        methods=("GET",),
        url_path="goal-song-admin",
        permission_classes=[permissions.IsAuthenticated],
    )
    def goal_song_admin(
        self,
        request: Request,
        *args: Any,
        **kwargs: Any,
    ) -> Response:
        """Return team player songs and fallback song configuration for moderation."""
        team = self.get_object()
        seasons_qs = list(team_seasons(team))
        season = resolve_team_season(request.query_params.get("season"), seasons_qs)

        self._ensure_goal_song_admin_access(
            request=request,
            team=team,
            season=season,
        )

        match_data_qs = team_matches(team, season)
        players = list(team_players(team, season, match_data_qs))
        songs = list(player_songs_for_players(players))

        songs_by_player: dict[str, list[PlayerSong]] = {}
        for song in songs:
            player_id = str(song.player_id)
            songs_by_player.setdefault(player_id, []).append(song)

        fallback_ids = self._fallback_goal_song_song_ids(team=team, season=season)
        fallback_songs = self._song_entries_for_ids(
            songs=songs,
            ids=fallback_ids,
        )

        players_payload = []
        for player in players:
            player_id = str(player.id_uuid)
            player_song_rows = songs_by_player.get(player_id, [])
            players_payload.append({
                "id_uuid": player_id,
                "username": player.user.username,
                "display_name": player.user.username,
                "goal_song_song_ids": [
                    song_id for song_id in (player.goal_song_song_ids or []) if song_id
                ],
                "goal_song_songs": self._song_entries_for_ids(
                    songs=player_song_rows,
                    ids=[
                        song_id
                        for song_id in (player.goal_song_song_ids or [])
                        if song_id
                    ],
                ),
                "songs": PlayerSongSerializer(player_song_rows, many=True).data,
            })

        payload = {
            "team": {
                "id_uuid": str(team.id_uuid),
                "name": team.name,
            },
            "season": {
                "id_uuid": str(season.id_uuid) if season is not None else None,
                "name": season.name if season is not None else None,
            },
            "fallback_goal_song_song_ids": fallback_ids,
            "fallback_goal_song_songs": fallback_songs,
            "players": players_payload,
        }
        return Response(payload)

    @action(
        detail=True,
        methods=("PATCH",),
        url_path="goal-song-admin/fallback",
        permission_classes=[permissions.IsAuthenticated],
    )
    def update_goal_song_fallback(
        self,
        request: Request,
        *args: Any,
        **kwargs: Any,
    ) -> Response:
        """Update the team fallback playlist used when a scorer has no own goal song.

        Raises:
          ValidationError: If payload/song ids are invalid or no season TeamData exists.

        """
        team = self.get_object()
        seasons_qs = list(team_seasons(team))
        season = resolve_team_season(request.query_params.get("season"), seasons_qs)

        self._ensure_goal_song_admin_access(
            request=request,
            team=team,
            season=season,
        )

        ids = self._parse_song_id_list_from_payload(
            payload=request.data,
            field_name="fallback_goal_song_song_ids",
        )
        roster_player_ids = self._team_roster_player_ids(team=team, season=season)
        valid_songs = self._validated_ready_songs(
            ids=ids,
            songs_qs=player_songs_by_ids(song_ids=ids).filter(
                player_id__in=roster_player_ids
            ),
        )

        team_data = self._team_data_for_season(team=team, season=season)
        if team_data is None:
            raise ValidationError({"detail": "No TeamData found for this season."})

        team_data.fallback_goal_song_song_ids = ids
        team_data.save(update_fields=["fallback_goal_song_song_ids"])

        return Response({
            "fallback_goal_song_song_ids": ids,
            "fallback_goal_song_songs": self._song_entries_for_ids(
                songs=valid_songs,
                ids=ids,
            ),
        })

    @action(
        detail=True,
        methods=("PATCH",),
        url_path=r"goal-song-admin/player/(?P<player_id>[^/.]+)",
        permission_classes=[permissions.IsAuthenticated],
    )
    def update_player_goal_song_selection(
        self,
        request: Request,
        player_id: str,
        *args: Any,
        **kwargs: Any,
    ) -> Response:
        """Update goal-song selection for a team player.

        Raises:
            NotFound: If the referenced player cannot be found.
            ValidationError: If payload/song ids are invalid or player is not in roster.

        """
        team = self.get_object()
        seasons_qs = list(team_seasons(team))
        season = resolve_team_season(request.query_params.get("season"), seasons_qs)

        self._ensure_goal_song_admin_access(
            request=request,
            team=team,
            season=season,
        )

        ids = self._parse_song_id_list_from_payload(
            payload=request.data,
            field_name="goal_song_song_ids",
        )
        roster_player_ids = self._team_roster_player_ids(team=team, season=season)
        if player_id not in roster_player_ids:
            raise ValidationError({"detail": "Player is not in this team roster."})

        player = Player.objects.select_related("user").filter(id_uuid=player_id).first()
        if player is None:
            raise NotFound(detail="Player not found")

        songs = self._validated_ready_songs(
            ids=ids,
            songs_qs=player_songs_by_ids(song_ids=ids, player=player),
        )
        by_id = {str(song.id_uuid): song for song in songs}

        player.goal_song_song_ids = ids
        update_fields: list[str] = ["goal_song_song_ids"]
        if ids:
            first = by_id[ids[0]]
            audio_file = first.effective_audio_file
            player.goal_song_uri = audio_file.url if audio_file else ""
            player.song_start_time = first.start_time_seconds
            update_fields.extend(["goal_song_uri", "song_start_time"])
        else:
            player.goal_song_uri = ""
            player.song_start_time = None
            update_fields.extend(["goal_song_uri", "song_start_time"])

        player.save(update_fields=update_fields)

        return Response({
            "player_id": str(player.id_uuid),
            "goal_song_song_ids": ids,
            "goal_song_songs": self._song_entries_for_ids(songs=songs, ids=ids),
        })

    @action(
        detail=True,
        methods=("DELETE",),
        url_path=(
            r"goal-song-admin/player/(?P<player_id>[^/.]+)/"
            r"songs/(?P<song_id>[^/.]+)"
        ),
        permission_classes=[permissions.IsAuthenticated],
    )
    def remove_player_song(
        self,
        request: Request,
        player_id: str,
        song_id: str,
        *args: Any,
        **kwargs: Any,
    ) -> Response:
        """Delete a player song from the team moderation view."""
        team = self.get_object()
        seasons_qs = list(team_seasons(team))
        season = resolve_team_season(request.query_params.get("season"), seasons_qs)

        if not self._viewer_can_manage_team(
            request=request,
            team=team,
            season=season,
        ):
            return Response(
                {"detail": "You do not have permission to manage team goal songs."},
                status=status.HTTP_403_FORBIDDEN,
            )

        roster_player_ids = self._team_roster_player_ids(team=team, season=season)
        if player_id not in roster_player_ids:
            return Response(
                {"detail": "Player is not in this team roster."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        player = Player.objects.filter(id_uuid=player_id).first()
        if player is None:
            return Response(
                {"detail": "Song not found"},
                status=status.HTTP_404_NOT_FOUND,
            )

        try:
            delete_team_player_song(
                player=player,
                song_id=song_id,
                team_data=self._team_data_for_season(team=team, season=season),
            )
        except PlayerSongNotFoundError:
            return Response(
                {"detail": "Song not found"},
                status=status.HTTP_404_NOT_FOUND,
            )
        return Response(status=status.HTTP_204_NO_CONTENT)

    @action(
        detail=True,
        methods=("PATCH",),
        url_path=(
            r"goal-song-admin/player/(?P<player_id>[^/.]+)/"
            r"songs/(?P<song_id>[^/.]+)/settings"
        ),
        permission_classes=[permissions.IsAuthenticated],
    )
    def update_player_song_settings(
        self,
        request: Request,
        player_id: str,
        song_id: str,
        *args: Any,
        **kwargs: Any,
    ) -> Response:
        """Update song timing/speed for a player song from team moderation."""
        team = self.get_object()
        seasons_qs = list(team_seasons(team))
        season = resolve_team_season(request.query_params.get("season"), seasons_qs)

        if not self._viewer_can_manage_team(
            request=request,
            team=team,
            season=season,
        ):
            return Response(
                {"detail": "You do not have permission to manage team goal songs."},
                status=status.HTTP_403_FORBIDDEN,
            )

        roster_player_ids = {
            str(player_id_value)
            for player_id_value in self._team_roster_player_ids(
                team=team, season=season
            )
        }
        if player_id not in roster_player_ids:
            return Response(
                {"detail": "Player is not in this team roster."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        player = Player.objects.filter(id_uuid=player_id).first()
        if player is None:
            return Response(
                {"detail": "Song not found"},
                status=status.HTTP_404_NOT_FOUND,
            )
        if owned_player_song_or_none(player=player, song_id=song_id) is None:
            return Response(
                {"detail": "Song not found"},
                status=status.HTTP_404_NOT_FOUND,
            )

        serializer = PlayerSongUpdateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            song = update_owned_player_song_settings(
                player=player,
                song_id=song_id,
                settings=PlayerSongSettingsPatch(
                    start_time_seconds=serializer.validated_data.get(
                        "start_time_seconds"
                    ),
                    playback_speed=serializer.validated_data.get("playback_speed"),
                ),
            )
        except PlayerSongNotFoundError:
            return Response(
                {"detail": "Song not found"},
                status=status.HTTP_404_NOT_FOUND,
            )

        return Response(PlayerSongSerializer(song).data)

    def _ensure_goal_song_admin_access(
        self,
        *,
        request: Request,
        team: Team,
        season: Season | None,
    ) -> None:
        if self._viewer_can_manage_team(
            request=request,
            team=team,
            season=season,
        ):
            return
        raise PermissionDenied(
            detail="You do not have permission to manage team goal songs."
        )

    @staticmethod
    def _parse_song_id_list_from_payload(
        *,
        payload: Any,
        field_name: str,
    ) -> list[str]:
        if not isinstance(payload, dict):
            raise ValidationError({"detail": "Invalid payload"})

        raw_ids = payload.get(field_name)
        if raw_ids is None:
            return []
        if not isinstance(raw_ids, list):
            raise ValidationError({"detail": f"{field_name} must be a list of strings"})

        ids: list[str] = []
        seen: set[str] = set()
        for entry in raw_ids:
            if not isinstance(entry, str):
                raise ValidationError({
                    "detail": f"{field_name} must be a list of strings"
                })
            song_id = entry.strip()
            if not song_id or song_id in seen:
                continue
            seen.add(song_id)
            ids.append(song_id)
        return ids

    @staticmethod
    def _validated_ready_songs(
        *,
        ids: list[str],
        songs_qs: QuerySet[PlayerSong],
    ) -> list[PlayerSong]:
        songs = list(songs_qs)
        by_id = {str(song.id_uuid): song for song in songs}

        missing = [song_id for song_id in ids if song_id not in by_id]
        if missing:
            raise ValidationError({"detail": "Unknown song id(s)", "missing": missing})

        not_ready: list[str] = []
        for song_id in ids:
            song = by_id[song_id]
            cached = song.cached_song
            status_value = cached.status if cached is not None else song.status
            audio_file = cached.audio_file if cached is not None else song.audio_file
            if status_value != PlayerSongStatus.READY or not audio_file:
                not_ready.append(str(song.id_uuid))

        if not_ready:
            raise ValidationError({
                "detail": "Song(s) not ready",
                "not_ready": not_ready,
            })

        return [by_id[song_id] for song_id in ids]

    def _impact_breakdown_match_queryset(
        self,
        *,
        team: Team,
        season: Season | None,
        player: Player,
    ) -> QuerySet[MatchData]:
        match_data_qs = team_matches(team, season).filter(status="finished")

        # When available, prefer stored match-impact rows for the given player.
        # This keeps the match set tight (only games where the player actually
        # has stored impact rows) and avoids scanning all team matches.
        persisted_match_data_qs = match_data_qs.filter(
            player_impacts__player=player,
            player_impacts__algorithm_version=LATEST_MATCH_IMPACT_ALGORITHM_VERSION,
        ).distinct()
        if persisted_match_data_qs.exists():
            return persisted_match_data_qs

        # Important: do NOT rely solely on designated MatchPlayer rows. In real
        # data, those rows may be missing while shots/events and/or persisted
        # PlayerMatchImpact rows still exist.
        return match_data_qs.filter(
            Q(
                player_impacts__player=player,
                player_impacts__algorithm_version=LATEST_MATCH_IMPACT_ALGORITHM_VERSION,
            )
            | Q(players__player=player)
            | Q(shots__player=player)
        ).distinct()

    @staticmethod
    def _impact_breakdown_for_impact(*, impact: PlayerMatchImpact) -> dict[str, Any]:
        breakdown_obj = getattr(impact, "breakdown", None)
        if (
            breakdown_obj is not None
            and breakdown_obj.algorithm_version == LATEST_MATCH_IMPACT_ALGORITHM_VERSION
            and isinstance(breakdown_obj.breakdown, dict)
        ):
            return breakdown_obj.breakdown

        # Best-effort: compute+persist breakdowns so next request is fast.
        try:
            persist_match_impact_rows_with_breakdowns(
                match_data=impact.match_data,
                algorithm_version=LATEST_MATCH_IMPACT_ALGORITHM_VERSION,
            )
        except Exception:
            return {}

        refreshed = (
            PlayerMatchImpactBreakdown.objects
            .filter(
                impact=impact,
                algorithm_version=LATEST_MATCH_IMPACT_ALGORITHM_VERSION,
            )
            .only("breakdown")
            .first()
        )
        if refreshed is None or not isinstance(refreshed.breakdown, dict):
            return {}
        return refreshed.breakdown

    def _aggregate_player_impact_breakdowns(
        self,
        *,
        team: Team,
        player: Player,
        match_data_qs: QuerySet[MatchData],
    ) -> tuple[int, float, dict[str, dict[str, float | int]]]:
        aggregated: dict[str, dict[str, float | int]] = {}
        matches_considered = 0
        impact_total_raw = 0.0

        impacts_qs = (
            PlayerMatchImpact.objects
            .filter(
                match_data__in=match_data_qs,
                player=player,
                team=team,
                algorithm_version=LATEST_MATCH_IMPACT_ALGORITHM_VERSION,
            )
            .select_related("match_data")
            .select_related("breakdown")
        )

        for impact in impacts_qs.iterator():
            matches_considered += 1
            impact_total_raw += float(impact.impact_score)

            per_player = self._impact_breakdown_for_impact(impact=impact)
            for key, item in per_player.items():
                if key not in aggregated:
                    aggregated[key] = {"points": 0.0, "count": 0}
                aggregated[key]["points"] = float(aggregated[key]["points"]) + float(
                    item["points"]
                )
                aggregated[key]["count"] = int(aggregated[key]["count"]) + int(
                    item["count"]
                )

        return matches_considered, impact_total_raw, aggregated

    @staticmethod
    def _viewer_player(request: Request) -> Player | None:
        if not request.user.is_authenticated:
            return None
        return Player.objects.filter(user=request.user).first()

    def _viewer_can_manage_team(
        self,
        *,
        request: Request,
        team: Team,
        season: Season | None,
    ) -> bool:
        user = request.user
        if not user.is_authenticated:
            return False
        if bool(
            getattr(user, "is_staff", False) or getattr(user, "is_superuser", False)
        ):
            return True

        viewer = self._viewer_player(request)
        if viewer is None:
            return False

        if team.club.admin.filter(id_uuid=viewer.id_uuid).exists():
            return True

        team_data_qs = TeamData.objects.filter(team=team, coach=viewer)
        if season is not None:
            team_data_qs = team_data_qs.filter(season=season)
        return team_data_qs.exists()

    @staticmethod
    def _team_data_for_season(*, team: Team, season: Season | None) -> TeamData | None:
        queryset = TeamData.objects.filter(team=team)
        if season is not None:
            queryset = queryset.filter(season=season)
        return queryset.order_by("-season__start_date").first()

    @staticmethod
    def _team_roster_player_ids(*, team: Team, season: Season | None) -> set[str]:
        team_data_qs = TeamData.objects.filter(team=team)
        if season is not None:
            team_data_qs = team_data_qs.filter(season=season)
        return {
            str(player_id)
            for player_id in TeamData.players.through.objects.filter(
                teamdata_id__in=team_data_qs.values_list("id", flat=True)
            ).values_list("player_id", flat=True)
        }

    def _fallback_goal_song_song_ids(
        self,
        *,
        team: Team,
        season: Season | None,
    ) -> list[str]:
        team_data = self._team_data_for_season(team=team, season=season)
        if team_data is None:
            return []
        seen: set[str] = set()
        normalized: list[str] = []
        for entry in team_data.fallback_goal_song_song_ids or []:
            if not isinstance(entry, str):
                continue
            song_id = entry.strip()
            if not song_id or song_id in seen:
                continue
            seen.add(song_id)
            normalized.append(song_id)
        return normalized

    @staticmethod
    def _song_entry(song: PlayerSong) -> dict[str, object] | None:
        audio_file = song.effective_audio_file
        if song.effective_status != PlayerSongStatus.READY or not audio_file:
            return None
        return {
            "id_uuid": str(song.id_uuid),
            "audio_url": audio_file.url,
            "start_time_seconds": int(song.start_time_seconds or 0),
            "playback_speed": float(song.playback_speed or 1.0),
            "title": song.effective_title,
            "artists": song.effective_artists,
            "player_id": str(song.player_id),
        }

    def _song_entries_for_ids(
        self,
        *,
        songs: list[PlayerSong],
        ids: list[str],
    ) -> list[dict[str, object]]:
        by_id = {str(song.id_uuid): song for song in songs}
        ordered: list[dict[str, object]] = []
        for song_id in ids:
            song = by_id.get(song_id)
            if song is None:
                continue
            entry = self._song_entry(song)
            if entry is None:
                continue
            ordered.append(entry)
        return ordered

    def _fallback_goal_song_audio_urls(
        self,
        *,
        team: Team,
        season: Season | None,
    ) -> list[str]:
        ids = self._fallback_goal_song_song_ids(team=team, season=season)
        if not ids:
            return []

        roster_player_ids = self._team_roster_player_ids(team=team, season=season)
        songs = list(
            player_songs_by_ids(song_ids=ids).filter(player_id__in=roster_player_ids)
        )
        entries = self._song_entries_for_ids(songs=songs, ids=ids)
        audio_urls: list[str] = []
        for entry in entries:
            audio_url = entry.get("audio_url")
            if isinstance(audio_url, str):
                audio_urls.append(audio_url)
        return audio_urls

    @staticmethod
    def _parse_bool_query_param(
        request: Request,
        name: str,
        *,
        default: bool,
    ) -> bool:
        raw = request.query_params.get(name)
        if raw is None:
            return default
        if not raw:
            return default
        normalized = raw.strip().lower()
        if normalized in {"1", "true", "t", "yes", "y", "on"}:
            return True
        if normalized in {"0", "false", "f", "no", "n", "off"}:
            return False
        return default
