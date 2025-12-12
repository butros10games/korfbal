"""Views for schedule endpoints."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, ClassVar

from django.conf import settings
from django.db.models import Count, Q, QuerySet
from django.utils import timezone
from rest_framework import permissions, status, viewsets
from rest_framework.decorators import action
from rest_framework.request import Request
from rest_framework.response import Response

from apps.game_tracker.models import (
    GoalType,
    MatchData,
    MatchPart,
    MatchPlayer,
    Pause,
    PlayerChange,
    Shot,
    Timeout,
)
from apps.kwt_common.utils.match_summary import build_match_summaries
from apps.player.models.player import Player
from apps.schedule.models import Match
from apps.team.models.team import Team

from .permissions import IsCoachOrAdmin
from .serializers import (
    MatchSerializer,
    PauseWriteSerializer,
    PlayerChangeWriteSerializer,
    ShotWriteSerializer,
    TimeoutWriteSerializer,
)


MATCH_TRACKER_DATA_NOT_FOUND = "Match tracker data not found."


class MatchViewSet(viewsets.ReadOnlyModelViewSet):
    """Expose match data for the mobile frontend."""

    serializer_class = MatchSerializer
    permission_classes: ClassVar[list[type[permissions.BasePermission]]] = [
        permissions.AllowAny,
    ]

    def get_queryset(self) -> QuerySet[Match]:
        """Return a queryset filtered by the current request context.

        Returns:
            QuerySet[Match]: Filtered match queryset.

        """
        queryset = Match.objects.select_related(
            "home_team__club",
            "away_team__club",
            "season",
        ).order_by("start_time")

        team_ids = self.request.query_params.getlist("team")
        club_ids = self.request.query_params.getlist("club")
        season_id = self.request.query_params.get("season")

        if not team_ids and self.request.query_params.get("followed"):
            player = self._get_player()
            if player:
                team_ids = list(player.team_follow.values_list("id_uuid", flat=True))

        if team_ids:
            queryset = queryset.filter(
                Q(home_team__id_uuid__in=team_ids) | Q(away_team__id_uuid__in=team_ids)
            )

        if club_ids:
            queryset = queryset.filter(
                Q(home_team__club__id_uuid__in=club_ids)
                | Q(away_team__club__id_uuid__in=club_ids)
            )

        if season_id:
            queryset = queryset.filter(season__id_uuid=season_id)

        return queryset

    def _get_player(self) -> Player | None:
        """Return the authenticated player (or debug override).

        Returns:
            Player | None: The player instance or None.

        """
        if self.request.user.is_authenticated:
            try:
                return Player.objects.prefetch_related("team_follow").get(
                    user=self.request.user
                )
            except Player.DoesNotExist:
                return None

        if settings.DEBUG:
            player_id = self.request.query_params.get("player_id")
            if player_id:
                return (
                    Player.objects.prefetch_related("team_follow")
                    .filter(
                        id_uuid=player_id,
                    )
                    .first()
                )
        return None

    def _upcoming_queryset(self) -> QuerySet[Match]:
        """Return upcoming matches ordered by start time.

        Returns:
            QuerySet[Match]: Upcoming matches.

        """
        now = timezone.now()
        return self.get_queryset().filter(start_time__gte=now).order_by("start_time")

    @action(detail=False, methods=["GET"], url_path="next")  # type: ignore[arg-type]
    def next_match(
        self,
        request: Request,
        *args: Any,  # noqa: ANN401
        **kwargs: Any,  # noqa: ANN401
    ) -> Response:
        """Return the next upcoming match for the active context.

        Returns:
            Response: Serialized next match.

        """
        match = self._upcoming_queryset().first()
        if not match:
            return Response(None, status=status.HTTP_200_OK)
        serializer = self.get_serializer(match)
        return Response(serializer.data)

    @action(detail=False, methods=["GET"], url_path="upcoming")  # type: ignore[arg-type]
    def upcoming(
        self,
        request: Request,
        *args: Any,  # noqa: ANN401
        **kwargs: Any,  # noqa: ANN401
    ) -> Response:
        """Return a limited list of upcoming matches.

        Returns:
            Response: Serialized list of upcoming matches.

        """
        limit_param = request.query_params.get("limit")
        try:
            limit = int(limit_param) if limit_param else 5
        except ValueError:
            limit = 5

        queryset = self._upcoming_queryset()[: max(limit, 1)]
        serializer = self.get_serializer(queryset, many=True)
        return Response(serializer.data)

    @action(detail=False, methods=["GET"], url_path="recent")  # type: ignore[arg-type]
    def recent(
        self,
        request: Request,
        *args: Any,  # noqa: ANN401
        **kwargs: Any,  # noqa: ANN401
    ) -> Response:
        """Return matches played within the recent window.

        Returns:
            Response: Serialized list of recent matches.

        """
        window = timezone.now() - timedelta(days=7)
        queryset = (
            self.get_queryset()
            .filter(start_time__gte=window)
            .order_by("-start_time")[:5]
        )
        serializer = self.get_serializer(queryset, many=True)
        return Response(serializer.data)

    @action(detail=True, methods=["GET"], url_path="summary")  # type: ignore[arg-type]
    def summary(
        self,
        request: Request,
        *args: Any,  # noqa: ANN401
        **kwargs: Any,  # noqa: ANN401
    ) -> Response:
        """Return a match summary payload for a single match.

        This is used by the korfbal-web Match page hero header to show
        score/status/time/parts in the same layout as other match elements.

        Returns:
            Response: Match summary dictionary or None.

        """
        match: Match = self.get_object()
        match_data = MatchData.objects.filter(match_link=match).first()
        if not match_data:
            return Response(None, status=status.HTTP_200_OK)

        summary = build_match_summaries([match_data])[0]
        return Response(summary)

    @action(detail=True, methods=["GET"], url_path="stats")  # type: ignore[arg-type]
    def stats(
        self,
        request: Request,
        *args: Any,  # noqa: ANN401
        **kwargs: Any,  # noqa: ANN401
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
        match_data = MatchData.objects.filter(match_link=match).first()
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

        home_team = match.home_team
        away_team = match.away_team

        goal_types = list(GoalType.objects.all())
        goal_types_json = [
            {"id": str(goal_type.id_uuid), "name": goal_type.name}
            for goal_type in goal_types
        ]

        team_goal_stats: dict[str, dict[str, int]] = {}
        for goal_type in goal_types:
            goals_home = Shot.objects.filter(
                match_data=match_data,
                team=home_team,
                shot_type=goal_type,
                scored=True,
            ).count()
            goals_away = Shot.objects.filter(
                match_data=match_data,
                team=away_team,
                shot_type=goal_type,
                scored=True,
            ).count()
            team_goal_stats[goal_type.name] = {
                "goals_by_player": int(goals_home),
                "goals_against_player": int(goals_away),
            }

        general = {
            "shots_for": Shot.objects.filter(
                match_data=match_data,
                team=home_team,
            ).count(),
            "shots_against": Shot.objects.filter(
                match_data=match_data,
                team=away_team,
            ).count(),
            "goals_for": Shot.objects.filter(
                match_data=match_data,
                team=home_team,
                scored=True,
            ).count(),
            "goals_against": Shot.objects.filter(
                match_data=match_data,
                team=away_team,
                scored=True,
            ).count(),
            "team_goal_stats": team_goal_stats,
            "goal_types": goal_types_json,
        }

        def build_player_lines(
            *,
            player_ids: set[str],
            team: Team,
            other_team: Team,
        ) -> list[dict[str, object]]:
            if not player_ids:
                return []

            queryset = (
                Player.objects.select_related("user")
                .filter(id_uuid__in=player_ids)
                .annotate(
                    shots_for=Count(
                        "shots__id_uuid",
                        filter=Q(
                            shots__match_data=match_data,
                            shots__team=team,
                        ),
                    ),
                    shots_against=Count(
                        "shots__id_uuid",
                        filter=Q(
                            shots__match_data=match_data,
                            shots__team=other_team,
                        ),
                    ),
                    goals_for=Count(
                        "shots__id_uuid",
                        filter=Q(
                            shots__match_data=match_data,
                            shots__team=team,
                            shots__scored=True,
                        ),
                    ),
                    goals_against=Count(
                        "shots__id_uuid",
                        filter=Q(
                            shots__match_data=match_data,
                            shots__team=other_team,
                            shots__scored=True,
                        ),
                    ),
                )
                .order_by("-goals_for", "-shots_for", "user__username")
            )

            return [
                {
                    "id_uuid": str(player.id_uuid),
                    "display_name": player.user.get_full_name() or player.user.username,
                    "username": player.user.username,
                    "profile_picture_url": player.get_profile_picture(),
                    "profile_url": player.get_absolute_url(),
                    "shots_for": int(getattr(player, "shots_for", 0)),
                    "shots_against": int(getattr(player, "shots_against", 0)),
                    "goals_for": int(getattr(player, "goals_for", 0)),
                    "goals_against": int(getattr(player, "goals_against", 0)),
                }
                for player in queryset
            ]

        home_player_ids = set(
            MatchPlayer.objects.filter(match_data=match_data, team=home_team)
            .values_list("player__id_uuid", flat=True)
            .distinct()
        ) | set(
            Shot.objects.filter(match_data=match_data, team=home_team)
            .values_list("player__id_uuid", flat=True)
            .distinct()
        )

        away_player_ids = set(
            MatchPlayer.objects.filter(match_data=match_data, team=away_team)
            .values_list("player__id_uuid", flat=True)
            .distinct()
        ) | set(
            Shot.objects.filter(match_data=match_data, team=away_team)
            .values_list("player__id_uuid", flat=True)
            .distinct()
        )

        players_payload = {
            "home": build_player_lines(
                player_ids=home_player_ids,
                team=home_team,
                other_team=away_team,
            ),
            "away": build_player_lines(
                player_ids=away_player_ids,
                team=away_team,
                other_team=home_team,
            ),
        }

        return Response(
            {
                "general": general,
                "players": players_payload,
                "meta": {
                    "home_team_id": str(home_team.id_uuid),
                    "away_team_id": str(away_team.id_uuid),
                },
            },
            status=status.HTTP_200_OK,
        )

    @action(detail=True, methods=["GET"], url_path="events")  # type: ignore[arg-type]
    def events(
        self,
        request: Request,
        *args: Any,  # noqa: ANN401
        **kwargs: Any,  # noqa: ANN401
    ) -> Response:
        """Return match tracker events for a single match.

        This powers the korfbal-web Match page "Events" tab.

        Returns:
            Response: JSON payload with an ordered events list.

        """
        match: Match = self.get_object()
        match_data = MatchData.objects.filter(match_link=match).first()
        if not match_data or match_data.status == "upcoming":
            return Response(
                {
                    "home_team_id": str(match.home_team.id_uuid),
                    "events": [],
                    "status": match_data.status if match_data else "unknown",
                },
                status=status.HTTP_200_OK,
            )

        events_payload = _build_match_events(match_data)
        return Response(
            {
                "home_team_id": str(match.home_team.id_uuid),
                "events": events_payload,
                "status": match_data.status,
            },
            status=status.HTTP_200_OK,
        )

    @action(
        detail=True,
        methods=("GET",),
        url_path="events/can-edit",
        permission_classes=[permissions.AllowAny],
    )  # type: ignore[arg-type]
    def can_edit_events(
        self,
        request: Request,
        *args: Any,  # noqa: ANN401
        **kwargs: Any,  # noqa: ANN401
    ) -> Response:
        """Return whether the current user can edit match events."""
        return Response({"can_edit": IsCoachOrAdmin().has_permission(request, self)})

    @action(
        detail=True,
        methods=("POST",),
        url_path="events/goals",
        permission_classes=[IsCoachOrAdmin],
    )  # type: ignore[arg-type]
    def create_goal(
        self,
        request: Request,
        *args: Any,  # noqa: ANN401
        **kwargs: Any,  # noqa: ANN401
    ) -> Response:
        """Create a goal (Shot) event for this match."""
        match: Match = self.get_object()
        match_data = MatchData.objects.filter(match_link=match).first()
        if not match_data:
            return Response(
                {"detail": MATCH_TRACKER_DATA_NOT_FOUND},
                status=status.HTTP_404_NOT_FOUND,
            )

        serializer = ShotWriteSerializer(
            data=request.data,
            context={"match": match, "match_data": match_data},
        )
        serializer.is_valid(raise_exception=True)
        shot = serializer.save()
        return Response(
            _serialize_goal_event(match_data, shot),
            status=status.HTTP_201_CREATED,
        )

    @action(
        detail=True,
        methods=("PATCH", "DELETE"),
        url_path=r"events/goals/(?P<shot_id>[^/.]+)",
        permission_classes=[IsCoachOrAdmin],
    )  # type: ignore[arg-type]
    def goal_detail(
        self,
        request: Request,
        shot_id: str,
        *args: Any,  # noqa: ANN401
        **kwargs: Any,  # noqa: ANN401
    ) -> Response:
        """Update or delete an existing goal (Shot) event."""
        match: Match = self.get_object()
        match_data = MatchData.objects.filter(match_link=match).first()
        if not match_data:
            return Response(
                {"detail": MATCH_TRACKER_DATA_NOT_FOUND},
                status=status.HTTP_404_NOT_FOUND,
            )

        shot = Shot.objects.filter(id_uuid=shot_id, match_data=match_data).first()
        if not shot:
            return Response(
                {"detail": "Goal event not found."},
                status=status.HTTP_404_NOT_FOUND,
            )

        if request.method == "DELETE":
            shot.delete()
            return Response(status=status.HTTP_204_NO_CONTENT)

        serializer = ShotWriteSerializer(
            instance=shot,
            data=request.data,
            partial=True,
            context={"match": match, "match_data": match_data},
        )
        serializer.is_valid(raise_exception=True)
        shot = serializer.save()
        return Response(
            _serialize_goal_event(match_data, shot),
            status=status.HTTP_200_OK,
        )

    @action(
        detail=True,
        methods=("POST",),
        url_path="events/substitutes",
        permission_classes=[IsCoachOrAdmin],
    )  # type: ignore[arg-type]
    def create_substitute(
        self,
        request: Request,
        *args: Any,  # noqa: ANN401
        **kwargs: Any,  # noqa: ANN401
    ) -> Response:
        """Create a substitution (PlayerChange) event for this match."""
        match: Match = self.get_object()
        match_data = MatchData.objects.filter(match_link=match).first()
        if not match_data:
            return Response(
                {"detail": MATCH_TRACKER_DATA_NOT_FOUND},
                status=status.HTTP_404_NOT_FOUND,
            )

        serializer = PlayerChangeWriteSerializer(
            data=request.data,
            context={"match": match, "match_data": match_data},
        )
        serializer.is_valid(raise_exception=True)
        change = serializer.save()
        return Response(
            _serialize_substitute_event(match_data, change),
            status=status.HTTP_201_CREATED,
        )

    @action(
        detail=True,
        methods=("PATCH", "DELETE"),
        url_path=r"events/substitutes/(?P<change_id>[^/.]+)",
        permission_classes=[IsCoachOrAdmin],
    )  # type: ignore[arg-type]
    def substitute_detail(
        self,
        request: Request,
        change_id: str,
        *args: Any,  # noqa: ANN401
        **kwargs: Any,  # noqa: ANN401
    ) -> Response:
        """Update or delete a substitution (PlayerChange) event."""
        match: Match = self.get_object()
        match_data = MatchData.objects.filter(match_link=match).first()
        if not match_data:
            return Response(
                {"detail": MATCH_TRACKER_DATA_NOT_FOUND},
                status=status.HTTP_404_NOT_FOUND,
            )

        change = PlayerChange.objects.filter(
            id_uuid=change_id,
            player_group__match_data=match_data,
        ).first()
        if not change:
            return Response(
                {"detail": "Substitution event not found."},
                status=status.HTTP_404_NOT_FOUND,
            )

        if request.method == "DELETE":
            change.delete()
            return Response(status=status.HTTP_204_NO_CONTENT)

        serializer = PlayerChangeWriteSerializer(
            instance=change,
            data=request.data,
            partial=True,
            context={"match": match, "match_data": match_data},
        )
        serializer.is_valid(raise_exception=True)
        change = serializer.save()
        return Response(
            _serialize_substitute_event(match_data, change),
            status=status.HTTP_200_OK,
        )

    @action(
        detail=True,
        methods=("POST",),
        url_path="events/pauses",
        permission_classes=[IsCoachOrAdmin],
    )  # type: ignore[arg-type]
    def create_pause(
        self,
        request: Request,
        *args: Any,  # noqa: ANN401
        **kwargs: Any,  # noqa: ANN401
    ) -> Response:
        """Create a pause (Pause) event for this match."""
        match: Match = self.get_object()
        match_data = MatchData.objects.filter(match_link=match).first()
        if not match_data:
            return Response(
                {"detail": MATCH_TRACKER_DATA_NOT_FOUND},
                status=status.HTTP_404_NOT_FOUND,
            )

        serializer = PauseWriteSerializer(
            data=request.data,
            context={"match": match, "match_data": match_data},
        )
        serializer.is_valid(raise_exception=True)
        pause = serializer.save()
        return Response(
            _serialize_pause_event(match_data, pause),
            status=status.HTTP_201_CREATED,
        )

    @action(
        detail=True,
        methods=("PATCH", "DELETE"),
        url_path=r"events/pauses/(?P<pause_id>[^/.]+)",
        permission_classes=[IsCoachOrAdmin],
    )  # type: ignore[arg-type]
    def pause_detail(
        self,
        request: Request,
        pause_id: str,
        *args: Any,  # noqa: ANN401
        **kwargs: Any,  # noqa: ANN401
    ) -> Response:
        """Update or delete a pause (Pause) event."""
        match: Match = self.get_object()
        match_data = MatchData.objects.filter(match_link=match).first()
        if not match_data:
            return Response(
                {"detail": MATCH_TRACKER_DATA_NOT_FOUND},
                status=status.HTTP_404_NOT_FOUND,
            )

        pause = Pause.objects.filter(id_uuid=pause_id, match_data=match_data).first()
        if not pause:
            return Response(
                {"detail": "Pause event not found."},
                status=status.HTTP_404_NOT_FOUND,
            )

        if request.method == "DELETE":
            Timeout.objects.filter(pause=pause).delete()
            pause.delete()
            return Response(status=status.HTTP_204_NO_CONTENT)

        serializer = PauseWriteSerializer(
            instance=pause,
            data=request.data,
            partial=True,
            context={"match": match, "match_data": match_data},
        )
        serializer.is_valid(raise_exception=True)
        pause = serializer.save()
        return Response(
            _serialize_pause_event(match_data, pause),
            status=status.HTTP_200_OK,
        )

    @action(
        detail=True,
        methods=("POST",),
        url_path="events/timeouts",
        permission_classes=[IsCoachOrAdmin],
    )  # type: ignore[arg-type]
    def create_timeout(
        self,
        request: Request,
        *args: Any,  # noqa: ANN401
        **kwargs: Any,  # noqa: ANN401
    ) -> Response:
        """Create a timeout (Timeout + Pause) event for this match."""
        match: Match = self.get_object()
        match_data = MatchData.objects.filter(match_link=match).first()
        if not match_data:
            return Response(
                {"detail": MATCH_TRACKER_DATA_NOT_FOUND},
                status=status.HTTP_404_NOT_FOUND,
            )

        serializer = TimeoutWriteSerializer(
            data=request.data,
            context={"match": match, "match_data": match_data},
        )
        serializer.is_valid(raise_exception=True)
        timeout = serializer.save()
        if not timeout.pause:
            return Response(
                {"detail": "Timeout was created without a pause."},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )
        return Response(
            _serialize_pause_event(match_data, timeout.pause),
            status=status.HTTP_201_CREATED,
        )

    @action(
        detail=True,
        methods=("GET",),
        url_path="events/options",
        permission_classes=[IsCoachOrAdmin],
    )  # type: ignore[arg-type]
    def event_options(
        self,
        request: Request,
        *args: Any,  # noqa: ANN401
        **kwargs: Any,  # noqa: ANN401
    ) -> Response:
        """Return option lists needed to create/update match tracker events.

        The korfbal-web frontend uses this payload to build event editor dropdowns
        (teams, match parts, players, player groups, goal types).
        """
        match: Match = self.get_object()
        match_data = MatchData.objects.filter(match_link=match).first()
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
    )  # type: ignore[arg-type]
    def timeout_detail(
        self,
        request: Request,
        timeout_id: str,
        *args: Any,  # noqa: ANN401
        **kwargs: Any,  # noqa: ANN401
    ) -> Response:
        """Update or delete a timeout (Timeout + Pause) event."""
        match: Match = self.get_object()
        match_data = MatchData.objects.filter(match_link=match).first()
        if not match_data:
            return Response(
                {"detail": MATCH_TRACKER_DATA_NOT_FOUND},
                status=status.HTTP_404_NOT_FOUND,
            )

        timeout = (
            Timeout.objects.select_related("pause")
            .filter(
                id_uuid=timeout_id,
                match_data=match_data,
            )
            .first()
        )
        if not timeout:
            return Response(
                {"detail": "Timeout event not found."},
                status=status.HTTP_404_NOT_FOUND,
            )

        pause = timeout.pause
        if request.method == "DELETE":
            timeout.delete()
            if pause:
                pause.delete()
            return Response(status=status.HTTP_204_NO_CONTENT)

        serializer = TimeoutWriteSerializer(
            instance=timeout,
            data=request.data,
            partial=True,
            context={"match": match, "match_data": match_data},
        )
        serializer.is_valid(raise_exception=True)
        timeout = serializer.save()
        if not timeout.pause:
            return Response(
                {"detail": "Timeout has no pause."},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )
        return Response(
            _serialize_pause_event(match_data, timeout.pause),
            status=status.HTTP_200_OK,
        )


def _event_time_key(event: object) -> datetime:
    value = getattr(event, "time", None)
    if value is not None:
        return value
    value = getattr(event, "start_time", None)
    if value is not None:
        return value
    return datetime.min.replace(tzinfo=UTC)


def _time_in_minutes(
    *,
    match_data: MatchData,
    match_part_start: datetime,
    match_part_number: int,
    event_time: datetime,
) -> str:
    pauses = Pause.objects.filter(
        match_data=match_data,
        active=False,
        start_time__lt=event_time,
        start_time__gte=match_part_start,
    )
    pause_time = sum(pause.length().total_seconds() for pause in pauses)

    time_in_minutes = round(
        (
            (event_time - match_part_start).total_seconds()
            + ((match_part_number - 1) * int(match_data.part_length))
            - pause_time
        )
        / 60,
    )

    left_over = time_in_minutes - ((match_part_number * match_data.part_length) / 60)
    if left_over > 0:
        return (
            str(time_in_minutes - left_over).split(".")[0]
            + "+"
            + str(left_over).split(".")[0]
        )
    return str(time_in_minutes)


def _build_match_events(match_data: MatchData) -> list[dict[str, Any]]:
    goals = list(
        Shot.objects.select_related(
            "player__user",
            "shot_type",
            "match_part",
            "team",
        )
        .filter(match_data=match_data, scored=True)
        .order_by("time")
    )

    player_changes = list(
        PlayerChange.objects.select_related(
            "player_in__user",
            "player_out__user",
            "player_group",
            "match_part",
        )
        .filter(player_group__match_data=match_data)
        .order_by("time")
    )

    pauses = list(
        Pause.objects.select_related("match_part")
        .filter(match_data=match_data)
        .order_by("start_time")
    )

    events: list[object] = []
    events.extend(goals)
    events.extend(player_changes)
    events.extend(pauses)
    events.sort(key=_event_time_key)

    payload: list[dict[str, Any]] = []

    for event in events:
        serialized = _serialize_match_event(match_data, event)
        if serialized is not None:
            payload.append(serialized)

    return payload


def _serialize_match_event(
    match_data: MatchData,
    event: object,
) -> dict[str, Any] | None:
    if isinstance(event, Shot):
        return _serialize_goal_event(match_data, event)
    if isinstance(event, PlayerChange):
        return _serialize_substitute_event(match_data, event)
    if isinstance(event, Pause):
        return _serialize_pause_event(match_data, event)
    return None


def _serialize_goal_event(match_data: MatchData, event: Shot) -> dict[str, Any] | None:
    if not event.match_part or not event.time or not event.team or not event.shot_type:
        return None

    return {
        "event_kind": "shot",
        "event_id": str(event.id_uuid),
        "type": "goal",
        "name": "Gescoord",
        "match_part_id": str(event.match_part.id_uuid),
        "time_iso": event.time.isoformat(),
        "time": _time_in_minutes(
            match_data=match_data,
            match_part_start=event.match_part.start_time,
            match_part_number=event.match_part.part_number,
            event_time=event.time,
        ),
        "player_id": str(event.player.id_uuid),
        "player": event.player.user.username,
        "shot_type_id": str(event.shot_type.id_uuid),
        "goal_type": event.shot_type.name,
        "for_team": event.for_team,
        "team_id": str(event.team.id_uuid),
    }


def _serialize_substitute_event(
    match_data: MatchData,
    event: PlayerChange,
) -> dict[str, Any] | None:
    if not event.match_part or not event.time:
        return None

    return {
        "event_kind": "player_change",
        "event_id": str(event.id_uuid),
        "type": "substitute",
        "name": "Wissel",
        "match_part_id": str(event.match_part.id_uuid),
        "time_iso": event.time.isoformat(),
        "time": _time_in_minutes(
            match_data=match_data,
            match_part_start=event.match_part.start_time,
            match_part_number=event.match_part.part_number,
            event_time=event.time,
        ),
        "player_in_id": str(event.player_in.id_uuid),
        "player_in": event.player_in.user.username,
        "player_out_id": str(event.player_out.id_uuid),
        "player_out": event.player_out.user.username,
        "player_group_id": str(event.player_group.id_uuid),
    }


def _serialize_pause_event(
    match_data: MatchData,
    event: Pause,
) -> dict[str, Any] | None:
    if not event.match_part or not event.start_time:
        return None

    timeout = Timeout.objects.select_related("team").filter(pause=event).first()

    return {
        "event_kind": "timeout" if timeout else "pause",
        "event_id": str(timeout.id_uuid) if timeout else str(event.id_uuid),
        "pause_id": str(event.id_uuid),
        "type": "intermission",
        "name": "Time-out" if timeout else "Pauze",
        "match_part_id": str(event.match_part.id_uuid),
        "team_id": str(timeout.team_id) if timeout else None,
        "time": _time_in_minutes(
            match_data=match_data,
            match_part_start=event.match_part.start_time,
            match_part_number=event.match_part.part_number,
            event_time=event.start_time,
        ),
        "length": event.length().total_seconds(),
        "start_time": (event.start_time.isoformat() if event.start_time else None),
        "end_time": event.end_time.isoformat() if event.end_time else None,
    }
