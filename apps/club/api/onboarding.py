"""First-run account setup: choose spectator, player or club representative."""

from __future__ import annotations

from typing import Any

from django.shortcuts import get_object_or_404
from django_ratelimit.core import is_ratelimited
from rest_framework import permissions, serializers, status
from rest_framework.request import Request
from rest_framework.response import Response

from apps.club.composition import join_request_ports
from apps.club.models import Club, ClubJoinRequest
from apps.club.services.join_requests import (
    JoinRequestError,
    choose_spectator,
    club_teams,
    find_knkv_player,
    request_club_admin,
    request_player_membership,
    search_clubs,
    withdraw_request,
)
from apps.kwt_common.api.base import KorfbalAPIView
from apps.player.models import Player
from apps.team.models import Team


# Looking up a person ID reveals the imported name; keep guessing impractical.
# Player requests with a relation number resolve it too and share these limits.
LOOKUP_LIMITS = ("korfbal.onboarding_knkv_lookup", (("ip", "20/h"), ("user", "10/h")))
# Each request notifies club admins or the support inbox.
REQUEST_LIMITS = ("korfbal.onboarding_request", (("ip", "10/h"), ("user", "5/h")))
TOO_MANY_ATTEMPTS = "Te veel pogingen. Probeer het over een uur opnieuw."


def _limited(
    request: Request, limits: tuple[str, tuple[tuple[str, str], ...]]
) -> Response | None:
    """Count the attempt against every key and refuse it when one is exhausted."""
    group, rates = limits
    hits = [
        is_ratelimited(
            request._request, group=group, key=key, rate=rate, increment=True
        )
        for key, rate in rates
    ]
    if any(hits):
        return Response(
            {"detail": TOO_MANY_ATTEMPTS}, status=status.HTTP_429_TOO_MANY_REQUESTS
        )
    return None


def _player(request: Request) -> Player:
    player, _ = Player.all_objects.get_or_create(user=request.user)
    return player


def _club(club: Club | None) -> dict[str, Any] | None:
    if club is None:
        return None
    return {
        "id_uuid": str(club.pk),
        "name": club.name,
        "logo_url": club.get_club_logo(),
    }


def _team(team: Team | None) -> dict[str, Any] | None:
    return None if team is None else {"id_uuid": str(team.pk), "name": team.name}


def serialize_join_request(request: ClubJoinRequest) -> dict[str, Any]:
    """Represent one of the account's own requests."""
    return {
        "id_uuid": str(request.pk),
        "kind": request.kind,
        "status": request.status,
        "club": _club(request.club),
        "team": _team(request.team),
        "knkv_person_id": request.knkv_person_id or None,
        "link_status": request.link_status or None,
        "created_at": request.created_at.isoformat(),
        "decided_at": request.decided_at.isoformat() if request.decided_at else None,
    }


def _state(player: Player) -> dict[str, Any]:
    requests = (
        ClubJoinRequest.objects
        .filter(player=player)
        .exclude(status=ClubJoinRequest.Status.WITHDRAWN)
        .select_related("club", "team")[:10]
    )
    return {
        "role": player.account_role or None,
        "completed": player.onboarded_at is not None,
        "knkv_linked": bool(player.knkv_person_id),
        "requests": [serialize_join_request(request) for request in requests],
    }


def _error(exc: JoinRequestError) -> Response:
    return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)


class OnboardingStateView(KorfbalAPIView):
    """The account's setup state and its club requests."""

    permission_classes = (permissions.IsAuthenticated,)

    def get(self, request: Request) -> Response:
        """Return whether setup is complete and which requests are open."""
        return Response(_state(_player(request)))


class OnboardingSpectatorView(KorfbalAPIView):
    """Finish setup as a spectator."""

    permission_classes = (permissions.IsAuthenticated,)

    def post(self, request: Request) -> Response:
        """Record the spectator role."""
        player = _player(request)
        choose_spectator(player)
        player.refresh_from_db()
        return Response(_state(player))


class KnkvLookupSerializer(serializers.Serializer):
    """A KNKV person ID (relatienummer) as shown on the digital player card."""

    knkv_person_id = serializers.RegexField(r"^\s*[A-Za-z0-9]{4,20}\s*$")


class OnboardingKnkvLookupView(KorfbalAPIView):
    """Find the imported player for a KNKV person ID, without linking it."""

    permission_classes = (permissions.IsAuthenticated,)
    serializer_class = KnkvLookupSerializer

    def post(self, request: Request) -> Response:
        """Return the matching player's name, club and latest team."""
        serializer = KnkvLookupSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        if limited := _limited(request, LOOKUP_LIMITS):
            return limited
        match = find_knkv_player(serializer.validated_data["knkv_person_id"])
        if match is None:
            return Response({"match": None})
        return Response({
            "match": {
                "knkv_person_id": match.player.knkv_person_id,
                "name": match.player.display_name,
                "club": _club(match.club),
                "team": _team(match.team),
            }
        })


class OnboardingClubSearchView(KorfbalAPIView):
    """Search active clubs for the setup club picker."""

    permission_classes = (permissions.IsAuthenticated,)

    def get(self, request: Request) -> Response:
        """Return up to 20 clubs whose name contains the search term."""
        term = request.query_params.get("search") or ""
        return Response({"results": [_club(club) for club in search_clubs(term)]})


class OnboardingClubTeamsView(KorfbalAPIView):
    """List a club's current teams for the setup team picker."""

    permission_classes = (permissions.IsAuthenticated,)

    def get(self, request: Request, club_id: str) -> Response:
        """Return the club's teams in the running season."""
        club = get_object_or_404(Club, pk=club_id, dissolved=False)
        return Response({"results": [_team(team) for team in club_teams(club)]})


class PlayerRequestSerializer(serializers.Serializer):
    """Join a club as a player."""

    club_id = serializers.UUIDField()
    team_id = serializers.UUIDField(required=False, allow_null=True)
    knkv_person_id = serializers.CharField(
        required=False, allow_blank=True, max_length=20
    )


class OnboardingPlayerView(KorfbalAPIView):
    """Register as a player; the club admin confirms the request."""

    permission_classes = (permissions.IsAuthenticated,)
    serializer_class = PlayerRequestSerializer

    def post(self, request: Request) -> Response:
        """Create the join request and finish setup."""
        serializer = PlayerRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        if limited := _limited(request, REQUEST_LIMITS):
            return limited
        if data.get("knkv_person_id") and (limited := _limited(request, LOOKUP_LIMITS)):
            return limited
        club = get_object_or_404(Club, pk=data["club_id"], dissolved=False)
        team = (
            get_object_or_404(Team, pk=data["team_id"]) if data.get("team_id") else None
        )
        player = _player(request)
        try:
            request_player_membership(
                player=player,
                club=club,
                team=team,
                knkv_person_id=data.get("knkv_person_id") or "",
                ports=join_request_ports,
            )
        except JoinRequestError as exc:
            return _error(exc)
        player.refresh_from_db()
        return Response(_state(player), status=status.HTTP_201_CREATED)


class ClubClaimSerializer(serializers.Serializer):
    """Claim to represent a club."""

    club_id = serializers.UUIDField()
    note = serializers.CharField(max_length=200, allow_blank=False)


class OnboardingClubView(KorfbalAPIView):
    """Register a club; the platform team verifies the representative."""

    permission_classes = (permissions.IsAuthenticated,)
    serializer_class = ClubClaimSerializer

    def post(self, request: Request) -> Response:
        """Create the club claim and finish setup."""
        serializer = ClubClaimSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        if limited := _limited(request, REQUEST_LIMITS):
            return limited
        club = get_object_or_404(
            Club, pk=serializer.validated_data["club_id"], dissolved=False
        )
        player = _player(request)
        try:
            request_club_admin(
                player=player,
                club=club,
                note=serializer.validated_data["note"],
                ports=join_request_ports,
            )
        except JoinRequestError as exc:
            return _error(exc)
        player.refresh_from_db()
        return Response(_state(player), status=status.HTTP_201_CREATED)


class OnboardingRequestView(KorfbalAPIView):
    """Withdraw one of the account's own pending requests."""

    permission_classes = (permissions.IsAuthenticated,)

    def delete(self, request: Request, request_id: str) -> Response:
        """Withdraw the request if it is still pending."""
        if not withdraw_request(player=_player(request), request_id=request_id):
            return Response(
                {"detail": "Aanmelding niet gevonden."},
                status=status.HTTP_404_NOT_FOUND,
            )
        return Response(status=status.HTTP_204_NO_CONTENT)
