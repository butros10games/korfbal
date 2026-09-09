"""ViewSets for team-related API endpoints."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from django.db import models
from django.db.models import QuerySet
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, extend_schema, extend_schema_view
from rest_framework import permissions, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import NotFound, PermissionDenied, ValidationError
from rest_framework.request import Request
from rest_framework.response import Response

from apps.game_tracker.services.match_impact import (
    LATEST_MATCH_IMPACT_ALGORITHM_VERSION,
    round_js_1dp,
)
from apps.kwt_common.api.pagination import StandardResultsSetPagination
from apps.kwt_common.api.permissions import IsStaffOrReadOnly
from apps.player.api.serializers import PlayerSongSerializer, PlayerSongUpdateSerializer
from apps.player.composition import update_owned_player_song_settings
from apps.player.models import Player
from apps.player.models.player_song import PlayerSong
from apps.player.services.goal_song import (
    GoalSongSelectionError,
    validate_ready_goal_songs,
)
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
from apps.team.api.permissions import (
    viewer_can_manage_roster,
    viewer_can_manage_team,
    viewer_player,
)
from apps.team.models.team import Team
from apps.team.models.team_data import TeamData
from apps.team.queries.overview import (
    main_roster_ids,
    player_impact_matches,
    resolve_team_season,
    team_data_for_season,
    team_matches,
    team_players,
    team_seasons,
)
from apps.team.services.goal_song_reads import (
    fallback_goal_song_audio_urls,
    fallback_goal_song_song_ids,
    song_entries_for_ids,
)
from apps.team.services.goal_songs import delete_team_player_song
from apps.team.services.impact_breakdowns import aggregate_player_impact_breakdowns
from apps.team.services.overview import (
    TeamOverviewOptions,
    build_team_overview_payload,
)
from apps.team.services.roster import change_team_membership

from .filters import TeamSearchFilter
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
    filter_backends = (TeamSearchFilter,)
    search_fields = ("name", "club__name")

    def get_queryset(self) -> QuerySet[Team]:
        """Optionally scope the paginated catalog to one club."""
        queryset = super().get_queryset()
        if (
            self.action == "list"
            and self.request.query_params.get("followed") == "true"
        ):
            if not self.request.user.is_authenticated:
                return queryset.none()
            queryset = queryset.filter(player__user=self.request.user)
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
        can_manage = viewer_can_manage_roster(request=request, team=team, season=season)
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
            .order_by("user__username", "name", "id_uuid")
        )
        return Response({
            "can_manage": can_manage,
            "players": [
                {"id_uuid": str(player.id_uuid), "username": player.display_name}
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
        if not viewer_can_manage_roster(request=request, team=team, season=season):
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
            .filter(
                models.Q(user__username__icontains=search)
                | models.Q(name__icontains=search)
            )
            .exclude(id_uuid__in=linked_ids)
            .order_by("user__username", "name", "id_uuid")[: _ROSTER_SEARCH_LIMIT + 1]
        )
        return Response({
            "players": [
                {"id_uuid": str(player.id_uuid), "username": player.display_name}
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

        payload = build_team_overview_payload(
            team=team,
            season=season,
            seasons=seasons_qs,
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

        fallback_ids = fallback_goal_song_song_ids(team=team, season=season)
        fallback_songs = song_entries_for_ids(
            songs=songs,
            ids=fallback_ids,
        )

        players_payload = []
        for player in players:
            player_id = str(player.id_uuid)
            player_song_rows = songs_by_player.get(player_id, [])
            players_payload.append({
                "id_uuid": player_id,
                "username": player.display_name,
                "display_name": player.display_name,
                "goal_song_song_ids": [
                    song_id for song_id in (player.goal_song_song_ids or []) if song_id
                ],
                "goal_song_songs": song_entries_for_ids(
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
        roster_player_ids = main_roster_ids(team=team, season=season)
        valid_songs = self._validated_ready_songs(
            ids=ids,
            songs_qs=player_songs_by_ids(song_ids=ids).filter(
                player_id__in=roster_player_ids
            ),
        )

        team_data = team_data_for_season(team=team, season=season)
        if team_data is None:
            raise ValidationError({"detail": "No TeamData found for this season."})

        team_data.fallback_goal_song_song_ids = ids
        team_data.save(update_fields=["fallback_goal_song_song_ids"])

        return Response({
            "fallback_goal_song_song_ids": ids,
            "fallback_goal_song_songs": song_entries_for_ids(
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
        roster_player_ids = main_roster_ids(team=team, season=season)
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
            "goal_song_songs": song_entries_for_ids(songs=songs, ids=ids),
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

        if not viewer_can_manage_team(
            request=request,
            team=team,
            season=season,
        ):
            return Response(
                {"detail": "You do not have permission to manage team goal songs."},
                status=status.HTTP_403_FORBIDDEN,
            )

        roster_player_ids = main_roster_ids(team=team, season=season)
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
                team_data=team_data_for_season(team=team, season=season),
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

        if not viewer_can_manage_team(
            request=request,
            team=team,
            season=season,
        ):
            return Response(
                {"detail": "You do not have permission to manage team goal songs."},
                status=status.HTTP_403_FORBIDDEN,
            )

        roster_player_ids = main_roster_ids(team=team, season=season)
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
        if viewer_can_manage_team(
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
        try:
            return validate_ready_goal_songs(ids=ids, songs=songs_qs)
        except GoalSongSelectionError as exc:
            detail: dict[str, object] = {"detail": exc.detail}
            if exc.missing is not None:
                detail["missing"] = exc.missing
            if exc.not_ready is not None:
                detail["not_ready"] = exc.not_ready
            raise ValidationError(detail) from exc

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
