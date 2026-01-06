"""Views powering the player API endpoints."""

from __future__ import annotations

from collections.abc import Mapping
from contextlib import suppress
from dataclasses import dataclass
from datetime import timedelta
import logging
import os
from pathlib import Path
import secrets
import shutil
import subprocess  # nosec B404
import tempfile
from typing import Any, ClassVar
from urllib.parse import urlencode

from django.conf import settings
from django.contrib.auth.base_user import AbstractBaseUser
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.core.files.uploadedfile import UploadedFile
from django.db import transaction
from django.db.models import (
    BooleanField,
    Count,
    Exists,
    OuterRef,
    Q,
    QuerySet,
    Subquery,
    Value,
)
from django.http import HttpResponseRedirect
from django.utils import timezone
from kombu.exceptions import OperationalError as KombuOperationalError
import requests
from rest_framework import permissions, status, viewsets
from rest_framework.exceptions import PermissionDenied
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.game_tracker.models import MatchData, MatchPlayer, PlayerGroup, Shot
from apps.kwt_common.utils.match_summary import build_match_summaries
from apps.player.models.cached_song import CachedSong, CachedSongStatus
from apps.player.models.player import Player
from apps.player.models.player_song import PlayerSong, PlayerSongStatus
from apps.player.models.push_subscription import PlayerPushSubscription
from apps.player.models.spotify_token import SpotifyToken
from apps.player.privacy import can_view_by_visibility
from apps.player.services.web_push import (
    WebPushException,
    WebPushPayload,
    send_to_model_subscription,
    webpush_library_available,
)
from apps.player.spotify import canonicalize_spotify_track_url
from apps.player.tasks import download_cached_song, download_player_song
from apps.schedule.models import Season
from apps.schedule.models.mvp import MatchMvp
from apps.team.api.serializers import TeamSerializer
from apps.team.models import TeamData

from .serializers import (
    PlayerPrivacySettingsSerializer,
    PlayerPushSubscriptionCreateSerializer,
    PlayerPushSubscriptionDeactivateSerializer,
    PlayerPushSubscriptionSerializer,
    PlayerSerializer,
    PlayerSongCreateSerializer,
    PlayerSongSerializer,
    PlayerSongUpdateSerializer,
)


logger = logging.getLogger(__name__)

TEST_PUSH_ERROR_LIMIT = 10


PLAYER_NOT_FOUND_MESSAGE = "Player not found"
PLAYER_NOT_FOUND_DETAIL = {"detail": PLAYER_NOT_FOUND_MESSAGE}
SONG_NOT_FOUND_DETAIL = {"detail": "Song not found"}

AUTHENTICATION_REQUIRED_MESSAGE = "Authentication required"
AUTHENTICATION_REQUIRED_DETAIL = {"detail": AUTHENTICATION_REQUIRED_MESSAGE}

PRIVATE_ACCOUNT_MESSAGE = "Private account"
PRIVATE_ACCOUNT_DETAIL = {"code": "private_account", "detail": PRIVATE_ACCOUNT_MESSAGE}

CELERY_BROKER_UNAVAILABLE_MESSAGE = "Celery broker unavailable"

SPOTIFY_NOT_CONFIGURED_MESSAGE = "Spotify is not configured on the server"
SPOTIFY_NOT_CONFIGURED_DETAIL = {"detail": SPOTIFY_NOT_CONFIGURED_MESSAGE}

SPOTIFY_NO_ACTIVE_DEVICE_DETAIL = (
    "No active Spotify device found. Open Spotify on your phone and try again."
)


def _store_goal_song_upload_best_effort(
    *,
    player: Player,
    uploaded: UploadedFile,
    safe_name: str,
    clip_duration_seconds: int = 8,
) -> tuple[str, str]:
    """Store a goal-song upload as a short clip, falling back to full upload.

    Returns:
        (stored_path, url)

    """

    def _store_original() -> tuple[str, str]:
        key_original = (
            f"goal_songs/{player.id_uuid}/"
            f"{timezone.now().strftime('%Y%m%d%H%M%S')}_{safe_name}"
        )
        with suppress(Exception):
            uploaded.seek(0)
        stored = default_storage.save(key_original, uploaded)
        return stored, default_storage.url(stored)

    ffmpeg_path = shutil.which("ffmpeg")
    if not ffmpeg_path:
        return _store_original()

    try:
        with tempfile.TemporaryDirectory(prefix="goal_song_") as tmpdir:
            input_path = os.path.join(tmpdir, "input")
            output_path = os.path.join(tmpdir, "clip.mp3")

            with suppress(Exception):
                uploaded.seek(0)
            with open(input_path, "wb") as handle:
                handle.writelines(uploaded.chunks())

            subprocess.run(
                [
                    ffmpeg_path,
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-y",
                    "-ss",
                    "0",
                    "-i",
                    input_path,
                    "-t",
                    str(clip_duration_seconds),
                    "-vn",
                    # Prevent inheriting source tags like TLEN (full-track length),
                    # which can confuse browser/OS duration reporting for short clips.
                    "-map_metadata",
                    "-1",
                    "-map_chapters",
                    "-1",
                    "-acodec",
                    "libmp3lame",
                    "-q:a",
                    "4",
                    output_path,
                ],
                check=True,
            )  # nosec B603

            clip_key = (
                f"goal_songs/{player.id_uuid}/"
                f"{timezone.now().strftime('%Y%m%d%H%M%S')}_clip_"
                f"{os.path.splitext(safe_name)[0] or 'goal_song'}.mp3"
            )

            clip_bytes = Path(output_path).read_bytes()
            stored_path = default_storage.save(
                clip_key,
                ContentFile(clip_bytes),
            )
            return stored_path, default_storage.url(stored_path)
    except (FileNotFoundError, subprocess.CalledProcessError):
        logger.info(
            "ffmpeg unavailable or failed; storing full goal song upload",
            exc_info=True,
        )
        return _store_original()
    except Exception:
        logger.warning(
            "Unexpected error while clipping goal song; storing full upload",
            exc_info=True,
        )
        return _store_original()


def _redirect_to_frontend(redirect_path: str | None = None) -> HttpResponseRedirect:
    """Redirect the user back to the SPA frontend.

    Notes:
        - Uses the configured WEB_APP_ORIGIN when available.
        - Only allows relative redirect paths (starting with '/').

    """
    web_origin = getattr(settings, "WEB_APP_ORIGIN", "").rstrip("/")
    if not web_origin:
        return HttpResponseRedirect("/")

    if isinstance(redirect_path, str) and redirect_path.startswith("/"):
        return HttpResponseRedirect(f"{web_origin}{redirect_path}")

    return HttpResponseRedirect(f"{web_origin}/")


def _spotify_enabled() -> bool:
    return bool(settings.SPOTIFY_CLIENT_ID and settings.SPOTIFY_CLIENT_SECRET)


def _normalise_spotify_track_uri(value: str) -> str:
    raw = value.strip()
    if raw.startswith("spotify:track:"):
        return raw

    if "open.spotify.com/track/" in raw:
        track_id = raw.split("open.spotify.com/track/")[-1].split("?")[0].split("/")[0]
        if track_id:
            return f"spotify:track:{track_id}"

    return raw


def _get_or_create_spotify_oauth_state(request: Request) -> str:
    state = secrets.token_urlsafe(24)
    request.session["spotify_oauth_state"] = state
    request.session.modified = True
    return state


def _get_spotify_token(user: AbstractBaseUser) -> SpotifyToken | None:
    return SpotifyToken.objects.filter(user=user).first()


def _refresh_spotify_access_token(token: SpotifyToken) -> SpotifyToken:
    payload = {
        "grant_type": "refresh_token",
        "refresh_token": token.refresh_token,
        "client_id": settings.SPOTIFY_CLIENT_ID,
        "client_secret": settings.SPOTIFY_CLIENT_SECRET,
    }
    response = requests.post(
        "https://accounts.spotify.com/api/token",
        data=payload,
        timeout=10,
    )
    response.raise_for_status()
    data: dict[str, Any] = response.json()

    access_token = str(data.get("access_token") or "")
    expires_in = int(data.get("expires_in") or 3600)
    if not access_token:
        raise RuntimeError("Spotify token refresh failed")

    token.access_token = access_token
    refreshed_refresh = data.get("refresh_token")
    if isinstance(refreshed_refresh, str) and refreshed_refresh:
        token.refresh_token = refreshed_refresh

    token.expires_at = timezone.now() + timedelta(seconds=max(0, expires_in - 60))
    token.save(update_fields=["access_token", "refresh_token", "expires_at"])
    return token


def _ensure_spotify_access_token(user: AbstractBaseUser) -> str:
    token = _get_spotify_token(user)
    if token is None:
        raise RuntimeError("Spotify not connected")
    if token.is_token_expired():
        token = _refresh_spotify_access_token(token)
    return token.access_token


def _spotify_play_error_response(play_response: requests.Response) -> Response:
    detail = play_response.text or "Spotify play failed"

    spotify_message = ""
    try:
        spotify_payload: dict[str, Any] = play_response.json()
        spotify_error = spotify_payload.get("error")
        if isinstance(spotify_error, dict):
            spotify_message = str(spotify_error.get("message") or "")
    except (ValueError, TypeError):
        spotify_message = ""

    # Spotify returns 404 when there is no active device.
    if (
        play_response.status_code == status.HTTP_404_NOT_FOUND
        and "no active device" in spotify_message.lower()
    ):
        return Response(
            {
                "code": "no_active_device",
                "detail": SPOTIFY_NO_ACTIVE_DEVICE_DETAIL,
            },
            status=status.HTTP_409_CONFLICT,
        )

    return Response(
        {"code": "spotify_play_failed", "detail": spotify_message or detail},
        status=status.HTTP_400_BAD_REQUEST,
    )


def _get_current_player(request: Request) -> Player | None:
    """Resolve the current player from the request context.

    Args:
        request (Request): Incoming request.

    Returns:
        Player | None: The resolved player or ``None`` when not found.

    """
    queryset = Player.objects.select_related("user").prefetch_related(
        "team_follow",
        "club_follow",
        "member_clubs",
        "club_membership_links",
        "club_membership_links__club",
    )

    if request.user.is_authenticated:
        try:
            return queryset.get(user=request.user)
        except Player.DoesNotExist:
            return None

    if settings.DEBUG:
        player_id = request.query_params.get("player_id")
        if player_id:
            player = queryset.filter(id_uuid=player_id).first()
            if player:
                return player
        return queryset.first()

    return None


class PlayerViewSet(viewsets.ModelViewSet):
    """Provide CRUD operations for players."""

    queryset = Player.objects.select_related("user").prefetch_related(
        "team_follow",
        "club_follow",
        "member_clubs",
        "club_membership_links",
        "club_membership_links__club",
    )
    serializer_class = PlayerSerializer
    permission_classes: ClassVar[list[type[permissions.BasePermission]]] = [
        permissions.IsAuthenticatedOrReadOnly,
    ]
    lookup_field = "id_uuid"

    def _ensure_can_modify(self, player: Player) -> None:
        user = self.request.user
        if not user or not getattr(user, "is_authenticated", False):
            raise PermissionDenied(AUTHENTICATION_REQUIRED_MESSAGE)

        if getattr(user, "is_staff", False) or getattr(user, "is_superuser", False):
            return

        user_id = getattr(user, "id", None)
        if user_id is None or player.user.id != user_id:
            raise PermissionDenied("You do not have permission to modify this player")

    def perform_update(self, serializer: Any) -> None:  # noqa: ANN401
        """Update a player after enforcing ownership/staff checks."""
        player = self.get_object()
        self._ensure_can_modify(player)
        serializer.save()

    def perform_destroy(self, instance: Player) -> None:
        """Delete a player after enforcing ownership/staff checks."""
        self._ensure_can_modify(instance)
        instance.delete()


class CurrentPlayerAPIView(APIView):
    """Return the profile for the active player (or a debug fallback)."""

    permission_classes: ClassVar[list[type[permissions.BasePermission]]] = [
        permissions.AllowAny,
    ]

    def get(
        self,
        request: Request,
        *args: Any,  # noqa: ANN401
        **kwargs: Any,  # noqa: ANN401
    ) -> Response:
        """Return the current player's profile.

        Args:
            request (Request): The request object.
            *args (Any): Positional arguments.
            **kwargs (Any): Keyword arguments.

        Returns:
            Response: The serialized player profile.

        """
        player = _get_current_player(request)

        if player is None:
            return Response(PLAYER_NOT_FOUND_DETAIL, status=status.HTTP_404_NOT_FOUND)

        serializer = PlayerSerializer(player, context={"request": request})
        return Response(serializer.data)


class PlayerFollowedTeamsAPIView(APIView):
    """Return teams followed by a player.

    This exists to avoid fetching `/api/team/teams/` (paginated global list)
    and filtering client-side.

    URLs:
        - /api/player/me/followed-teams/
        - /api/player/players/<uuid>/followed-teams/
    """

    permission_classes: ClassVar[list[type[permissions.BasePermission]]] = [
        permissions.AllowAny,
    ]

    def get(
        self,
        request: Request,
        player_id: str | None = None,
        *args: Any,  # noqa: ANN401
        **kwargs: Any,  # noqa: ANN401
    ) -> Response:
        """Return teams followed by the requested player."""
        player = self._resolve_player(request, player_id)
        if player is None:
            return Response(PLAYER_NOT_FOUND_DETAIL, status=status.HTTP_404_NOT_FOUND)

        # Follow lists are preferences; when viewing another player enforce the
        # same visibility gate as other profile endpoints.
        if player_id:
            viewer = (
                Player.objects.filter(user=request.user).first()
                if request.user.is_authenticated
                else None
            )
            if not can_view_by_visibility(
                visibility=player.stats_visibility,
                viewer=viewer,
                target=player,
            ):
                return Response(
                    PRIVATE_ACCOUNT_DETAIL,
                    status=status.HTTP_403_FORBIDDEN,
                )

        teams_qs = (
            player.team_follow
            .all()
            .select_related("club")
            .order_by(
                "club__name",
                "name",
            )
        )

        serializer = TeamSerializer(teams_qs, many=True, context={"request": request})
        return Response(serializer.data)

    @staticmethod
    def _resolve_player(request: Request, player_id: str | None) -> Player | None:
        if player_id:
            return (
                Player.objects
                .select_related("user")
                .prefetch_related("team_follow")
                .filter(id_uuid=player_id)
                .first()
            )

        return _get_current_player(request)


class CurrentPlayerFollowedTeamsAPIView(PlayerFollowedTeamsAPIView):
    """Return teams followed by the current player."""

    def get(
        self,
        request: Request,
        *args: Any,  # noqa: ANN401
        **kwargs: Any,  # noqa: ANN401
    ) -> Response:
        """Return teams followed by the current player."""
        return super().get(request, None, *args, **kwargs)


class CurrentPlayerPrivacySettingsAPIView(APIView):
    """Read/update privacy visibility settings for the authenticated player."""

    permission_classes: ClassVar[list[type[permissions.BasePermission]]] = [
        permissions.IsAuthenticated,
    ]

    def get(
        self,
        request: Request,
        *args: Any,  # noqa: ANN401
        **kwargs: Any,  # noqa: ANN401
    ) -> Response:
        """Return the authenticated player's privacy visibility settings."""
        player = Player.objects.select_related("user").filter(user=request.user).first()
        if player is None:
            return Response(PLAYER_NOT_FOUND_DETAIL, status=status.HTTP_404_NOT_FOUND)

        profile_visibility = player.profile_picture_visibility
        if profile_visibility == Player.Visibility.PRIVATE:
            profile_visibility = Player.Visibility.CLUB

        stats_visibility = player.stats_visibility
        if stats_visibility == Player.Visibility.PRIVATE:
            stats_visibility = Player.Visibility.CLUB

        return Response({
            "profile_picture_visibility": profile_visibility,
            "stats_visibility": stats_visibility,
        })

    def patch(
        self,
        request: Request,
        *args: Any,  # noqa: ANN401
        **kwargs: Any,  # noqa: ANN401
    ) -> Response:
        """Update the authenticated player's privacy visibility settings."""
        player = Player.objects.select_related("user").filter(user=request.user).first()
        if player is None:
            return Response(PLAYER_NOT_FOUND_DETAIL, status=status.HTTP_404_NOT_FOUND)

        serializer = PlayerPrivacySettingsSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        update_fields: list[str] = []
        if "profile_picture_visibility" in serializer.validated_data:
            player.profile_picture_visibility = str(
                serializer.validated_data["profile_picture_visibility"]
            )
            update_fields.append("profile_picture_visibility")

        if "stats_visibility" in serializer.validated_data:
            player.stats_visibility = str(serializer.validated_data["stats_visibility"])
            update_fields.append("stats_visibility")

        if update_fields:
            player.save(update_fields=update_fields)

        return Response(PlayerSerializer(player, context={"request": request}).data)


class CurrentPlayerPushSubscriptionsAPIView(APIView):
    """Register/list/deactivate web push subscriptions for the current user.

    Endpoints:
        - GET    /api/player/me/push-subscriptions/
        - POST   /api/player/me/push-subscriptions/
        - DELETE /api/player/me/push-subscriptions/

    Notes:
        - Uses SessionAuthentication, so POST/DELETE require a valid CSRF token.
        - Subscriptions are stored per-user but `endpoint` is globally unique.

    """

    permission_classes: ClassVar[list[type[permissions.BasePermission]]] = [
        permissions.IsAuthenticated,
    ]
    parser_classes: ClassVar[list[type[Any]]] = [JSONParser]

    def get(
        self,
        request: Request,
        *args: Any,  # noqa: ANN401
        **kwargs: Any,  # noqa: ANN401
    ) -> Response:
        """List active push subscriptions for the current user."""
        subs = PlayerPushSubscription.objects.filter(
            user=request.user, is_active=True
        ).order_by("-updated_at")
        return Response(PlayerPushSubscriptionSerializer(subs, many=True).data)

    @transaction.atomic
    def post(
        self,
        request: Request,
        *args: Any,  # noqa: ANN401
        **kwargs: Any,  # noqa: ANN401
    ) -> Response:
        """Register (or upsert) a push subscription for the current user."""
        serializer = PlayerPushSubscriptionCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        subscription = serializer.validated_data["subscription"]
        if not isinstance(subscription, dict):
            return Response(
                {"detail": "Invalid subscription payload"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        endpoint = str(subscription.get("endpoint") or "").strip()
        user_agent = str(serializer.validated_data.get("user_agent") or "").strip()
        if not endpoint:
            return Response(
                {"detail": "subscription.endpoint is required"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        obj = PlayerPushSubscription.objects.filter(endpoint=endpoint).first()
        created = False

        if obj is None:
            obj = PlayerPushSubscription.objects.create(
                user=request.user,
                endpoint=endpoint,
                subscription=subscription,
                is_active=True,
                user_agent=user_agent,
            )
            created = True
        else:
            obj.user = request.user
            obj.subscription = subscription
            obj.is_active = True
            obj.user_agent = user_agent
            obj.save(
                update_fields=[
                    "user",
                    "subscription",
                    "is_active",
                    "user_agent",
                    "updated_at",
                ]
            )

        payload = PlayerPushSubscriptionSerializer(obj).data
        return Response(
            {"created": created, "subscription": payload},
            status=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
        )

    @transaction.atomic
    def delete(
        self,
        request: Request,
        *args: Any,  # noqa: ANN401
        **kwargs: Any,  # noqa: ANN401
    ) -> Response:
        """Deactivate a stored push subscription for the current user."""
        serializer = PlayerPushSubscriptionDeactivateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        endpoint = serializer.validated_data.get("endpoint")
        sub_id = serializer.validated_data.get("id_uuid")

        queryset = PlayerPushSubscription.objects.filter(user=request.user)
        if endpoint:
            queryset = queryset.filter(endpoint=endpoint)
        if sub_id:
            queryset = queryset.filter(id_uuid=sub_id)

        obj = queryset.first()
        if obj is None:
            return Response(
                {"detail": "Subscription not found"},
                status=status.HTTP_404_NOT_FOUND,
            )

        if obj.is_active:
            obj.is_active = False
            obj.save(update_fields=["is_active", "updated_at"])

        return Response(status=status.HTTP_204_NO_CONTENT)


class CurrentPlayerTestPushNotificationAPIView(APIView):
    """Send a test push notification to the current user's active subscriptions.

    Endpoint:
        - POST /api/player/me/push-subscriptions/test/

    Notes:
        - Staff/superusers only (intended for debug tooling).
        - Uses SessionAuthentication, so POST requires a valid CSRF token.

    """

    permission_classes: ClassVar[list[type[permissions.BasePermission]]] = [
        permissions.IsAuthenticated,
    ]

    @staticmethod
    def _is_staff_user(user: Any) -> bool:  # noqa: ANN401
        return bool(
            getattr(user, "is_staff", False) or getattr(user, "is_superuser", False)
        )

    @staticmethod
    def _missing_webpush_settings() -> list[str]:
        vapid_public = str(
            getattr(settings, "WEBPUSH_VAPID_PUBLIC_KEY", "") or ""
        ).strip()
        vapid_private = str(
            getattr(settings, "WEBPUSH_VAPID_PRIVATE_KEY", "") or ""
        ).strip()
        vapid_subject = str(
            getattr(settings, "WEBPUSH_VAPID_SUBJECT", "") or ""
        ).strip()

        return [
            name
            for name, value in [
                ("WEBPUSH_VAPID_PUBLIC_KEY", vapid_public),
                ("WEBPUSH_VAPID_PRIVATE_KEY", vapid_private),
                ("WEBPUSH_VAPID_SUBJECT", vapid_subject),
            ]
            if not value
        ]

    @staticmethod
    def _build_target_url() -> str:
        base_url = str(getattr(settings, "WEB_APP_ORIGIN", "") or "").rstrip("/")
        return f"{base_url}/profile" if base_url else "/profile"

    @staticmethod
    def _send_test_payload(
        *,
        subs: list[PlayerPushSubscription],
        payload: WebPushPayload,
    ) -> tuple[int, int, list[dict[str, Any]]]:
        sent = 0
        failed = 0
        errors: list[dict[str, Any]] = []

        for sub in subs:
            try:
                send_to_model_subscription(sub=sub, payload=payload)
                sent += 1
            except WebPushException as exc:  # pragma: no cover - debug endpoint
                failed += 1
                status_code = getattr(
                    getattr(exc, "response", None),
                    "status_code",
                    None,
                )
                # Avoid leaking subscription keys; the endpoint + id is enough.
                errors.append({
                    "subscription_id": str(sub.id_uuid),
                    "endpoint": str(sub.endpoint),
                    "status_code": status_code,
                    "detail": str(exc),
                })
            except Exception as exc:  # pragma: no cover - best-effort debug endpoint
                failed += 1
                logger.warning(
                    "Unexpected error while sending test web push to %s",
                    sub.id_uuid,
                    exc_info=True,
                )
                errors.append({
                    "subscription_id": str(sub.id_uuid),
                    "endpoint": str(sub.endpoint),
                    "detail": str(exc) or "Unexpected error",
                })

        return sent, failed, errors

    def post(
        self,
        request: Request,
        *args: Any,  # noqa: ANN401
        **kwargs: Any,  # noqa: ANN401
    ) -> Response:
        """Send a test push notification to the current user's subscriptions."""
        user = request.user
        if not self._is_staff_user(user):
            return Response(
                {"detail": "Staff only"},
                status=status.HTTP_403_FORBIDDEN,
            )

        missing = self._missing_webpush_settings()
        if missing:
            return Response(
                {
                    "detail": "Web push not configured",
                    "missing": missing,
                },
                status=status.HTTP_409_CONFLICT,
            )

        if not webpush_library_available():
            return Response(
                {
                    "detail": "Web push runtime is missing pywebpush",
                    "missing": ["pywebpush"],
                },
                status=status.HTTP_409_CONFLICT,
            )

        subs_qs = PlayerPushSubscription.objects.filter(
            user=user, is_active=True
        ).order_by("-updated_at")
        if not subs_qs.exists():
            return Response(
                {"detail": "No active push subscriptions"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        subs = list(subs_qs)

        payload = WebPushPayload(
            title="Test pushmelding",
            body="Als je dit ziet werkt push via de PWA.",
            url=self._build_target_url(),
            tag="debug-test",
        )

        total = len(subs)
        sent, failed, errors = self._send_test_payload(subs=subs, payload=payload)

        response_payload: dict[str, Any] = {
            "total": total,
            "sent": sent,
            "failed": failed,
        }
        if errors:
            # Keep this endpoint actionable even if a user has many devices.
            response_payload["errors"] = errors[:TEST_PUSH_ERROR_LIMIT]
            if len(errors) > TEST_PUSH_ERROR_LIMIT:
                response_payload["errors_truncated"] = True

        return Response(response_payload, status=status.HTTP_200_OK)


class PlayerOverviewAPIView(APIView):
    """Expose player-specific match data grouped by season."""

    permission_classes: ClassVar[list[type[permissions.BasePermission]]] = [
        permissions.AllowAny,
    ]

    def get(
        self,
        request: Request,
        player_id: str | None = None,
        *args: Any,  # noqa: ANN401
        **kwargs: Any,  # noqa: ANN401
    ) -> Response:
        """Return upcoming and recent matches for the requested player.

        Supports both the authenticated player (``/me/overview/``) and
        explicit player lookups (``/players/<uuid>/overview/``).
        """
        player = self._resolve_player(request, player_id)
        if player is None:
            return Response(PLAYER_NOT_FOUND_DETAIL, status=status.HTTP_404_NOT_FOUND)

        # Restrict viewing other players' match history when configured.
        if player_id:
            viewer = (
                Player.objects.filter(user=request.user).first()
                if request.user.is_authenticated
                else None
            )
            if not can_view_by_visibility(
                visibility=player.stats_visibility,
                viewer=viewer,
                target=player,
            ):
                return Response(
                    PRIVATE_ACCOUNT_DETAIL,
                    status=status.HTTP_403_FORBIDDEN,
                )

        seasons_qs = list(self._player_seasons_queryset(player))
        season = self._resolve_season(request, seasons_qs)

        upcoming_matches = build_match_summaries(
            self
            ._match_queryset_for_player(player, season, include_roster=True)
            .filter(status__in=["upcoming", "active"])
            .order_by("match_link__start_time")[:10]
        )

        recent_matches = build_match_summaries(
            self
            ._match_queryset_for_player(player, season, include_roster=False)
            .filter(status="finished")
            .order_by("-match_link__start_time")[:10]
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
            "matches": {
                "upcoming": upcoming_matches,
                "recent": recent_matches,
            },
            "seasons": seasons_payload,
            "meta": {
                "season_id": str(season.id_uuid) if season else None,
                "season_name": season.name if season else None,
            },
        }

        return Response(payload)

    @staticmethod
    def _resolve_player(request: Request, player_id: str | None) -> Player | None:
        if player_id:
            return (
                Player.objects
                .select_related("user")
                .prefetch_related(
                    "team_follow",
                    "club_follow",
                    "member_clubs",
                    "club_membership_links",
                    "club_membership_links__club",
                )
                .filter(id_uuid=player_id)
                .first()
            )

        return _get_current_player(request)

    @staticmethod
    def _match_queryset_for_player(
        player: Player,
        season: Season | None,
        *,
        include_roster: bool,
    ) -> QuerySet[MatchData]:
        # IMPORTANT:
        # Avoid OR-of-joins filters here. They can create huge join explosions and
        # force DISTINCT, which is exactly how we ended up with multi-second
        # queries on PlayerOverview.
        #
        # Use EXISTS subqueries instead. This keeps the main query small and
        # lets Postgres use indexes on the referenced tables.
        queryset = MatchData.objects.select_related(
            "match_link",
            "match_link__home_team",
            "match_link__home_team__club",
            "match_link__away_team",
            "match_link__away_team__club",
            "match_link__season",
        )

        # A player is considered a participant if they appear in a PlayerGroup
        # (tracking) or have any Shot rows for the match.
        queryset = queryset.annotate(
            has_player_group=Exists(
                PlayerGroup.objects.filter(
                    match_data=OuterRef("pk"),
                    players=player,
                )
            ),
            has_shot=Exists(
                Shot.objects.filter(
                    match_data=OuterRef("pk"),
                    player=player,
                )
            ),
        )

        filter_q = Q(has_player_group=True) | Q(has_shot=True)

        if include_roster:
            # Prefer the per-match roster table when present.
            queryset = queryset.annotate(
                is_match_roster=Exists(
                    MatchPlayer.objects.filter(
                        match_data=OuterRef("pk"),
                        player=player,
                    )
                ),
                is_home_teamdata_roster=Exists(
                    TeamData.objects.filter(
                        team_id=OuterRef("match_link__home_team_id"),
                        season_id=OuterRef("match_link__season_id"),
                        players=player,
                    )
                ),
                is_away_teamdata_roster=Exists(
                    TeamData.objects.filter(
                        team_id=OuterRef("match_link__away_team_id"),
                        season_id=OuterRef("match_link__season_id"),
                        players=player,
                    )
                ),
            )
            filter_q |= (
                Q(is_match_roster=True)
                | Q(is_home_teamdata_roster=True)
                | Q(is_away_teamdata_roster=True)
            )
        else:
            # Keep annotations stable for callers / debug tooling.
            queryset = queryset.annotate(
                is_match_roster=Value(False, output_field=BooleanField()),
                is_home_teamdata_roster=Value(False, output_field=BooleanField()),
                is_away_teamdata_roster=Value(False, output_field=BooleanField()),
            )

        queryset = queryset.filter(filter_q)

        if season:
            queryset = queryset.filter(match_link__season=season)

        return queryset

    @staticmethod
    def _player_seasons_queryset(player: Player) -> QuerySet[Season]:
        # IMPORTANT:
        # Avoid a single big OR-of-joins query here.
        # That pattern creates join explosions (and forces DISTINCT) which can
        # become extremely slow on real datasets.
        #
        # Instead, collect candidate season IDs from each relevant source and
        # UNION them. Postgres can execute these subqueries using indexes.
        season_ids = TeamData.objects.filter(players=player).values_list(
            "season_id",
            flat=True,
        )
        season_ids = season_ids.union(
            MatchPlayer.objects.filter(player=player).values_list(
                "match_data__match_link__season_id",
                flat=True,
            ),
            PlayerGroup.objects.filter(players=player).values_list(
                "match_data__match_link__season_id",
                flat=True,
            ),
            Shot.objects.filter(player=player).values_list(
                "match_data__match_link__season_id",
                flat=True,
            ),
        )

        return Season.objects.filter(id_uuid__in=Subquery(season_ids)).order_by(
            "-start_date"
        )

    def _resolve_season(self, request: Request, seasons: list[Season]) -> Season | None:
        season_param = request.query_params.get("season")
        if season_param:
            return next(
                (option for option in seasons if str(option.id_uuid) == season_param),
                None,
            )

        if not seasons:
            return None

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


class PlayerConnectedClubRecentResultsAPIView(APIView):
    """Return recent finished matches for the current player's followed clubs.

    This endpoint exists because the player overview endpoint is scoped to matches
    where the player participated (or is on the roster). For the Home 'Updates'
    widget we want the latest results for the player's connected clubs.

    Query params:
        - limit: int (default 3, max 10)
        - days: int (optional; when provided, only matches within the last N days)
        - season: UUID (optional; restrict to a season)
        - player_id: UUID (debug-only; supported via _get_current_player)
    """

    permission_classes: ClassVar[list[type[permissions.BasePermission]]] = [
        permissions.AllowAny,
    ]

    def get(
        self,
        request: Request,
        *args: Any,  # noqa: ANN401
        **kwargs: Any,  # noqa: ANN401
    ) -> Response:
        """Return match summaries for the player's followed clubs."""
        player = _get_current_player(request)
        if player is None:
            return Response(PLAYER_NOT_FOUND_DETAIL, status=status.HTTP_404_NOT_FOUND)

        try:
            limit = int(request.query_params.get("limit", "3"))
        except (TypeError, ValueError):
            limit = 3
        limit = max(1, min(limit, 10))

        days_param = request.query_params.get("days")
        days: int | None
        if not days_param:
            days = None
        else:
            try:
                days = int(days_param)
            except (TypeError, ValueError):
                days = None
            if days is not None and days <= 0:
                days = None

        season_id = request.query_params.get("season")

        clubs_qs = player.club_follow.all()
        if not clubs_qs.exists():
            return Response([])

        queryset = (
            MatchData.objects
            .select_related(
                "match_link",
                "match_link__home_team",
                "match_link__home_team__club",
                "match_link__away_team",
                "match_link__away_team__club",
                "match_link__season",
            )
            .filter(
                status="finished",
            )
            .filter(
                Q(match_link__home_team__club__in=clubs_qs)
                | Q(match_link__away_team__club__in=clubs_qs)
            )
            .distinct()
        )

        if season_id:
            queryset = queryset.filter(match_link__season_id=season_id)

        if days is not None:
            cutoff = timezone.now() - timedelta(days=days)
            queryset = queryset.filter(match_link__start_time__gte=cutoff)

        matches = build_match_summaries(
            queryset.order_by("-match_link__start_time")[:limit]
        )
        return Response(matches)


class PlayerStatsAPIView(APIView):
    """Expose season-scoped player shooting and scoring stats."""

    permission_classes: ClassVar[list[type[permissions.BasePermission]]] = [
        permissions.AllowAny,
    ]

    def get(
        self,
        request: Request,
        player_id: str | None = None,
        *args: Any,  # noqa: ANN401
        **kwargs: Any,  # noqa: ANN401
    ) -> Response:
        """Return aggregate stats for a player in a season."""
        player = self._resolve_player(request, player_id)
        if player is None:
            return Response(PLAYER_NOT_FOUND_DETAIL, status=status.HTTP_404_NOT_FOUND)

        # Restrict viewing other players' season stats when configured.
        if player_id:
            viewer = (
                Player.objects.filter(user=request.user).first()
                if request.user.is_authenticated
                else None
            )
            if not can_view_by_visibility(
                visibility=player.stats_visibility,
                viewer=viewer,
                target=player,
            ):
                return Response(
                    PRIVATE_ACCOUNT_DETAIL,
                    status=status.HTTP_403_FORBIDDEN,
                )

        seasons_qs = list(self._player_seasons_queryset(player))
        season = self._resolve_season(request, seasons_qs)

        mvp_queryset = MatchMvp.objects.filter(
            mvp_player=player,
            published_at__isnull=False,
        )
        if season:
            mvp_queryset = mvp_queryset.filter(match__season=season)

        mvp_match_ids = list(mvp_queryset.values_list("match_id", flat=True))
        mvp_matches: list[dict[str, Any]] = []
        if mvp_match_ids:
            mvp_matchdata_queryset = (
                MatchData.objects
                .select_related(
                    "match_link",
                    "match_link__home_team",
                    "match_link__home_team__club",
                    "match_link__away_team",
                    "match_link__away_team__club",
                    "match_link__season",
                )
                .filter(
                    status="finished",
                    match_link_id__in=mvp_match_ids,
                )
                .distinct()
            )
            mvp_matches = build_match_summaries(
                mvp_matchdata_queryset.order_by("-match_link__start_time")
            )

        shot_queryset = Shot.objects.select_related("match_data", "shot_type").filter(
            player=player
        )
        if season:
            shot_queryset = shot_queryset.filter(match_data__match_link__season=season)

        aggregated = shot_queryset.aggregate(
            shots_for=Count("id_uuid", filter=Q(for_team=True)),
            shots_against=Count("id_uuid", filter=Q(for_team=False)),
            goals_for=Count("id_uuid", filter=Q(for_team=True, scored=True)),
            goals_against=Count("id_uuid", filter=Q(for_team=False, scored=True)),
        )

        goal_types_for = self._goal_type_breakdown(shot_queryset, for_team=True)
        goal_types_against = self._goal_type_breakdown(shot_queryset, for_team=False)

        payload = {
            "shots_for": int(aggregated.get("shots_for", 0)),
            "shots_against": int(aggregated.get("shots_against", 0)),
            "goals_for": int(aggregated.get("goals_for", 0)),
            "goals_against": int(aggregated.get("goals_against", 0)),
            "mvps": int(mvp_queryset.count()),
            "mvp_matches": mvp_matches,
            "goal_types": {
                "for": goal_types_for,
                "against": goal_types_against,
            },
        }

        return Response(payload)

    @staticmethod
    def _resolve_player(request: Request, player_id: str | None) -> Player | None:
        if player_id:
            return (
                Player.objects.select_related("user").filter(id_uuid=player_id).first()
            )

        return _get_current_player(request)

    def _resolve_season(self, request: Request, seasons: list[Season]) -> Season | None:
        season_param = request.query_params.get("season")
        if season_param:
            return next(
                (option for option in seasons if str(option.id_uuid) == season_param),
                None,
            )

        if not seasons:
            return None

        current = self._current_season()
        if current and any(option.id_uuid == current.id_uuid for option in seasons):
            return current

        return seasons[0]

    @staticmethod
    def _player_seasons_queryset(player: Player) -> QuerySet[Season]:
        # Keep this in sync with PlayerOverviewAPIView._player_seasons_queryset.
        season_ids = TeamData.objects.filter(players=player).values_list(
            "season_id",
            flat=True,
        )
        season_ids = season_ids.union(
            MatchPlayer.objects.filter(player=player).values_list(
                "match_data__match_link__season_id",
                flat=True,
            ),
            PlayerGroup.objects.filter(players=player).values_list(
                "match_data__match_link__season_id",
                flat=True,
            ),
            Shot.objects.filter(player=player).values_list(
                "match_data__match_link__season_id",
                flat=True,
            ),
        )

        return Season.objects.filter(id_uuid__in=Subquery(season_ids)).order_by(
            "-start_date"
        )

    def _current_season(self) -> Season | None:
        today = timezone.now().date()
        return Season.objects.filter(
            start_date__lte=today,
            end_date__gte=today,
        ).first()

    @staticmethod
    def _goal_type_breakdown(
        queryset: QuerySet[Shot], *, for_team: bool
    ) -> list[dict[str, str | int | None]]:
        breakdown = (
            queryset
            .filter(for_team=for_team, scored=True)
            .values("shot_type__id_uuid", "shot_type__name")
            .annotate(count=Count("id_uuid"))
            .order_by("shot_type__name")
        )

        return [
            {
                "id_uuid": str(row.get("shot_type__id_uuid"))
                if row.get("shot_type__id_uuid")
                else None,
                "name": row.get("shot_type__name") or "Onbekend",
                "count": int(row.get("count", 0)),
            }
            for row in breakdown
        ]


class CurrentPlayerGoalSongAPIView(APIView):
    """Update goal song configuration for the current player.

    Payload:
        - goal_song_uri: str | null (HTTP audio URL or Spotify track URI)
        - song_start_time: int | null (seconds)
        - goal_song_song_ids: list[str] | null (PlayerSong UUIDs; cycles through them)
    """

    permission_classes: ClassVar[list[type[permissions.BasePermission]]] = [
        permissions.IsAuthenticated,
    ]

    @dataclass(frozen=True, slots=True)
    class _ParsedPatchPayload:
        goal_song_uri_provided: bool
        goal_song_uri: str | None
        song_start_time_provided: bool
        song_start_time: int | None
        goal_song_ids_provided: bool
        goal_song_song_ids: list[str] | None

    @staticmethod
    def _parse_optional_string(
        payload: Mapping[str, object],
        key: str,
    ) -> tuple[bool, str | None, str | None]:
        if key not in payload:
            return False, None, None

        raw = payload.get(key)
        if raw is None:
            return True, "", None
        if isinstance(raw, str):
            return True, raw.strip(), None
        return True, None, f"{key} must be a string or null"

    @staticmethod
    def _parse_optional_non_negative_int(
        payload: Mapping[str, object],
        key: str,
    ) -> tuple[bool, int | None, str | None]:
        if key not in payload:
            return False, None, None

        raw = payload.get(key)
        if raw in {None, ""}:
            return True, None, None
        if isinstance(raw, bool):
            return True, None, f"{key} must be a number or null"

        try:
            if isinstance(raw, (int, float, str)):
                value = int(float(raw))
            else:
                raise TypeError
        except (TypeError, ValueError):
            return True, None, f"{key} must be a number or null"

        return True, max(0, value), None

    @staticmethod
    def _parse_optional_uuid_list(
        payload: Mapping[str, object],
        key: str,
    ) -> tuple[bool, list[str] | None, str | None]:
        if key not in payload:
            return False, None, None

        raw = payload.get(key)
        if raw is None:
            return True, [], None

        if not isinstance(raw, list):
            return True, None, f"{key} must be a list of strings or null"

        items: list[str] = []
        for entry in raw:
            if not isinstance(entry, str):
                return True, None, f"{key} must be a list of strings"
            value = entry.strip()
            if not value:
                continue
            items.append(value)

        # De-duplicate while preserving order.
        seen: set[str] = set()
        deduped: list[str] = []
        for item in items:
            if item in seen:
                continue
            seen.add(item)
            deduped.append(item)

        return True, deduped, None

    @staticmethod
    def _validate_goal_song_ids(
        player: Player,
        ids: list[str],
    ) -> tuple[list[PlayerSong] | None, Response | None]:
        if not ids:
            return [], None

        songs = list(
            PlayerSong.objects.select_related("cached_song").filter(
                player=player,
                id_uuid__in=ids,
            )
        )
        by_id = {str(song.id_uuid): song for song in songs}

        missing = [song_id for song_id in ids if song_id not in by_id]
        if missing:
            return (
                None,
                Response(
                    {"detail": "Unknown song id(s)", "missing": missing},
                    status=status.HTTP_400_BAD_REQUEST,
                ),
            )

        ordered = [by_id[song_id] for song_id in ids]
        not_ready: list[str] = []
        for song in ordered:
            cached = song.cached_song
            status_value = cached.status if cached is not None else song.status
            audio_file = cached.audio_file if cached is not None else song.audio_file
            if status_value != PlayerSongStatus.READY or not audio_file:
                not_ready.append(str(song.id_uuid))
        if not_ready:
            return (
                None,
                Response(
                    {
                        "detail": "Song(s) not ready",
                        "not_ready": not_ready,
                    },
                    status=status.HTTP_400_BAD_REQUEST,
                ),
            )

        return ordered, None

    @classmethod
    def _apply_goal_song_song_ids(
        cls,
        *,
        player: Player,
        ids: list[str],
    ) -> tuple[list[str] | None, Response | None]:
        ordered, error = cls._validate_goal_song_ids(player, ids)
        if error is not None:
            return None, error

        update_fields: list[str] = ["goal_song_song_ids"]
        player.goal_song_song_ids = ids

        if ordered:
            first = ordered[0]
            audio_file = (
                first.cached_song.audio_file
                if first.cached_song is not None
                else first.audio_file
            )
            if audio_file:
                player.goal_song_uri = audio_file.url  # type: ignore[no-any-return]
                update_fields.append("goal_song_uri")
            player.song_start_time = first.start_time_seconds
            update_fields.append("song_start_time")
            return update_fields, None

        # Clearing selection
        player.goal_song_uri = ""
        player.song_start_time = None
        update_fields.extend(["goal_song_uri", "song_start_time"])
        return update_fields, None

    @classmethod
    def _parse_patch_payload(
        cls,
        data: Mapping[str, object],
    ) -> tuple[_ParsedPatchPayload | None, Response | None]:
        goal_song_uri_provided, goal_song_uri, goal_song_uri_error = (
            cls._parse_optional_string(data, "goal_song_uri")
        )
        if goal_song_uri_error:
            return (
                None,
                Response(
                    {"detail": goal_song_uri_error},
                    status=status.HTTP_400_BAD_REQUEST,
                ),
            )

        (
            song_start_time_provided,
            song_start_time,
            song_start_time_error,
        ) = cls._parse_optional_non_negative_int(data, "song_start_time")
        if song_start_time_error:
            return (
                None,
                Response(
                    {"detail": song_start_time_error},
                    status=status.HTTP_400_BAD_REQUEST,
                ),
            )

        (
            goal_song_ids_provided,
            goal_song_song_ids,
            goal_song_ids_error,
        ) = cls._parse_optional_uuid_list(data, "goal_song_song_ids")
        if goal_song_ids_error:
            return (
                None,
                Response(
                    {"detail": goal_song_ids_error},
                    status=status.HTTP_400_BAD_REQUEST,
                ),
            )

        return (
            cls._ParsedPatchPayload(
                goal_song_uri_provided=goal_song_uri_provided,
                goal_song_uri=goal_song_uri,
                song_start_time_provided=song_start_time_provided,
                song_start_time=song_start_time,
                goal_song_ids_provided=goal_song_ids_provided,
                goal_song_song_ids=goal_song_song_ids,
            ),
            None,
        )

    def patch(
        self,
        request: Request,
        *args: Any,  # noqa: ANN401
        **kwargs: Any,  # noqa: ANN401
    ) -> Response:
        """Update goal-song configuration for the authenticated player."""
        player = Player.objects.select_related("user").filter(user=request.user).first()
        if player is None:
            return Response(PLAYER_NOT_FOUND_DETAIL, status=status.HTTP_404_NOT_FOUND)

        if not isinstance(request.data, Mapping):
            return Response(
                {"detail": "Invalid payload"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        parsed, error = self._parse_patch_payload(request.data)
        if error is not None:
            return error
        if parsed is None:
            return Response(
                {"detail": "Invalid payload"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        update_fields: list[str] = []
        if parsed.goal_song_uri_provided:
            player.goal_song_uri = parsed.goal_song_uri or ""
            update_fields.append("goal_song_uri")
        if parsed.song_start_time_provided:
            player.song_start_time = parsed.song_start_time
            update_fields.append("song_start_time")

        if parsed.goal_song_ids_provided:
            fields, error = self._apply_goal_song_song_ids(
                player=player,
                ids=parsed.goal_song_song_ids or [],
            )
            if error is not None:
                return error
            update_fields.extend(fields or [])

        if update_fields:
            # De-duplicate without reordering fields.
            deduped_fields = list(dict.fromkeys(update_fields))
            player.save(update_fields=deduped_fields)

        return Response(PlayerSerializer(player, context={"request": request}).data)


class UploadProfilePictureAPIView(APIView):
    """Upload a profile picture (API variant used by the Vite frontend)."""

    permission_classes: ClassVar[list[type[permissions.BasePermission]]] = [
        permissions.IsAuthenticated,
    ]
    parser_classes: ClassVar[list[type[Any]]] = [MultiPartParser, FormParser]

    def post(
        self,
        request: Request,
        *args: Any,  # noqa: ANN401
        **kwargs: Any,  # noqa: ANN401
    ) -> Response:
        """Upload and persist a profile picture for the authenticated player."""
        files = request.FILES.getlist("profile_picture")
        if not files:
            return Response(
                {"error": "No profile_picture uploaded"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        uploaded = files[0]
        if not (isinstance(uploaded, UploadedFile) or hasattr(uploaded, "name")):
            return Response(
                {"error": "Invalid uploaded file"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            player = Player.objects.get(user=request.user)
        except Player.DoesNotExist:
            return Response(
                {"error": PLAYER_NOT_FOUND_MESSAGE},
                status=status.HTTP_404_NOT_FOUND,
            )

        filename = getattr(uploaded, "name", "profile_picture")
        player.profile_picture.save(filename, uploaded)
        return Response({"url": player.get_profile_picture()})


class UploadGoalSongAPIView(APIView):
    """Upload an audio file to use as the goal song.

    The uploaded file is stored using Django's default storage (S3/MinIO in prod,
    filesystem in local dev). The resulting URL is stored in `Player.goal_song_uri`.
    """

    permission_classes: ClassVar[list[type[permissions.BasePermission]]] = [
        permissions.IsAuthenticated,
    ]
    parser_classes: ClassVar[list[type[Any]]] = [MultiPartParser, FormParser]

    def post(
        self,
        request: Request,
        *args: Any,  # noqa: ANN401
        **kwargs: Any,  # noqa: ANN401
    ) -> Response:
        """Upload an audio file and store its URL on the authenticated player."""
        files = request.FILES.getlist("goal_song")
        if not files:
            return Response(
                {"error": "No goal_song uploaded"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        uploaded = files[0]
        if not (isinstance(uploaded, UploadedFile) or hasattr(uploaded, "name")):
            return Response(
                {"error": "Invalid uploaded file"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        content_type = getattr(uploaded, "content_type", "") or ""
        allowed_types = {
            "audio/mpeg",
            "audio/mp3",
            "audio/wav",
            "audio/x-wav",
            "audio/ogg",
            "audio/mp4",
            "audio/x-m4a",
        }
        if content_type and content_type.lower() not in allowed_types:
            return Response(
                {
                    "error": "Unsupported audio type",
                    "content_type": content_type,
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            player = Player.objects.get(user=request.user)
        except Player.DoesNotExist:
            return Response(
                {"error": PLAYER_NOT_FOUND_MESSAGE},
                status=status.HTTP_404_NOT_FOUND,
            )

        filename = (getattr(uploaded, "name", "goal_song") or "goal_song").strip()
        safe_name = "".join(
            ch for ch in filename if ch.isalnum() or ch in {".", "-", "_"}
        )
        if not safe_name:
            safe_name = "goal_song"

        # We only ever play ~8 seconds in the UI, so store a clipped variant to
        # avoid slow downloads on poor connections.
        _, url = _store_goal_song_upload_best_effort(
            player=player,
            uploaded=uploaded,
            safe_name=safe_name,
            clip_duration_seconds=8,
        )

        # NOTE: goal_song_uri is capped at 255 chars. If you ever see this break,
        # switch to storing a key and exposing a computed URL instead.
        player.goal_song_uri = url
        player.save(update_fields=["goal_song_uri"])

        return Response({
            "url": url,
            "player": PlayerSerializer(player, context={"request": request}).data,
        })


class PlayerSongClipAPIView(APIView):
    """Return (and cache) an 8-second clip for a PlayerSong.

    Why:
        Goal songs only play briefly in the UI, but the browser used to download
        the *entire* track before it could play reliably on slower connections.

    Behavior:
        - Generates and stores a deterministic clip in the configured storage.
        - Responds with a redirect to that stored clip URL.
        - If clip generation fails, redirects to the full audio URL as fallback.

    Query params:
        - start: int (seconds; default 0)
        - duration: int (seconds; default 8, max 15)
    """

    permission_classes: ClassVar[list[type[permissions.BasePermission]]] = [
        permissions.AllowAny,
    ]

    @staticmethod
    def _parse_seconds_query(request: Request, key: str, default: int) -> int:
        raw = request.query_params.get(key)
        if not raw:
            return default
        try:
            return int(float(raw))
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _clip_or_full_location(
        *,
        audio_file: Any,  # noqa: ANN401
        song: PlayerSong,
        start_seconds: int,
        duration_seconds: int,
    ) -> str:
        # v2: the original clip generation could accidentally inherit source
        # metadata like TLEN (full track length), making players report the
        # duration as the full song even when audio data is only ~8 seconds.
        # Bump the cache key to force regeneration of existing stored clips.
        clip_key = (
            f"song_clips_v2/{song.id_uuid}/"
            f"start_{start_seconds}_dur_{duration_seconds}.mp3"
        )

        ffmpeg_path = shutil.which("ffmpeg")
        if not ffmpeg_path:
            return str(audio_file.url)

        try:
            if default_storage.exists(clip_key):
                return default_storage.url(clip_key)

            with tempfile.TemporaryDirectory(prefix="song_clip_") as tmpdir:
                input_path = os.path.join(tmpdir, "input")
                output_path = os.path.join(tmpdir, "clip.mp3")

                with audio_file.open("rb") as source, open(input_path, "wb") as dest:
                    while True:
                        chunk = source.read(1024 * 1024)
                        if not chunk:
                            break
                        dest.write(chunk)

                subprocess.run(
                    [
                        ffmpeg_path,
                        "-hide_banner",
                        "-loglevel",
                        "error",
                        "-y",
                        "-ss",
                        str(start_seconds),
                        "-i",
                        input_path,
                        "-t",
                        str(duration_seconds),
                        "-vn",
                        # Prevent inheriting source tags like TLEN (full-track
                        # length), which can confuse duration reporting.
                        "-map_metadata",
                        "-1",
                        "-map_chapters",
                        "-1",
                        "-acodec",
                        "libmp3lame",
                        "-q:a",
                        "4",
                        output_path,
                    ],
                    check=True,
                )  # nosec B603

                clip_bytes = Path(output_path).read_bytes()
                default_storage.save(
                    clip_key,
                    ContentFile(clip_bytes),
                )
            return default_storage.url(clip_key)
        except (FileNotFoundError, subprocess.CalledProcessError):
            logger.info(
                "Clip generation failed; falling back to full audio for %s",
                song.id_uuid,
                exc_info=True,
            )
            return str(audio_file.url)
        except Exception:
            logger.warning(
                "Unexpected error generating clip; falling back to full audio for %s",
                song.id_uuid,
                exc_info=True,
            )
            return str(audio_file.url)

    def get(
        self,
        request: Request,
        song_id: str,
        *args: Any,  # noqa: ANN401
        **kwargs: Any,  # noqa: ANN401
    ) -> HttpResponseRedirect:
        """Redirect to an 8-second clip URL for the requested song."""
        start_seconds = max(0, self._parse_seconds_query(request, "start", 0))
        duration_seconds = self._parse_seconds_query(request, "duration", 8)
        duration_seconds = max(1, min(15, duration_seconds))

        song = (
            PlayerSong.objects
            .select_related("cached_song")
            .filter(id_uuid=song_id)
            .first()
        )
        if song is None:
            return HttpResponseRedirect("/")

        audio_file = (
            song.cached_song.audio_file
            if song.cached_song is not None
            else song.audio_file
        )
        if not audio_file:
            return HttpResponseRedirect("/")

        location = self._clip_or_full_location(
            audio_file=audio_file,
            song=song,
            start_seconds=start_seconds,
            duration_seconds=duration_seconds,
        )
        return HttpResponseRedirect(location)


class CurrentPlayerSongsAPIView(APIView):
    """List and create downloaded songs for the authenticated player."""

    permission_classes: ClassVar[list[type[permissions.BasePermission]]] = [
        permissions.IsAuthenticated,
    ]

    parser_classes: ClassVar[list[type[object]]] = [
        JSONParser,
        FormParser,
        MultiPartParser,
    ]

    def get(
        self,
        request: Request,
        *args: Any,  # noqa: ANN401
        **kwargs: Any,  # noqa: ANN401
    ) -> Response:
        """Return the current player's downloaded songs."""
        player = _get_current_player(request)
        if player is None:
            return Response(PLAYER_NOT_FOUND_DETAIL, status=status.HTTP_404_NOT_FOUND)

        songs = (
            PlayerSong.objects
            .select_related("cached_song")
            .filter(player=player)
            .order_by("-created_at")
        )
        return Response(PlayerSongSerializer(songs, many=True).data)

    def post(
        self,
        request: Request,
        *args: Any,  # noqa: ANN401
        **kwargs: Any,  # noqa: ANN401
    ) -> Response:
        """Create a new song download request for the current player."""
        player = _get_current_player(request)
        if player is None:
            return Response(PLAYER_NOT_FOUND_DETAIL, status=status.HTTP_404_NOT_FOUND)

        serializer = PlayerSongCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        uploaded_audio = serializer.validated_data.get("audio_file")
        if isinstance(uploaded_audio, UploadedFile):
            # Uploaded MP3: store directly and mark ready.
            filename = os.path.basename(uploaded_audio.name or "uploaded.mp3")
            title = os.path.splitext(filename)[0][:255]

            song = PlayerSong.objects.create(
                player=player,
                cached_song=None,
                spotify_url="",
                title=title,
                artists="",
                duration_seconds=None,
                start_time_seconds=0,
                playback_speed=1.0,
                status=PlayerSongStatus.READY,
                error_message="",
                audio_file=uploaded_audio,
            )

            return Response(
                PlayerSongSerializer(song).data,
                status=status.HTTP_201_CREATED,
            )

        raw_url = str(serializer.validated_data.get("spotify_url") or "").strip()
        spotify_url = canonicalize_spotify_track_url(raw_url)

        cached, _ = CachedSong.objects.get_or_create(spotify_url=spotify_url)
        song, created = PlayerSong.objects.get_or_create(
            player=player,
            cached_song=cached,
            defaults={"spotify_url": spotify_url},
        )

        try:
            if getattr(settings, "CELERY_TASK_ALWAYS_EAGER", False) or getattr(
                settings, "TESTING", False
            ):
                download_cached_song.apply(args=[str(cached.id_uuid)])
            else:
                download_cached_song.delay(str(cached.id_uuid))
        except KombuOperationalError:
            logger.warning(
                "Celery broker unavailable; could not enqueue PlayerSong %s",
                song.id_uuid,
                exc_info=True,
            )
            cached.status = CachedSongStatus.FAILED
            cached.error_message = CELERY_BROKER_UNAVAILABLE_MESSAGE
            cached.save(update_fields=["status", "error_message", "updated_at"])

        return Response(
            PlayerSongSerializer(song).data,
            status=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
        )


def _remove_deleted_song_from_goal_song_selection(
    *,
    player: Player,
    deleted_song_id: str,
) -> None:
    """Remove a deleted song from a player's goal-song selection (best-effort).

    This keeps `goal_song_song_ids` consistent and updates the legacy fields
    (`goal_song_uri`, `song_start_time`) to match the first remaining selection.
    """
    current_ids = [sid for sid in (player.goal_song_song_ids or []) if sid]
    next_ids = [sid for sid in current_ids if sid != deleted_song_id]
    if next_ids == current_ids:
        return

    player.goal_song_song_ids = next_ids
    update_fields: list[str] = [
        "goal_song_song_ids",
        "goal_song_uri",
        "song_start_time",
    ]

    if not next_ids:
        player.goal_song_uri = ""
        player.song_start_time = None
        player.save(update_fields=update_fields)
        return

    first = (
        PlayerSong.objects
        .select_related("cached_song")
        .filter(player=player, id_uuid=next_ids[0])
        .only(
            "id_uuid",
            "start_time_seconds",
            "audio_file",
            "cached_song__audio_file",
        )
        .first()
    )
    audio_file = None
    if first is not None:
        audio_file = (
            first.cached_song.audio_file
            if first.cached_song is not None
            else first.audio_file
        )
    if audio_file:
        player.goal_song_uri = audio_file.url  # type: ignore[no-any-return]
    else:
        player.goal_song_uri = ""
    player.song_start_time = first.start_time_seconds if first is not None else None
    player.save(update_fields=update_fields)


class CurrentPlayerSongDetailAPIView(APIView):
    """Update a specific song for the authenticated player."""

    permission_classes: ClassVar[list[type[permissions.BasePermission]]] = [
        permissions.IsAuthenticated,
    ]

    def patch(
        self,
        request: Request,
        song_id: str,
        *args: Any,  # noqa: ANN401
        **kwargs: Any,  # noqa: ANN401
    ) -> Response:
        """Update per-song playback settings for a specific downloaded song."""
        player = _get_current_player(request)
        if player is None:
            return Response(PLAYER_NOT_FOUND_DETAIL, status=status.HTTP_404_NOT_FOUND)

        song = PlayerSong.objects.filter(player=player, id_uuid=song_id).first()
        if song is None:
            return Response(SONG_NOT_FOUND_DETAIL, status=status.HTTP_404_NOT_FOUND)

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

        return Response(PlayerSongSerializer(song).data)

    def delete(
        self,
        request: Request,
        song_id: str,
        *args: Any,  # noqa: ANN401
        **kwargs: Any,  # noqa: ANN401
    ) -> Response:
        """Delete a specific downloaded song."""
        player = _get_current_player(request)
        if player is None:
            return Response(PLAYER_NOT_FOUND_DETAIL, status=status.HTTP_404_NOT_FOUND)

        song = PlayerSong.objects.filter(player=player, id_uuid=song_id).first()
        if song is None:
            return Response(SONG_NOT_FOUND_DETAIL, status=status.HTTP_404_NOT_FOUND)

        song_id_str = str(song.id_uuid)
        with transaction.atomic():
            _remove_deleted_song_from_goal_song_selection(
                player=player,
                deleted_song_id=song_id_str,
            )
            song.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


def _enqueue_download_for_player_song(song: PlayerSong) -> None:
    """Enqueue (or eagerly execute) the download for a PlayerSong.

    If `song.cached_song` is set, the shared CachedSong download is queued.
    """
    cached = song.cached_song
    always_eager = getattr(settings, "CELERY_TASK_ALWAYS_EAGER", False) or getattr(
        settings, "TESTING", False
    )

    if always_eager:
        if cached is not None:
            download_cached_song.apply(args=[str(cached.id_uuid)])
        else:
            download_player_song.apply(args=[str(song.id_uuid)])
    elif cached is not None:
        download_cached_song.delay(str(cached.id_uuid))
    else:
        download_player_song.delay(str(song.id_uuid))


class CurrentPlayerSongRetryAPIView(APIView):
    """Retry downloading a failed song for the authenticated player."""

    permission_classes: ClassVar[list[type[permissions.BasePermission]]] = [
        permissions.IsAuthenticated,
    ]

    def post(
        self,
        request: Request,
        song_id: str,
        *args: Any,  # noqa: ANN401
        **kwargs: Any,  # noqa: ANN401
    ) -> Response:
        """Reset song status and re-enqueue download task."""
        player = _get_current_player(request)
        if player is None:
            return Response(PLAYER_NOT_FOUND_DETAIL, status=status.HTTP_404_NOT_FOUND)

        song = PlayerSong.objects.filter(player=player, id_uuid=song_id).first()
        if song is None:
            return Response(
                {"detail": "Song not found"},
                status=status.HTTP_404_NOT_FOUND,
            )

        cached = song.cached_song
        effective_status = cached.status if cached is not None else song.status
        if effective_status == PlayerSongStatus.READY:
            return Response(
                {"detail": "Song is already ready"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        with transaction.atomic():
            if cached is not None:
                cached.status = CachedSongStatus.QUEUED
                cached.error_message = ""
                cached.save(update_fields=["status", "error_message", "updated_at"])
            else:
                song.status = PlayerSongStatus.QUEUED
                song.error_message = ""
                song.save(update_fields=["status", "error_message", "updated_at"])

        try:
            _enqueue_download_for_player_song(song)
        except KombuOperationalError:
            logger.warning(
                "Celery broker unavailable; could not retry PlayerSong %s",
                song.id_uuid,
                exc_info=True,
            )
            if cached is not None:
                cached.status = CachedSongStatus.FAILED
                cached.error_message = CELERY_BROKER_UNAVAILABLE_MESSAGE
                cached.save(update_fields=["status", "error_message", "updated_at"])
            else:
                song.status = PlayerSongStatus.FAILED
                song.error_message = CELERY_BROKER_UNAVAILABLE_MESSAGE
                song.save(update_fields=["status", "error_message", "updated_at"])

        return Response(PlayerSongSerializer(song).data)


class SpotifyConnectAPIView(APIView):
    """Start Spotify OAuth flow by returning an authorization URL."""

    permission_classes: ClassVar[list[type[permissions.BasePermission]]] = [
        permissions.IsAuthenticated,
    ]

    def get(
        self,
        request: Request,
        *args: Any,  # noqa: ANN401
        **kwargs: Any,  # noqa: ANN401
    ) -> Response:
        """Return a Spotify OAuth authorization URL for the authenticated user."""
        if not _spotify_enabled():
            return Response(
                SPOTIFY_NOT_CONFIGURED_DETAIL,
                status=status.HTTP_400_BAD_REQUEST,
            )

        redirect_path = request.query_params.get("redirect")
        if isinstance(redirect_path, str) and redirect_path.startswith("/"):
            request.session["spotify_oauth_redirect"] = redirect_path
            request.session.modified = True

        state = _get_or_create_spotify_oauth_state(request)

        scopes = " ".join([
            "user-read-email",
            "user-read-private",
            "user-read-playback-state",
            "user-modify-playback-state",
            "user-read-currently-playing",
        ])

        params = {
            "response_type": "code",
            "client_id": settings.SPOTIFY_CLIENT_ID,
            "redirect_uri": settings.SPOTIFY_REDIRECT_URI,
            "scope": scopes,
            "state": state,
        }
        url = f"https://accounts.spotify.com/authorize?{urlencode(params)}"
        return Response({"url": url})


class SpotifyCallbackView(APIView):
    """OAuth callback handler.

    This responds with a redirect so the user lands back in the UI.
    """

    permission_classes: ClassVar[list[type[permissions.BasePermission]]] = [
        permissions.IsAuthenticated,
    ]

    def get(
        self,
        request: Request,
        *args: Any,  # noqa: ANN401
        **kwargs: Any,  # noqa: ANN401
    ) -> HttpResponseRedirect:
        """Handle the Spotify OAuth callback and persist tokens."""
        if not _spotify_enabled():
            return _redirect_to_frontend()

        code = request.query_params.get("code")
        state = request.query_params.get("state")
        expected_state = request.session.get("spotify_oauth_state")
        if not code or not state or not expected_state or state != expected_state:
            return _redirect_to_frontend()

        token_response = requests.post(
            "https://accounts.spotify.com/api/token",
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": settings.SPOTIFY_REDIRECT_URI,
                "client_id": settings.SPOTIFY_CLIENT_ID,
                "client_secret": settings.SPOTIFY_CLIENT_SECRET,
            },
            timeout=10,
        )
        token_response.raise_for_status()
        token_data: dict[str, Any] = token_response.json()

        access_token = str(token_data.get("access_token") or "")
        refresh_token = str(token_data.get("refresh_token") or "")
        expires_in = int(token_data.get("expires_in") or 3600)
        if not access_token or not refresh_token:
            return _redirect_to_frontend()

        profile_response = requests.get(
            "https://api.spotify.com/v1/me",
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=10,
        )
        profile_response.raise_for_status()
        profile: dict[str, Any] = profile_response.json()
        spotify_user_id = str(profile.get("id") or "")
        if not spotify_user_id:
            return _redirect_to_frontend()

        expires_at = timezone.now() + timedelta(seconds=max(0, expires_in - 60))

        SpotifyToken.objects.update_or_create(
            user=request.user,
            defaults={
                "access_token": access_token,
                "refresh_token": refresh_token,
                "expires_at": expires_at,
                "spotify_user_id": spotify_user_id,
            },
        )

        redirect_path = request.query_params.get("redirect")
        if not (isinstance(redirect_path, str) and redirect_path.startswith("/")):
            redirect_path = request.session.pop("spotify_oauth_redirect", None)
            request.session.modified = True

        return _redirect_to_frontend(
            redirect_path if isinstance(redirect_path, str) else None,
        )


class SpotifyPlayAPIView(APIView):
    """Trigger Spotify playback for the connected user.

    This controls the user's active Spotify Connect device.
    """

    permission_classes: ClassVar[list[type[permissions.BasePermission]]] = [
        permissions.IsAuthenticated,
    ]

    def post(
        self,
        request: Request,
        *args: Any,  # noqa: ANN401
        **kwargs: Any,  # noqa: ANN401
    ) -> Response:
        """Start playback on the user's active Spotify Connect device."""
        if not _spotify_enabled():
            return Response(
                SPOTIFY_NOT_CONFIGURED_DETAIL,
                status=status.HTTP_400_BAD_REQUEST,
            )

        track_uri_raw = request.data.get("track_uri")
        if not isinstance(track_uri_raw, str) or not track_uri_raw.strip():
            return Response(
                {"detail": "track_uri is required"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        track_uri = _normalise_spotify_track_uri(track_uri_raw)

        position_ms_raw = request.data.get("position_ms", 0)
        try:
            position_ms = int(float(position_ms_raw))
        except (TypeError, ValueError):
            position_ms = 0
        position_ms = max(0, position_ms)

        try:
            user = request.user
            if not isinstance(user, AbstractBaseUser):
                return Response(
                    AUTHENTICATION_REQUIRED_DETAIL,
                    status=status.HTTP_401_UNAUTHORIZED,
                )
            access_token = _ensure_spotify_access_token(user)
        except RuntimeError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        play_payload: dict[str, Any] = {
            "uris": [track_uri],
            "position_ms": position_ms,
        }

        device_id = request.data.get("device_id")
        query = (
            f"?{urlencode({'device_id': device_id})}"
            if isinstance(device_id, str) and device_id
            else ""
        )

        play_response = requests.put(
            f"https://api.spotify.com/v1/me/player/play{query}",
            headers={
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json",
            },
            json=play_payload,
            timeout=10,
        )

        if play_response.status_code not in {200, 202, 204}:
            return _spotify_play_error_response(play_response)

        return Response({"ok": True})


class SpotifyPauseAPIView(APIView):
    """Pause Spotify playback for the connected user."""

    permission_classes: ClassVar[list[type[permissions.BasePermission]]] = [
        permissions.IsAuthenticated,
    ]

    def post(
        self,
        request: Request,
        *args: Any,  # noqa: ANN401
        **kwargs: Any,  # noqa: ANN401
    ) -> Response:
        """Pause playback on the user's active Spotify Connect device."""
        if not _spotify_enabled():
            return Response(
                SPOTIFY_NOT_CONFIGURED_DETAIL,
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            user = request.user
            if not isinstance(user, AbstractBaseUser):
                return Response(
                    AUTHENTICATION_REQUIRED_DETAIL,
                    status=status.HTTP_401_UNAUTHORIZED,
                )
            access_token = _ensure_spotify_access_token(user)
        except RuntimeError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        device_id = request.data.get("device_id")
        query = (
            f"?{urlencode({'device_id': device_id})}"
            if isinstance(device_id, str) and device_id
            else ""
        )

        pause_response = requests.put(
            f"https://api.spotify.com/v1/me/player/pause{query}",
            headers={
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json",
            },
            timeout=10,
        )

        if pause_response.status_code not in {200, 202, 204}:
            # Keep this permissive: this endpoint is often called as a best-effort
            # follow-up after a snippet play.
            detail = pause_response.text or "Spotify pause failed"
            return Response(
                {"code": "spotify_pause_failed", "detail": detail},
                status=status.HTTP_400_BAD_REQUEST,
            )

        return Response({"ok": True})
