"""Views for schedule endpoints."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
import json
import logging
from typing import Any, ClassVar
from uuid import uuid4

from django.conf import settings
from django.core import signing
from django.core.cache import cache
from django.db.models import Count, Q, QuerySet
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import permissions, status, viewsets
from rest_framework.decorators import action
from rest_framework.request import Request
from rest_framework.response import Response

from apps.awards.services.mvp import (
    build_mvp_candidates,
    cast_vote,
    cast_vote_anon,
    ensure_mvp_published,
)
from apps.game_tracker.models import MatchData, PlayerMatchImpact
from apps.game_tracker.services.match_impact import (
    LATEST_MATCH_IMPACT_ALGORITHM_VERSION,
    persist_match_impact_rows,
)
from apps.game_tracker.services.tracker_http import (
    TrackerCommandError,
    apply_tracker_command,
    get_tracker_state,
    poll_tracker_state,
)
from apps.kwt_common.utils.match_summary import build_match_summaries
from apps.player.models.player import Player
from apps.schedule.models import Match
from apps.team.models.team import Team

from .constants import (
    MVP_VOTE_COOKIE_MAX_AGE_SECONDS,
    MVP_VOTE_COOKIE_NAME,
    MVP_VOTE_COOKIE_SALT,
)
from .match_stats_payload import _build_match_stats_payload
from .match_viewset_events import MatchEventsActionsMixin
from .permissions import IsClubMemberOrCoachOrAdmin, IsCoachOrAdmin
from .serializers import MatchSerializer


logger = logging.getLogger(__name__)


def _self_heal_latest_impacts_for_finished_match(*, match_data: MatchData) -> None:
    """Best-effort: persist latest impacts for finished matches when missing."""
    if match_data.status != "finished":
        return

    has_latest = PlayerMatchImpact.objects.filter(
        match_data=match_data,
        algorithm_version=LATEST_MATCH_IMPACT_ALGORITHM_VERSION,
    ).exists()
    if has_latest:
        return

    lock_key = (
        f"korfbal:match-impacts-selfheal:{match_data.id_uuid}:"
        f"{LATEST_MATCH_IMPACT_ALGORITHM_VERSION}"
    )
    if not cache.add(lock_key, "1", timeout=60 * 10):
        return

    try:
        persist_match_impact_rows(
            match_data=match_data,
            algorithm_version=LATEST_MATCH_IMPACT_ALGORITHM_VERSION,
        )
    except Exception:
        # Never fail the endpoint; the frontend may still fall back (or retry)
        # if impacts are unavailable.
        logger.exception(
            "Failed to self-heal match impacts for %s",
            match_data.id_uuid,
        )


def _read_mvp_vote_tokens(request: Request) -> dict[str, str]:
    """Return the signed cookie mapping match_id -> voter_token."""
    try:
        raw = request.get_signed_cookie(
            MVP_VOTE_COOKIE_NAME,
            default="{}",
            salt=MVP_VOTE_COOKIE_SALT,
        )
    except signing.BadSignature:
        return {}

    # Some request stubs type this as `str | None`; be defensive.
    if raw is None:
        return {}

    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return {}

    if not isinstance(parsed, dict):
        return {}

    return {
        key: value
        for key, value in parsed.items()
        if isinstance(key, str) and isinstance(value, str) and key and value
    }


def _write_mvp_vote_tokens(
    *,
    request: Request,
    response: Response,
    tokens: dict[str, str],
) -> None:
    response.set_signed_cookie(
        MVP_VOTE_COOKIE_NAME,
        json.dumps(tokens, separators=(",", ":")),
        salt=MVP_VOTE_COOKIE_SALT,
        max_age=MVP_VOTE_COOKIE_MAX_AGE_SECONDS,
        samesite="Lax",
        secure=request.is_secure(),
        httponly=True,
        path="/",
    )


def _authenticated_player(request: Request) -> Player | None:
    if request.user.is_authenticated and hasattr(request.user, "player"):
        player = getattr(request.user, "player", None)
        if isinstance(player, Player):
            return player
    return None


def _vote_for_request(
    *,
    match: Match,
    request: Request,
    anon_voter_token_override: str | None = None,
) -> tuple[dict[str, str] | None, str | None]:
    """Return (user_vote_payload, anon_voter_token_used)."""
    player = _authenticated_player(request)
    if player:
        vote = match.mvp_votes.filter(voter=player).first()
        if not vote:
            return None, None
        return {"candidate_id_uuid": str(vote.candidate_id)}, None

    token = anon_voter_token_override
    if not token:
        tokens = _read_mvp_vote_tokens(request)
        token = tokens.get(str(match.id_uuid))
    if not token:
        return None, None

    vote = match.mvp_votes.filter(voter_token=token).first()
    if not vote:
        return None, token
    return {"candidate_id_uuid": str(vote.candidate_id)}, token


def _build_mvp_status_payload(
    *,
    match: Match,
    match_data: MatchData,
    request: Request,
    anon_voter_token_override: str | None = None,
) -> dict[str, Any]:
    mvp = ensure_mvp_published(match, match_data)
    candidates = build_mvp_candidates(match, match_data)
    user_vote, _anon_token_used = _vote_for_request(
        match=match,
        request=request,
        anon_voter_token_override=anon_voter_token_override,
    )

    mvp_player = mvp.mvp_player
    mvp_payload: dict[str, str | None] | None = None
    if mvp_player:
        username = mvp_player.user.username
        display_name = mvp_player.user.get_full_name() or username
        mvp_payload = {
            "id_uuid": str(mvp_player.id_uuid),
            "username": username,
            "display_name": display_name,
            "profile_picture_url": mvp_player.get_profile_picture(),
        }

    now = timezone.now()
    open_for_votes = bool(now < mvp.closes_at)

    # Vote breakdown: only players that received at least 1 vote.
    vote_rows = list(
        match.mvp_votes
        .values("candidate")
        .annotate(votes=Count("id_uuid"))
        .order_by(
            "-votes",
            "candidate",
        )
    )

    side_by_id = {c.id_uuid: c.team_side for c in candidates}

    voted_candidate_ids = [
        str(row.get("candidate"))
        for row in vote_rows
        if row.get("candidate") is not None
    ]
    voted_players = Player.objects.select_related("user").filter(
        id_uuid__in=voted_candidate_ids
    )
    voted_players_by_id = {str(p.id_uuid): p for p in voted_players}

    vote_breakdown: list[dict[str, Any]] = []
    for row in vote_rows:
        cid = row.get("candidate")
        if cid is None:
            continue
        cid_str = str(cid)
        player = voted_players_by_id.get(cid_str)
        if not player:
            continue
        username = player.user.username
        display_name = player.user.get_full_name() or username
        votes = row.get("votes")
        vote_breakdown.append({
            "candidate": {
                "id_uuid": cid_str,
                "username": username,
                "display_name": display_name,
                "profile_picture_url": player.get_profile_picture(),
                "team_side": side_by_id.get(cid_str),
            },
            "votes": int(votes) if isinstance(votes, int) else 0,
        })

    return {
        "available": True,
        "match_status": match_data.status,
        "open": open_for_votes,
        "finished_at": mvp.finished_at.isoformat(),
        "closes_at": mvp.closes_at.isoformat(),
        "candidates": [
            {
                "id_uuid": c.id_uuid,
                "username": c.username,
                "display_name": c.display_name,
                "profile_picture_url": c.profile_picture_url,
                "team_side": c.team_side,
            }
            for c in candidates
        ],
        "user_vote": user_vote,
        "mvp": mvp_payload,
        "published_at": mvp.published_at.isoformat() if mvp.published_at else None,
        "vote_breakdown": vote_breakdown,
    }


class MatchViewSet(MatchEventsActionsMixin, viewsets.ReadOnlyModelViewSet):
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
                    Player.objects
                    .prefetch_related("team_follow")
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

    def _is_cacheable_public_request(self) -> bool:
        """Return True when it is safe to cache a response for this request.

        We deliberately skip caching for authenticated callers and for requests
        that use the user-specific `followed` filter.
        """
        if self.request.user.is_authenticated:
            return False
        return not self.request.query_params.get("followed")

    def _public_cache_key(self) -> str:
        """Cache key that varies by full path (including query string)."""
        return f"korfbal:schedule:{self.request.get_full_path()}"

    @action(detail=False, methods=("GET",), url_path="next")
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
        if self._is_cacheable_public_request():
            cache_key = self._public_cache_key()
            cache_miss = object()
            cached_payload = cache.get(cache_key, cache_miss)
            if cached_payload is not cache_miss:
                return Response(cached_payload)

        match = self._upcoming_queryset().first()
        if not match:
            payload: Any = None
            if self._is_cacheable_public_request():
                cache.set(self._public_cache_key(), payload, timeout=30)
            return Response(payload, status=status.HTTP_200_OK)
        serializer = self.get_serializer(match)
        payload = serializer.data
        if self._is_cacheable_public_request():
            cache.set(self._public_cache_key(), payload, timeout=30)
        return Response(payload)

    @action(detail=False, methods=("GET",), url_path="upcoming")
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

    @action(detail=False, methods=("GET",), url_path="recent")
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
            self
            .get_queryset()
            .filter(start_time__gte=window)
            .order_by("-start_time")[:5]
        )
        serializer = self.get_serializer(queryset, many=True)
        return Response(serializer.data)

    @action(detail=False, methods=("GET",), url_path="finished")
    def finished(
        self,
        request: Request,
        *args: Any,  # noqa: ANN401
        **kwargs: Any,  # noqa: ANN401
    ) -> Response:
        """Return the latest finished matches as match summaries.

        This endpoint is designed for public/anonymous UIs (Home page, etc.)
        that want to show the latest results with scores.

        Query params:
            limit: Maximum number of matches to return (default: 3).

        Returns:
            Response: List of match summary dictionaries.

        """
        limit_param = request.query_params.get("limit")
        try:
            limit = int(limit_param) if limit_param else 3
        except ValueError:
            limit = 3

        limit = max(limit, 1)

        if self._is_cacheable_public_request():
            cache_key = self._public_cache_key()
            cache_miss = object()
            cached_payload = cache.get(cache_key, cache_miss)
            if cached_payload is not cache_miss:
                return Response(cached_payload)

        # Respect the same filtering as other match list endpoints.
        # Instead of building an IN(subquery) over matches, apply the filter
        # directly to the MatchData join to keep the query planner happy.
        now = timezone.now()
        match_filter = Q(match_link__start_time__lte=now)

        team_ids = request.query_params.getlist("team")
        club_ids = request.query_params.getlist("club")
        season_id = request.query_params.get("season")

        if not team_ids and request.query_params.get("followed"):
            player = self._get_player()
            if player:
                team_ids = list(player.team_follow.values_list("id_uuid", flat=True))

        if team_ids:
            match_filter &= Q(match_link__home_team__id_uuid__in=team_ids) | Q(
                match_link__away_team__id_uuid__in=team_ids
            )

        if club_ids:
            match_filter &= Q(match_link__home_team__club__id_uuid__in=club_ids) | Q(
                match_link__away_team__club__id_uuid__in=club_ids
            )

        if season_id:
            match_filter &= Q(match_link__season__id_uuid=season_id)

        match_data_queryset = (
            MatchData.objects
            .select_related(
                "match_link",
                "match_link__home_team__club",
                "match_link__away_team__club",
                "match_link__season",
            )
            .filter(match_filter, status="finished")
            .order_by("-match_link__start_time")[:limit]
        )

        summaries = build_match_summaries(list(match_data_queryset))
        if self._is_cacheable_public_request():
            cache.set(self._public_cache_key(), summaries, timeout=30)
        return Response(summaries)

    @action(
        detail=True,
        methods=("GET",),
        url_path=r"tracker/(?P<team_id>[^/.]+)/state",
        permission_classes=[IsClubMemberOrCoachOrAdmin],
    )
    def tracker_state(
        self,
        request: Request,
        team_id: str,
        *args: Any,  # noqa: ANN401
        **kwargs: Any,  # noqa: ANN401
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
            code = getattr(exc, "code", "error")
            http_status = (
                status.HTTP_404_NOT_FOUND
                if code == "not_found"
                else status.HTTP_400_BAD_REQUEST
            )
            return Response({"detail": str(exc), "code": code}, status=http_status)

    @action(
        detail=True,
        methods=("POST",),
        url_path=r"tracker/(?P<team_id>[^/.]+)/commands",
        permission_classes=[IsCoachOrAdmin],
    )
    def tracker_command(
        self,
        request: Request,
        team_id: str,
        *args: Any,  # noqa: ANN401
        **kwargs: Any,  # noqa: ANN401
    ) -> Response:
        """Apply a match tracker command and return updated state."""
        match: Match = self.get_object()
        team = get_object_or_404(Team.objects.select_related("club"), id_uuid=team_id)
        if not isinstance(request.data, dict):
            return Response(
                {"detail": "Invalid JSON body."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        try:
            return Response(
                apply_tracker_command(match, team=team, payload=request.data),
                status=status.HTTP_200_OK,
            )
        except TrackerCommandError as exc:
            code = getattr(exc, "code", "error")
            if code == "match_paused":
                return Response(
                    {"detail": str(exc), "code": code},
                    status=status.HTTP_409_CONFLICT,
                )
            http_status = (
                status.HTTP_404_NOT_FOUND
                if code == "not_found"
                else status.HTTP_400_BAD_REQUEST
            )
            return Response({"detail": str(exc), "code": code}, status=http_status)

    @action(
        detail=True,
        methods=("GET",),
        url_path=r"tracker/(?P<team_id>[^/.]+)/poll",
        permission_classes=[IsClubMemberOrCoachOrAdmin],
    )
    def tracker_poll(
        self,
        request: Request,
        team_id: str,
        *args: Any,  # noqa: ANN401
        **kwargs: Any,  # noqa: ANN401
    ) -> Response:
        """Long-poll for match tracker state updates."""
        match: Match = self.get_object()
        team = get_object_or_404(Team.objects.select_related("club"), id_uuid=team_id)

        since_raw = request.query_params.get("since")
        timeout_raw = request.query_params.get("timeout")

        try:
            since = (
                datetime.fromisoformat(since_raw)
                if since_raw
                else datetime.min.replace(tzinfo=UTC)
            )
            if since.tzinfo is None:
                since = since.replace(tzinfo=UTC)
        except ValueError:
            return Response(
                {"detail": "Invalid 'since' timestamp."},
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
                    since=since,
                    timeout_seconds=timeout_seconds,
                ),
                status=status.HTTP_200_OK,
            )
        except TrackerCommandError as exc:
            code = getattr(exc, "code", "error")
            http_status = (
                status.HTTP_404_NOT_FOUND
                if code == "not_found"
                else status.HTTP_400_BAD_REQUEST
            )
            return Response({"detail": str(exc), "code": code}, status=http_status)

    def _public_live_state(
        self,
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
        }

    @action(
        detail=True,
        methods=("GET",),
        url_path="live",
        permission_classes=[permissions.AllowAny],
    )
    def live_state(
        self,
        request: Request,
        *args: Any,  # noqa: ANN401
        **kwargs: Any,  # noqa: ANN401
    ) -> Response:
        """Return a match-level live snapshot (timer + score).

        This endpoint is designed for read-only UIs like the korfbal-web Match
        page. It intentionally does not include player groups or other
        coach-only tracker details.

        """
        match: Match = self.get_object()
        match_data = MatchData.objects.filter(match_link=match).first()
        if not match_data:
            return Response(None, status=status.HTTP_200_OK)

        try:
            home_tracker_state = get_tracker_state(match, team=match.home_team)
        except TrackerCommandError as exc:
            code = getattr(exc, "code", "error")
            http_status = (
                status.HTTP_404_NOT_FOUND
                if code == "not_found"
                else status.HTTP_400_BAD_REQUEST
            )
            return Response({"detail": str(exc), "code": code}, status=http_status)

        return Response(
            self._public_live_state(
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
        self,
        request: Request,
        *args: Any,  # noqa: ANN401
        **kwargs: Any,  # noqa: ANN401
    ) -> Response:
        """Long-poll for match-level live updates (timer + score).

        Response shape mirrors tracker polling:
        - on timeout: {changed: false, server_time, last_changed_at}
        - on change: live_state payload

        """
        match: Match = self.get_object()
        match_data = MatchData.objects.filter(match_link=match).first()
        if not match_data:
            return Response(
                {
                    "changed": False,
                    "server_time": timezone.now().isoformat(),
                    "last_changed_at": timezone.now().isoformat(),
                },
                status=status.HTTP_200_OK,
            )

        since_raw = request.query_params.get("since")
        timeout_raw = request.query_params.get("timeout")

        try:
            since = (
                datetime.fromisoformat(since_raw)
                if since_raw
                else datetime.min.replace(tzinfo=UTC)
            )
            if since.tzinfo is None:
                since = since.replace(tzinfo=UTC)
        except ValueError:
            return Response(
                {"detail": "Invalid 'since' timestamp."},
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
                since=since,
                timeout_seconds=timeout_seconds,
            )
        except TrackerCommandError as exc:
            code = getattr(exc, "code", "error")
            http_status = (
                status.HTTP_404_NOT_FOUND
                if code == "not_found"
                else status.HTTP_400_BAD_REQUEST
            )
            return Response({"detail": str(exc), "code": code}, status=http_status)

        if isinstance(payload, dict) and payload.get("changed") is False:
            return Response(payload, status=status.HTTP_200_OK)

        # Changed: payload is a full tracker state (home team perspective)
        if not isinstance(payload, dict):
            return Response(
                {"detail": "Invalid live poll payload."},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

        return Response(
            self._public_live_state(
                match=match,
                match_data=match_data,
                home_tracker_state=payload,
            ),
            status=status.HTTP_200_OK,
        )

    @action(detail=True, methods=("GET",), url_path="summary")
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

    @action(detail=True, methods=("GET",), url_path="stats")
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

        payload = _build_match_stats_payload(match=match, match_data=match_data)
        return Response(payload, status=status.HTTP_200_OK)

    @action(detail=True, methods=("GET",), url_path="impacts")
    def impacts(
        self,
        request: Request,
        *args: Any,  # noqa: ANN401
        **kwargs: Any,  # noqa: ANN401
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
        match: Match = self.get_object()
        match_data = MatchData.objects.filter(match_link=match).first()
        if not match_data:
            return Response(
                {
                    "match_data_id": None,
                    "status": "unknown",
                    "algorithm_version": LATEST_MATCH_IMPACT_ALGORITHM_VERSION,
                    "computed_at": None,
                    "impacts": [],
                },
                status=status.HTTP_200_OK,
            )

        _self_heal_latest_impacts_for_finished_match(match_data=match_data)

        impacts = list(
            PlayerMatchImpact.objects
            .filter(
                match_data=match_data,
                algorithm_version=LATEST_MATCH_IMPACT_ALGORITHM_VERSION,
            )
            .select_related("player", "team")
            .order_by("-impact_score", "player__user__username")
        )

        computed_at = None
        if impacts:
            computed_at = max(impact.computed_at for impact in impacts).isoformat()

        def _side_for_team_id(team_id: str | None) -> str | None:
            if not team_id:
                return None
            if team_id == str(match.home_team_id):
                return "home"
            if team_id == str(match.away_team_id):
                return "away"
            return None

        payload = {
            "match_data_id": str(match_data.id_uuid),
            "status": match_data.status,
            "algorithm_version": LATEST_MATCH_IMPACT_ALGORITHM_VERSION,
            "computed_at": computed_at,
            "impacts": [
                {
                    "player_id_uuid": str(impact.player_id),
                    "team_id_uuid": str(impact.team_id) if impact.team_id else None,
                    "team_side": _side_for_team_id(
                        str(impact.team_id) if impact.team_id else None
                    ),
                    "impact_score": float(impact.impact_score),
                }
                for impact in impacts
            ],
        }
        return Response(payload, status=status.HTTP_200_OK)

    @action(
        detail=True,
        methods=("GET",),
        url_path="mvp",
        permission_classes=[permissions.AllowAny],
    )
    def mvp_status(
        self,
        request: Request,
        *args: Any,  # noqa: ANN401
        **kwargs: Any,  # noqa: ANN401
    ) -> Response:
        """Return MVP voting status + candidates + published winner (if any)."""
        match: Match = self.get_object()
        match_data = MatchData.objects.filter(match_link=match).first()
        if not match_data:
            return Response(
                {
                    "available": False,
                    "match_status": "unknown",
                    "open": False,
                    "finished_at": None,
                    "closes_at": None,
                    "published_at": None,
                    "candidates": [],
                    "user_vote": None,
                    "mvp": None,
                    "vote_breakdown": [],
                },
                status=status.HTTP_200_OK,
            )

        if match_data.status != "finished":
            return Response(
                {
                    "available": False,
                    "match_status": match_data.status,
                    "open": False,
                    "finished_at": None,
                    "closes_at": None,
                    "published_at": None,
                    "candidates": [],
                    "user_vote": None,
                    "mvp": None,
                    "vote_breakdown": [],
                },
                status=status.HTTP_200_OK,
            )

        payload = _build_mvp_status_payload(
            match=match,
            match_data=match_data,
            request=request,
        )
        return Response(payload, status=status.HTTP_200_OK)

    @action(
        detail=True,
        methods=("POST",),
        url_path="mvp/vote",
        permission_classes=[permissions.AllowAny],
    )
    def mvp_vote(
        self,
        request: Request,
        *args: Any,  # noqa: ANN401
        **kwargs: Any,  # noqa: ANN401
    ) -> Response:
        """Cast or update the current user's MVP vote."""
        match: Match = self.get_object()
        match_data = MatchData.objects.filter(match_link=match).first()
        if not match_data or match_data.status != "finished":
            return Response(
                {"detail": "Voting is only available after the match is finished."},
                status=status.HTTP_409_CONFLICT,
            )

        if not isinstance(request.data, dict):
            return Response(
                {"detail": "Invalid JSON body."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        candidate_id = request.data.get("candidate_id_uuid")
        if not isinstance(candidate_id, str) or not candidate_id:
            return Response(
                {"detail": "Missing 'candidate_id_uuid'."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        candidate = Player.objects.filter(id_uuid=candidate_id).first()
        if not candidate:
            return Response(
                {"detail": "Unknown candidate."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        player = _authenticated_player(request)
        anon_voter_token: str | None = None
        anon_tokens: dict[str, str] | None = None

        try:
            if player:
                cast_vote(
                    match=match,
                    match_data=match_data,
                    voter=player,
                    candidate=candidate,
                )
            else:
                anon_tokens = _read_mvp_vote_tokens(request)
                match_key = str(match.id_uuid)
                anon_voter_token = anon_tokens.get(match_key)
                if not anon_voter_token:
                    anon_voter_token = str(uuid4())
                    anon_tokens[match_key] = anon_voter_token

                cast_vote_anon(
                    match=match,
                    match_data=match_data,
                    voter_token=anon_voter_token,
                    candidate=candidate,
                )
        except ValueError as exc:
            return Response(
                {"detail": str(exc)},
                status=status.HTTP_409_CONFLICT,
            )

        payload = _build_mvp_status_payload(
            match=match,
            match_data=match_data,
            request=request,
            anon_voter_token_override=anon_voter_token,
        )

        response = Response(payload, status=status.HTTP_200_OK)
        if not player:
            _write_mvp_vote_tokens(
                request=request,
                response=response,
                tokens=anon_tokens or {},
            )
        return response
