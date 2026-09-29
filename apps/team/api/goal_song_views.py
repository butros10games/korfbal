"""Team goal-song administration: fallback playlist, team songs and clips."""

from __future__ import annotations

from typing import Any, NoReturn

from django.core.files.uploadedfile import UploadedFile
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, extend_schema, extend_schema_view
from rest_framework import permissions, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import NotFound, PermissionDenied, ValidationError
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.request import Request
from rest_framework.response import Response

from apps.kwt_common.api.pagination import StandardResultsSetPagination
from apps.kwt_common.api.params import UUID_URL_REGEX, uuid_query_value
from apps.player.api.serializers import (
    PlayerSongClipCreateSerializer,
    PlayerSongCreateSerializer,
    PlayerSongSerializer,
    PlayerSongUpdateSerializer,
)
from apps.player.composition import create_song_clip, update_owned_player_song_settings
from apps.player.models import Player
from apps.player.services.goal_song import (
    GoalSongSelectionError,
    apply_goal_song_selection,
    validate_goal_song_ids,
)
from apps.player.services.player_song_queries import (
    owned_player_song_or_none,
)
from apps.player.services.player_songs import (
    InvalidSongClipError,
    PlayerSongAlreadyReadyError,
    PlayerSongNotFoundError,
    PlayerSongSettingsPatch,
)
from apps.player.services.upload_validation import InvalidAudioUploadError
from apps.schedule.models import Season
from apps.team.api.permissions import (
    viewer_can_manage_team,
)
from apps.team.composition import (
    add_team_library_clip,
    create_team_song,
    retry_team_song,
    update_team_song,
)
from apps.team.models.team import Team
from apps.team.models.team_data import TeamData
from apps.team.queries.overview import (
    main_roster_ids,
    resolve_team_season,
    team_data_for_season,
    team_seasons,
)
from apps.team.services.clip_library import search_team_clips
from apps.team.services.goal_song_admin import (
    MissingTeamSeasonError,
    goal_song_admin_snapshot,
    set_fallback_goal_songs,
)
from apps.team.services.goal_song_reads import (
    song_entries_for_ids,
)
from apps.team.services.goal_songs import delete_team_player_song, delete_team_song

from .serializers import (
    TeamClipLibraryAddSerializer,
    TeamClipLibraryQuerySerializer,
    TeamClipLibrarySerializer,
)


_PLAYER_ID_PARAMETER = OpenApiParameter(
    "player_id", OpenApiTypes.UUID, OpenApiParameter.PATH
)
_SONG_ID_PARAMETER = OpenApiParameter(
    "song_id", OpenApiTypes.UUID, OpenApiParameter.PATH
)


@extend_schema_view(
    update_player_goal_song_selection=extend_schema(parameters=[_PLAYER_ID_PARAMETER]),
    remove_player_song=extend_schema(
        parameters=[_PLAYER_ID_PARAMETER, _SONG_ID_PARAMETER]
    ),
    update_player_song_settings=extend_schema(
        parameters=[_PLAYER_ID_PARAMETER, _SONG_ID_PARAMETER]
    ),
)
class TeamGoalSongAdminActions(viewsets.GenericViewSet):
    """Moderate player and team goal songs for one team season."""

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
        team, season = self._goal_song_admin_context(request)
        snapshot = goal_song_admin_snapshot(team, season)
        return Response({
            "team": {
                "id_uuid": str(team.id_uuid),
                "name": team.name,
            },
            "season": {
                "id_uuid": str(season.id_uuid) if season is not None else None,
                "name": season.name if season is not None else None,
            },
            "fallback_goal_song_song_ids": snapshot.fallback_ids,
            "fallback_goal_song_songs": snapshot.fallback_songs,
            "team_songs": PlayerSongSerializer(snapshot.team_songs, many=True).data,
            "players": [
                {
                    "id_uuid": str(row.player.id_uuid),
                    "username": row.player.display_name,
                    "display_name": row.player.display_name,
                    "goal_song_song_ids": row.selected_ids,
                    "goal_song_songs": row.selected,
                    "songs": PlayerSongSerializer(row.songs, many=True).data,
                }
                for row in snapshot.players
            ],
        })

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
        team, season = self._goal_song_admin_context(request)
        ids = self._parse_song_id_list_from_payload(
            payload=request.data,
            field_name="fallback_goal_song_song_ids",
        )
        try:
            entries = set_fallback_goal_songs(team, season, ids)
        except MissingTeamSeasonError as exc:
            raise ValidationError({
                "detail": "No TeamData found for this season."
            }) from exc
        except GoalSongSelectionError as exc:
            _reject_selection(exc)
        return Response({
            "fallback_goal_song_song_ids": ids,
            "fallback_goal_song_songs": entries,
        })

    @extend_schema(
        request=PlayerSongCreateSerializer,
        responses={200: PlayerSongSerializer, 201: PlayerSongSerializer},
    )
    @action(
        detail=True,
        methods=("POST",),
        url_path="goal-song-admin/songs",
        permission_classes=[permissions.IsAuthenticated],
        parser_classes=[JSONParser, FormParser, MultiPartParser],
    )
    def create_goal_song(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        """Upload or import a song owned by this team and season."""
        team_data = self._goal_song_team_owner(request)
        serializer = PlayerSongCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        uploaded = serializer.validated_data.get("audio_file")
        creation = create_team_song(
            team_data=team_data,
            uploaded_audio=uploaded if isinstance(uploaded, UploadedFile) else None,
            source_url=str(
                serializer.validated_data.get("source_url")
                or serializer.validated_data.get("spotify_url")
                or ""
            ),
        )
        return Response(
            PlayerSongSerializer(creation.song).data,
            status=201 if creation.created else 200,
        )

    @extend_schema(
        methods=["PATCH"],
        request=PlayerSongUpdateSerializer,
        responses=PlayerSongSerializer,
        parameters=[_SONG_ID_PARAMETER],
    )
    @extend_schema(
        methods=["DELETE"],
        request=None,
        responses={204: None},
        parameters=[_SONG_ID_PARAMETER],
    )
    @action(
        detail=True,
        methods=("PATCH", "DELETE"),
        url_path=rf"goal-song-admin/songs/(?P<song_id>{UUID_URL_REGEX})",
        permission_classes=[permissions.IsAuthenticated],
    )
    def manage_goal_song(
        self, request: Request, song_id: str, *args: Any, **kwargs: Any
    ) -> Response:
        """Edit or remove a song only from the authorized team's library.

        Raises:
            NotFound: The song is not owned by this team and season.
            ValidationError: The clip settings exceed the source bounds.

        """
        team_data = self._goal_song_team_owner(request)
        try:
            if request.method == "DELETE":
                delete_team_song(team_data=team_data, song_id=song_id)
                return Response(status=204)
            serializer = PlayerSongUpdateSerializer(data=request.data)
            serializer.is_valid(raise_exception=True)
            song = update_team_song(
                team_data=team_data,
                song_id=song_id,
                settings=PlayerSongSettingsPatch(**serializer.validated_data),
            )
        except PlayerSongNotFoundError as exc:
            raise NotFound("Song not found") from exc
        except InvalidSongClipError as exc:
            raise ValidationError({"detail": str(exc)}) from exc
        return Response(PlayerSongSerializer(song).data)

    @extend_schema(
        request=PlayerSongClipCreateSerializer,
        responses={201: PlayerSongSerializer},
        parameters=[_SONG_ID_PARAMETER],
    )
    @action(
        detail=True,
        methods=("POST",),
        url_path=rf"goal-song-admin/songs/(?P<song_id>{UUID_URL_REGEX})/clips",
        permission_classes=[permissions.IsAuthenticated],
    )
    def create_goal_song_clip(
        self, request: Request, song_id: str, *args: Any, **kwargs: Any
    ) -> Response:
        """Create another clip within the authorized team and season.

        Raises:
            NotFound: The source belongs to a different owner.
            ValidationError: The source or clip settings are invalid.

        """
        owner = self._goal_song_team_owner(request)
        serializer = PlayerSongClipCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            song = create_song_clip(
                owner=owner,
                song_id=song_id,
                settings=PlayerSongSettingsPatch(**serializer.validated_data),
            )
        except PlayerSongNotFoundError as exc:
            raise NotFound("Song not found") from exc
        except InvalidSongClipError as exc:
            raise ValidationError({"detail": str(exc)}) from exc
        return Response(PlayerSongSerializer(song).data, status=201)

    @extend_schema(
        request=PlayerSongClipCreateSerializer,
        responses={201: PlayerSongSerializer},
        parameters=[_PLAYER_ID_PARAMETER, _SONG_ID_PARAMETER],
    )
    @action(
        detail=True,
        methods=("POST",),
        url_path=rf"goal-song-admin/player/(?P<player_id>{UUID_URL_REGEX})/songs/(?P<song_id>{UUID_URL_REGEX})/clips",
        permission_classes=[permissions.IsAuthenticated],
    )
    def create_player_song_clip(
        self, request: Request, player_id: str, song_id: str, *args: Any, **kwargs: Any
    ) -> Response:
        """Create a clip only for an authorized roster player's own audio.

        Raises:
            NotFound: The source belongs to a different owner.
            ValidationError: The source or clip settings are invalid.

        """
        team, season = self._goal_song_admin_context(request)
        owner = self._goal_song_roster_player(team, season, player_id)
        serializer = PlayerSongClipCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            song = create_song_clip(
                owner=owner,
                song_id=song_id,
                settings=PlayerSongSettingsPatch(**serializer.validated_data),
            )
        except PlayerSongNotFoundError as exc:
            raise NotFound("Song not found") from exc
        except InvalidSongClipError as exc:
            raise ValidationError({"detail": str(exc)}) from exc
        return Response(PlayerSongSerializer(song).data, status=201)

    @extend_schema(
        request=None, responses=PlayerSongSerializer, parameters=[_SONG_ID_PARAMETER]
    )
    @action(
        detail=True,
        methods=("POST",),
        url_path=rf"goal-song-admin/songs/(?P<song_id>{UUID_URL_REGEX})/retry",
        permission_classes=[permissions.IsAuthenticated],
    )
    def retry_goal_song(
        self, request: Request, song_id: str, *args: Any, **kwargs: Any
    ) -> Response:
        """Retry a team import using the durable media worker.

        Raises:
            NotFound: The song is not owned by this team and season.

        """
        team_data = self._goal_song_team_owner(request)
        try:
            song = retry_team_song(team_data=team_data, song_id=song_id)
        except PlayerSongNotFoundError as exc:
            raise NotFound("Song not found") from exc
        except PlayerSongAlreadyReadyError:
            return Response(
                {"detail": "Song is already ready.", "code": "song_already_ready"},
                status=status.HTTP_409_CONFLICT,
            )
        return Response(PlayerSongSerializer(song).data)

    def _goal_song_team_owner(self, request: Request) -> TeamData:
        team, season = self._goal_song_admin_context(request)
        team_data = team_data_for_season(team=team, season=season)
        if team_data is None:
            raise ValidationError({"detail": "No TeamData found for this season."})
        return team_data

    @extend_schema(
        parameters=[TeamClipLibraryQuerySerializer],
        responses=TeamClipLibrarySerializer(many=True),
    )
    @action(
        detail=True,
        methods=("GET",),
        url_path="goal-song-admin/library",
        permission_classes=[permissions.IsAuthenticated],
        filter_backends=[],
    )
    def goal_song_library(
        self, request: Request, *args: Any, **kwargs: Any
    ) -> Response:
        """Browse ready clips from other teams for an authorized recipient."""
        owner = self._goal_song_team_owner(request)
        query = TeamClipLibraryQuerySerializer(data=request.query_params)
        query.is_valid(raise_exception=True)
        songs = search_team_clips(owner=owner, search=query.validated_data["search"])
        paginator = StandardResultsSetPagination()
        paginator.page_size = 20
        page = paginator.paginate_queryset(songs, request, view=self)
        return paginator.get_paginated_response(
            TeamClipLibrarySerializer(page, many=True).data
        )

    @extend_schema(
        request=TeamClipLibraryAddSerializer,
        responses={201: PlayerSongSerializer, 200: PlayerSongSerializer},
    )
    @action(
        detail=True,
        methods=("POST",),
        url_path="goal-song-admin/library/add",
        permission_classes=[permissions.IsAuthenticated],
        filter_backends=[],
    )
    def add_goal_song_from_library(
        self, request: Request, *args: Any, **kwargs: Any
    ) -> Response:
        """Import a shared clip without granting write access to its source team.

        Raises:
            NotFound: The source is not a ready shared team clip.
            ValidationError: The clip or stored upload is invalid.

        """
        owner = self._goal_song_team_owner(request)
        serializer = TeamClipLibraryAddSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            result = add_team_library_clip(
                owner=owner, source_id=str(serializer.validated_data["source_id"])
            )
        except PlayerSongNotFoundError as exc:
            raise NotFound("Clip not found in the team library.") from exc
        except (InvalidSongClipError, InvalidAudioUploadError) as exc:
            raise ValidationError({"detail": str(exc)}) from exc
        return Response(
            PlayerSongSerializer(result.song).data,
            status=201 if result.created else 200,
        )

    @action(
        detail=True,
        methods=("PATCH",),
        url_path=rf"goal-song-admin/player/(?P<player_id>{UUID_URL_REGEX})",
        permission_classes=[permissions.IsAuthenticated],
    )
    def update_player_goal_song_selection(
        self,
        request: Request,
        player_id: str,
        *args: Any,
        **kwargs: Any,
    ) -> Response:
        """Update goal-song selection for a player in the authorized team's roster."""
        team, season = self._goal_song_admin_context(request)

        ids = self._parse_song_id_list_from_payload(
            payload=request.data,
            field_name="goal_song_song_ids",
        )
        player = self._goal_song_roster_player(
            team, season, player_id, not_found="Player not found"
        )

        try:
            songs = validate_goal_song_ids(player=player, ids=ids)
        except GoalSongSelectionError as exc:
            _reject_selection(exc)
        player.save(
            update_fields=apply_goal_song_selection(
                player=player, ids=ids, ordered=songs
            )
        )

        return Response({
            "player_id": str(player.id_uuid),
            "goal_song_song_ids": ids,
            "goal_song_songs": song_entries_for_ids(songs=songs, ids=ids),
        })

    @action(
        detail=True,
        methods=("DELETE",),
        url_path=(
            rf"goal-song-admin/player/(?P<player_id>{UUID_URL_REGEX})/"
            rf"songs/(?P<song_id>{UUID_URL_REGEX})"
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
        team, season = self._goal_song_admin_context(request)

        player = self._goal_song_roster_player(team, season, player_id)

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
            rf"goal-song-admin/player/(?P<player_id>{UUID_URL_REGEX})/"
            rf"songs/(?P<song_id>{UUID_URL_REGEX})/settings"
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
        """Update clip settings from team moderation.

        Raises:
            ValidationError: The clip settings exceed the source bounds.

        """
        team, season = self._goal_song_admin_context(request)

        player = self._goal_song_roster_player(team, season, player_id)
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
                settings=PlayerSongSettingsPatch(**serializer.validated_data),
            )
        except InvalidSongClipError as exc:
            raise ValidationError({"detail": str(exc)}) from exc
        except PlayerSongNotFoundError:
            return Response(
                {"detail": "Song not found"},
                status=status.HTTP_404_NOT_FOUND,
            )

        return Response(PlayerSongSerializer(song).data)

    def _goal_song_admin_context(self, request: Request) -> tuple[Team, Season | None]:
        """Resolve the requested season and require moderation access.

        Raises:
            NotFound: An explicitly requested season is not available.
            PermissionDenied: The viewer cannot manage this team's songs.

        """
        team = self.get_object()
        season = resolve_team_season(
            request.query_params.get("season"), list(team_seasons(team))
        )
        requested_season = (request.query_params.get("season") or "").strip()
        if requested_season and (season is None or str(season.pk) != requested_season):
            raise NotFound(detail="Season not found for this team.")
        if not viewer_can_manage_team(request=request, team=team, season=season):
            raise PermissionDenied(
                detail="You do not have permission to manage team goal songs."
            )
        return team, season

    @staticmethod
    def _goal_song_roster_player(
        team: Team,
        season: Season | None,
        player_id: str,
        *,
        not_found: str = "Song not found",
    ) -> Player:
        """Resolve a player only after verifying the season's main roster.

        Raises:
            ValidationError: The player is outside this team's roster.
            NotFound: The roster player no longer exists.

        """
        if player_id not in main_roster_ids(team=team, season=season):
            raise ValidationError({"detail": "Player is not in this team roster."})
        player = Player.objects.select_related("user").filter(id_uuid=player_id).first()
        if player is None:
            raise NotFound(detail=not_found)
        return player

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
            if song_id:
                song_id = str(uuid_query_value(song_id, parameter=field_name))
            if not song_id or song_id in seen:
                continue
            seen.add(song_id)
            ids.append(song_id)
        return ids


def _reject_selection(exc: GoalSongSelectionError) -> NoReturn:
    """Expose which selected songs are unknown or still processing.

    Raises:
        ValidationError: Always, carrying the offending song ids.

    """
    detail: dict[str, object] = {"detail": exc.detail}
    if exc.missing is not None:
        detail["missing"] = exc.missing
    if exc.not_ready is not None:
        detail["not_ready"] = exc.not_ready
    raise ValidationError(detail) from exc
