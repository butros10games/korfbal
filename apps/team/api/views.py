"""ViewSets for team-related API endpoints."""

from __future__ import annotations

from typing import Any, ClassVar

from asgiref.sync import async_to_sync
from django.db.models import Q, QuerySet
from django.utils import timezone
from rest_framework import filters, permissions, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import NotFound, PermissionDenied, ValidationError
from rest_framework.request import Request
from rest_framework.response import Response

from apps.game_tracker.models import (
    MatchData,
    MatchPlayer,
    PlayerMatchImpact,
    PlayerMatchImpactBreakdown,
    Shot,
)
from apps.game_tracker.services.match_impact import (
    LATEST_MATCH_IMPACT_ALGORITHM_VERSION,
    persist_match_impact_rows_with_breakdowns,
    round_js_1dp,
)
from apps.kwt_common.api.pagination import StandardResultsSetPagination
from apps.kwt_common.api.permissions import IsStaffOrReadOnly
from apps.kwt_common.utils.general_stats import build_general_stats
from apps.kwt_common.utils.match_summary import build_match_summaries
from apps.kwt_common.utils.players_stats import build_player_stats
from apps.player.api.serializers import PlayerSongSerializer, PlayerSongUpdateSerializer
from apps.player.models import Player
from apps.player.models.player_song import PlayerSong, PlayerSongStatus
from apps.player.privacy import can_view_by_visibility
from apps.schedule.models import Season
from apps.team.models.team import Team
from apps.team.models.team_data import TeamData

from .serializers import TeamSerializer


class TeamViewSet(viewsets.ModelViewSet):
    """Expose team CRUD endpoints with lightweight search support."""

    queryset = Team.objects.select_related("club").order_by("club__name", "name")
    serializer_class = TeamSerializer
    pagination_class = StandardResultsSetPagination
    permission_classes: ClassVar[list[type[permissions.BasePermission]]] = [
        IsStaffOrReadOnly,
    ]
    lookup_field = "id_uuid"
    filter_backends: ClassVar[list[type[filters.BaseFilterBackend]]] = [
        filters.SearchFilter
    ]
    search_fields: ClassVar[list[str]] = ["name", "club__name"]

    @action(detail=True, methods=("GET",), url_path="overview")
    def overview(
        self,
        request: Request,
        *args: Any,  # noqa: ANN401
        **kwargs: Any,  # noqa: ANN401
    ) -> Response:
        """Return match summaries, stats, roster data, and season options.

        Returns:
            Response: Aggregated team overview data.

        """
        team = self.get_object()
        seasons_qs = list(self._team_seasons_queryset(team))
        season = self._resolve_season(request, seasons_qs)

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

        match_data_qs = self._team_match_queryset(team, season)
        upcoming_matches = build_match_summaries(
            match_data_qs.filter(status__in=["upcoming", "active"]).order_by(
                "match_link__start_time"
            )[:10]
        )
        recent_matches = build_match_summaries(
            match_data_qs.filter(status="finished").order_by("-match_link__start_time")[
                :10
            ]
        )

        stats_general = None
        if include_stats and match_data_qs.exists():
            stats_general = async_to_sync(build_general_stats)(match_data_qs)

        roster_players: list[Player] = []
        if include_roster or include_stats:
            roster_players = list(
                self._team_players_queryset(team, season, match_data_qs)
            )

        # Mark which players are part of the team's main season roster (TeamData)
        # vs. reserve/guest players who only show up in matches/stats.
        main_roster_ids = self._main_roster_ids(team=team, season=season)
        ordered_roster_players = self._order_roster_players(
            roster_players=roster_players,
            main_roster_ids=main_roster_ids,
        )

        viewer_player = (
            Player.objects.filter(user=request.user).first()
            if request.user.is_authenticated
            else None
        )

        roster: list[dict[str, str]] = []
        if include_roster:
            roster = [
                {
                    "id_uuid": str(player.id_uuid),
                    # Avoid exposing full names to anonymous/outside viewers.
                    "display_name": player.user.username,
                    "username": player.user.username,
                    "roster_role": (
                        "main" if str(player.id_uuid) in main_roster_ids else "reserve"
                    ),
                    "profile_picture_url": (
                        player.get_profile_picture()
                        if can_view_by_visibility(
                            visibility=player.profile_picture_visibility,
                            viewer=viewer_player,
                            target=player,
                        )
                        else player.get_placeholder_profile_picture_url()
                    ),
                    "profile_url": player.get_absolute_url(),
                }
                for player in ordered_roster_players
            ]

        stats_players = []
        if include_stats and roster_players and match_data_qs.exists():
            stats_players = async_to_sync(build_player_stats)(
                roster_players, match_data_qs
            )

        current_season = self._current_season()
        seasons_payload = [
            {
                "id_uuid": str(option.id_uuid),
                "name": option.name,
                "start_date": option.start_date.isoformat(),
                "end_date": option.end_date.isoformat(),
                "is_current": current_season is not None
                and option.id_uuid == current_season.id_uuid,
            }
            for option in seasons_qs
        ]

        payload = {
            "team": self.get_serializer(team).data,
            "matches": {
                "upcoming": upcoming_matches,
                "recent": recent_matches,
            },
            "stats": {
                "general": stats_general,
                "players": stats_players,
            },
            "roster": roster,
            "seasons": seasons_payload,
            "meta": {
                "season_id": str(season.id_uuid) if season else None,
                "season_name": season.name if season else None,
                "roster_count": len(roster),
                "viewer_can_manage_goal_songs": self._viewer_can_manage_goal_songs(
                    request=request,
                    team=team,
                    season=season,
                ),
                "fallback_goal_song_audio_urls": self._fallback_goal_song_audio_urls(
                    team=team,
                    season=season,
                ),
            },
        }
        return Response(payload)

    @action(
        detail=True,
        methods=("GET",),
        url_path="impact-breakdown",
    )
    def impact_breakdown(
        self,
        request: Request,
        *args: Any,  # noqa: ANN401
        **kwargs: Any,  # noqa: ANN401
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
        seasons_qs = list(self._team_seasons_queryset(team))
        season = self._resolve_season(request, seasons_qs)

        player_param = (request.query_params.get("player") or "").strip()
        if not player_param:
            return Response(
                {"detail": "Missing required query param: player"},
                status=400,
            )

        player = (
            Player.objects
            .select_related("user")
            .only("id_uuid", "user__username")
            .filter(id_uuid=player_param)
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
        *args: Any,  # noqa: ANN401
        **kwargs: Any,  # noqa: ANN401
    ) -> Response:
        """Return team player songs and fallback song configuration for moderation."""
        team = self.get_object()
        seasons_qs = list(self._team_seasons_queryset(team))
        season = self._resolve_season(request, seasons_qs)

        self._ensure_goal_song_admin_access(
            request=request,
            team=team,
            season=season,
        )

        match_data_qs = self._team_match_queryset(team, season)
        players = list(self._team_players_queryset(team, season, match_data_qs))
        songs = list(
            PlayerSong.objects
            .select_related("cached_song", "player", "player__user")
            .filter(player__in=players)
            .order_by("-created_at")
        )

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
        *args: Any,  # noqa: ANN401
        **kwargs: Any,  # noqa: ANN401
    ) -> Response:
        """Update the team fallback playlist used when a scorer has no own goal song.

        Raises:
          ValidationError: If payload/song ids are invalid or no season TeamData exists.

        """
        team = self.get_object()
        seasons_qs = list(self._team_seasons_queryset(team))
        season = self._resolve_season(request, seasons_qs)

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
            songs_qs=PlayerSong.objects.select_related("cached_song").filter(
                id_uuid__in=ids,
                player_id__in=roster_player_ids,
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
        *args: Any,  # noqa: ANN401
        **kwargs: Any,  # noqa: ANN401
    ) -> Response:
        """Update goal-song selection for a team player.

        Raises:
            NotFound: If the referenced player cannot be found.
            ValidationError: If payload/song ids are invalid or player is not in roster.

        """
        team = self.get_object()
        seasons_qs = list(self._team_seasons_queryset(team))
        season = self._resolve_season(request, seasons_qs)

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
            songs_qs=PlayerSong.objects.select_related("cached_song").filter(
                player=player,
                id_uuid__in=ids,
            ),
        )
        by_id = {str(song.id_uuid): song for song in songs}

        player.goal_song_song_ids = ids
        update_fields: list[str] = ["goal_song_song_ids"]
        if ids:
            first = by_id[ids[0]]
            audio_file = (
                first.cached_song.audio_file
                if first.cached_song is not None
                else first.audio_file
            )
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
        *args: Any,  # noqa: ANN401
        **kwargs: Any,  # noqa: ANN401
    ) -> Response:
        """Delete a player song from the team moderation view."""
        team = self.get_object()
        seasons_qs = list(self._team_seasons_queryset(team))
        season = self._resolve_season(request, seasons_qs)

        if not self._viewer_can_manage_goal_songs(
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

        song = (
            PlayerSong.objects
            .select_related("player")
            .filter(id_uuid=song_id, player_id=player_id)
            .first()
        )
        if song is None:
            return Response(
                {"detail": "Song not found"},
                status=status.HTTP_404_NOT_FOUND,
            )

        player = song.player
        selected_ids = [sid for sid in (player.goal_song_song_ids or []) if sid]
        fallback_ids = self._fallback_goal_song_song_ids(team=team, season=season)

        if str(song.id_uuid) in selected_ids:
            next_selected_ids = [
                sid for sid in selected_ids if sid != str(song.id_uuid)
            ]
            player.goal_song_song_ids = next_selected_ids
            update_fields: list[str] = ["goal_song_song_ids"]
            if next_selected_ids:
                first = (
                    PlayerSong.objects
                    .select_related("cached_song")
                    .filter(player=player, id_uuid=next_selected_ids[0])
                    .first()
                )
                audio_file = None
                if first is not None:
                    audio_file = (
                        first.cached_song.audio_file
                        if first.cached_song is not None
                        else first.audio_file
                    )
                player.goal_song_uri = audio_file.url if audio_file else ""
                player.song_start_time = first.start_time_seconds if first else None
            else:
                player.goal_song_uri = ""
                player.song_start_time = None
            update_fields.extend(["goal_song_uri", "song_start_time"])
            player.save(update_fields=update_fields)

        if str(song.id_uuid) in fallback_ids:
            next_fallback_ids = [
                sid for sid in fallback_ids if sid != str(song.id_uuid)
            ]
            team_data = self._team_data_for_season(team=team, season=season)
            if team_data is not None:
                team_data.fallback_goal_song_song_ids = next_fallback_ids
                team_data.save(update_fields=["fallback_goal_song_song_ids"])

        song.delete()
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
        *args: Any,  # noqa: ANN401
        **kwargs: Any,  # noqa: ANN401
    ) -> Response:
        """Update song timing/speed for a player song from team moderation."""
        team = self.get_object()
        seasons_qs = list(self._team_seasons_queryset(team))
        season = self._resolve_season(request, seasons_qs)

        if not self._viewer_can_manage_goal_songs(
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

        song = (
            PlayerSong.objects
            .select_related("player")
            .filter(id_uuid=song_id, player_id=player_id)
            .first()
        )
        if song is None:
            return Response(
                {"detail": "Song not found"},
                status=status.HTTP_404_NOT_FOUND,
            )

        serializer = PlayerSongUpdateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        update_fields: list[str] = ["updated_at"]
        if "start_time_seconds" in serializer.validated_data:
            song.start_time_seconds = int(
                serializer.validated_data["start_time_seconds"]
            )
            update_fields.append("start_time_seconds")
        if "playback_speed" in serializer.validated_data:
            song.playback_speed = float(serializer.validated_data["playback_speed"])
            update_fields.append("playback_speed")
        song.save(update_fields=update_fields)

        player = song.player
        current_selected = [sid for sid in (player.goal_song_song_ids or []) if sid]
        if current_selected and current_selected[0] == str(song.id_uuid):
            player.song_start_time = song.start_time_seconds
            player.save(update_fields=["song_start_time"])

        return Response(PlayerSongSerializer(song).data)

    def _ensure_goal_song_admin_access(
        self,
        *,
        request: Request,
        team: Team,
        season: Season | None,
    ) -> None:
        if self._viewer_can_manage_goal_songs(
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
        payload: Any,  # noqa: ANN401
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
        match_data_qs = self._team_match_queryset(team, season).filter(
            status="finished"
        )

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

    def _viewer_can_manage_goal_songs(
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
        cached = song.cached_song
        status_value = cached.status if cached is not None else song.status
        audio_file = cached.audio_file if cached is not None else song.audio_file
        if status_value != PlayerSongStatus.READY or not audio_file:
            return None
        return {
            "id_uuid": str(song.id_uuid),
            "audio_url": audio_file.url,
            "start_time_seconds": int(song.start_time_seconds or 0),
            "playback_speed": float(song.playback_speed or 1.0),
            "title": song.title,
            "artists": song.artists,
            "player_id": str(song.player.id_uuid),
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
            PlayerSong.objects.select_related("cached_song").filter(
                id_uuid__in=ids,
                player_id__in=roster_player_ids,
            )
        )
        entries = self._song_entries_for_ids(songs=songs, ids=ids)
        audio_urls: list[str] = []
        for entry in entries:
            audio_url = entry.get("audio_url")
            if isinstance(audio_url, str):
                audio_urls.append(audio_url)
        return audio_urls

    @staticmethod
    def _main_roster_ids(*, team: Team, season: Season | None) -> set[str]:
        team_data_qs = TeamData.objects.filter(team=team)
        if season is not None:
            team_data_qs = team_data_qs.filter(season=season)

        return {
            str(player_id)
            for player_id in team_data_qs
            .values_list("players__id_uuid", flat=True)
            .distinct()
            .exclude(players__id_uuid__isnull=True)
        }

    @staticmethod
    def _order_roster_players(
        *,
        roster_players: list[Player],
        main_roster_ids: set[str],
    ) -> list[Player]:
        main_roster_players = [
            player
            for player in roster_players
            if str(player.id_uuid) in main_roster_ids
        ]
        reserve_roster_players = [
            player
            for player in roster_players
            if str(player.id_uuid) not in main_roster_ids
        ]

        # Keep username ordering within each section.
        main_roster_players.sort(key=lambda p: p.user.username.lower())
        reserve_roster_players.sort(key=lambda p: p.user.username.lower())
        return [*main_roster_players, *reserve_roster_players]

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

    def _team_match_queryset(
        self, team: Team, season: Season | None
    ) -> QuerySet[MatchData]:
        queryset = MatchData.objects.select_related(
            "match_link",
            "match_link__home_team",
            "match_link__home_team__club",
            "match_link__away_team",
            "match_link__away_team__club",
            "match_link__season",
        ).filter(
            Q(match_link__home_team=team) | Q(match_link__away_team=team),
        )
        if season:
            queryset = queryset.filter(match_link__season=season)
        return queryset

    def _resolve_season(self, request: Request, seasons: list[Season]) -> Season | None:
        """Resolve the requested season in a safe, team-scoped way.

        Important:
            If a `season` query param is supplied but cannot be resolved, we do
            **not** return `None` (which would broaden queries to all seasons).
            Instead, we fall back to a sensible default within the provided
            season list.

        """
        season_param = request.query_params.get("season")
        if season_param:
            selected = next(
                (option for option in seasons if str(option.id_uuid) == season_param),
                None,
            )
            if selected is not None:
                return selected

        if not seasons:
            return self._current_season() or self._most_recent_season()

        current = self._current_season()
        if current and any(option.id_uuid == current.id_uuid for option in seasons):
            return current

        return seasons[0]

    def _current_season(self) -> Season | None:
        today = timezone.now().date()
        return Season.objects.filter(
            start_date__lte=today,
            end_date__gte=today,
        ).first()

    def _most_recent_season(self) -> Season | None:
        today = timezone.now().date()
        return Season.objects.filter(end_date__lte=today).order_by("-end_date").first()

    def _team_players_queryset(
        self, team: Team, season: Season | None, match_data_qs: QuerySet[MatchData]
    ) -> QuerySet[Player]:
        # IMPORTANT:
        # Avoid OR-of-joins filtering here.
        # The old approach produced large LEFT JOIN chains + DISTINCT and can
        # easily degrade into multi-second queries on real datasets.
        #
        # Instead, build candidate player ids from each source table and UNION
        # them. Postgres can satisfy these subqueries using indexes, and the
        # final Player query becomes a simple IN (subquery).
        teamdata_qs = TeamData.objects.filter(team=team)
        if season is not None:
            teamdata_qs = teamdata_qs.filter(season=season)

        # Pull roster ids from the M2M join table directly to avoid LEFT JOIN +
        # DISTINCT patterns.
        teamdata_player_ids = TeamData.players.through.objects.filter(
            teamdata_id__in=teamdata_qs.values_list("id", flat=True)
        ).values_list("player_id", flat=True)

        # Materialize match ids once so we don't embed the same match subquery
        # multiple times (once for MatchPlayer, once for Shot).
        match_ids = list(match_data_qs.values_list("id_uuid", flat=True))

        all_player_ids = teamdata_player_ids
        if match_ids:
            match_player_ids = MatchPlayer.objects.filter(
                team=team,
                match_data_id__in=match_ids,
            ).values_list("player_id", flat=True)
            shot_player_ids = Shot.objects.filter(
                team=team,
                match_data_id__in=match_ids,
            ).values_list("player_id", flat=True)
            all_player_ids = all_player_ids.union(match_player_ids, shot_player_ids)

        return (
            Player.objects
            .select_related("user")
            .only(
                "id_uuid",
                "profile_picture",
                "profile_picture_visibility",
                "stats_visibility",
                "goal_song_uri",
                "song_start_time",
                "goal_song_song_ids",
                "user__username",
            )
            .filter(id_uuid__in=all_player_ids)
            .order_by("user__username")
        )

    def _team_seasons_queryset(self, team: Team) -> QuerySet[Season]:
        return (
            Season.objects
            .filter(
                Q(team_data__team=team)
                | Q(matches__home_team=team)
                | Q(matches__away_team=team)
            )
            .distinct()
            .order_by("-start_date")
        )
