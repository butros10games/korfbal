"""HTTP endpoints for creating, revoking and redeeming tracker links."""

from typing import Any

from drf_spectacular.utils import extend_schema
from rest_framework import permissions, serializers
from rest_framework.authentication import SessionAuthentication
from rest_framework.decorators import action
from rest_framework.exceptions import NotFound, PermissionDenied
from rest_framework.request import Request
from rest_framework.response import Response

from apps.game_tracker.models import TrackerAccessLink
from apps.game_tracker.services.tracker_access import (
    SESSION_KEY,
    active_tracker_links,
    issue_tracker_link,
    token_digest,
)

from .permissions import IsClubMemberOrCoachOrAdmin, _get_match_and_team
from .validation import UUID_URL_REGEX


class TrackerLinkStatusSerializer(serializers.Serializer):
    """Sharing permission and invitation expiry, with a one-time secret response."""

    can_share = serializers.BooleanField()
    expires_at = serializers.DateTimeField(allow_null=True)
    token = serializers.CharField(required=False)


class TrackerLinkRedeemSerializer(serializers.Serializer):
    """Invitation exchange input."""

    token = serializers.CharField(max_length=128, trim_whitespace=False)


class TrackerAccessActionsMixin:
    """Keep sharing authorization separate from delegated tracker access."""

    @extend_schema(request=None, responses=TrackerLinkStatusSerializer)
    @action(
        detail=True,
        methods=["GET", "POST", "DELETE"],
        url_path=rf"tracker/(?P<team_id>{UUID_URL_REGEX})/access",
        permission_classes=[permissions.AllowAny],
    )
    def tracker_access(
        self, request: Request, team_id: str, **kwargs: object
    ) -> Response:
        """Manage invitations for existing trackers.

        Raises:
            NotFound: The match/team scope does not exist.
            PermissionDenied: The caller cannot manage invitations.

        """
        match, team = _get_match_and_team(self, require_team=True)
        if not match or not team:
            raise NotFound()
        can_share = IsClubMemberOrCoachOrAdmin().has_permission(request, self)
        if request.method != "GET" and not can_share:
            raise PermissionDenied()
        payload: dict[str, Any] = {"can_share": can_share, "expires_at": None}
        if request.method == "POST":
            token, link = issue_tracker_link(match, team)
            payload.update(token=token, expires_at=link.expires_at)
        elif request.method == "DELETE":
            TrackerAccessLink.objects.filter(match=match, team=team).delete()
        elif can_share:
            link = active_tracker_links(match, team).first()
            payload["expires_at"] = link.expires_at if link else None
        return Response(payload, headers={"Cache-Control": "no-store"})

    @extend_schema(request=TrackerLinkRedeemSerializer, responses=None)
    @action(
        detail=True,
        methods=["POST"],
        url_path=rf"tracker/(?P<team_id>{UUID_URL_REGEX})/access/redeem",
        permission_classes=[permissions.AllowAny],
    )
    def tracker_access_redeem(
        self, request: Request, team_id: str, **kwargs: object
    ) -> Response:
        """Exchange an invitation for a scoped session without logging in a user.

        Raises:
            NotFound: The match/team scope does not exist.
            PermissionDenied: The invitation is invalid or no longer active.

        """
        SessionAuthentication().enforce_csrf(request)
        data = TrackerLinkRedeemSerializer(data=request.data)
        data.is_valid(raise_exception=True)
        match, team = _get_match_and_team(self, require_team=True)
        if not match or not team:
            raise NotFound()
        digest = token_digest(data.validated_data["token"])
        if not active_tracker_links(match, team).filter(token_hash=digest).exists():
            raise PermissionDenied("This tracker link is invalid, expired or revoked.")
        grants = request.session.get(SESSION_KEY, {})
        # Bound session size, including when signed-cookie sessions are configured.
        grants = dict(list(grants.items())[-19:])
        grants[f"{match.pk}:{team.pk}"] = digest
        request.session[SESSION_KEY] = grants
        return Response(status=204, headers={"Cache-Control": "no-store"})
