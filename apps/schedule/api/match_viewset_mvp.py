"""MatchViewSet actions for mvp."""

from __future__ import annotations

from collections.abc import Mapping
import json
from uuid import UUID, uuid4

from django.core import signing
from rest_framework import permissions, status
from rest_framework.decorators import action
from rest_framework.request import Request
from rest_framework.response import Response

from apps.awards.services.mvp import (
    build_match_mvp_status_payload,
    cast_vote,
    cast_vote_anon,
)
from apps.game_tracker.models import MatchData
from apps.player.models.player import Player
from apps.schedule.models import Match

from .constants import (
    MVP_VOTE_COOKIE_MAX_AGE_SECONDS,
    MVP_VOTE_COOKIE_NAME,
    MVP_VOTE_COOKIE_SALT,
)
from .match_viewset_contracts import MatchViewSetContext


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


def _mvp_candidate(candidate_id: str) -> Player | None:
    """Resolve a candidate without passing malformed UUIDs into the ORM."""
    try:
        candidate_uuid = UUID(candidate_id)
    except ValueError:
        return None
    return Player.objects.filter(id_uuid=candidate_uuid).first()


def _cast_mvp_vote_for_request(
    *,
    request: Request,
    match: Match,
    match_data: MatchData,
    player: Player | None,
    candidate: Player,
) -> tuple[str | None, dict[str, str] | None]:
    if player:
        cast_vote(
            match=match,
            match_data=match_data,
            voter=player,
            candidate=candidate,
        )
        return None, None

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
    return anon_voter_token, anon_tokens


class MatchMvpActionsMixin:
    """Schedule actions for mvp."""

    @action(
        detail=True,
        methods=("GET",),
        url_path="mvp",
        permission_classes=[permissions.AllowAny],
    )
    def mvp_status(
        self: MatchViewSetContext,
        request: Request,
        *args: object,
        **kwargs: object,
    ) -> Response:
        """Return MVP voting status + candidates + published winner (if any)."""
        match: Match = self.get_object()
        match_data = self._match_data(match)
        if not match_data or match_data.status != "finished":
            return Response(
                {
                    "available": False,
                    "match_status": match_data.status if match_data else "unknown",
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

        voter = _authenticated_player(request)
        anon_voter_token = None
        if voter is None:
            anon_voter_token = _read_mvp_vote_tokens(request).get(str(match.id_uuid))

        payload = build_match_mvp_status_payload(
            match=match,
            match_data=match_data,
            voter=voter,
            anon_voter_token=anon_voter_token,
        )
        return Response(payload, status=status.HTTP_200_OK)

    @action(
        detail=True,
        methods=("POST",),
        url_path="mvp/vote",
        permission_classes=[permissions.AllowAny],
    )
    def mvp_vote(
        self: MatchViewSetContext,
        request: Request,
        *args: object,
        **kwargs: object,
    ) -> Response:
        """Cast or update the current user's MVP vote."""
        match: Match = self.get_object()
        match_data = self._match_data(match)
        if not match_data or match_data.status != "finished":
            return Response(
                {"detail": "Voting is only available after the match is finished."},
                status=status.HTTP_409_CONFLICT,
            )

        if not isinstance(request.data, Mapping):
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

        candidate = _mvp_candidate(candidate_id)
        if not candidate:
            return Response(
                {"detail": "Unknown candidate."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        player = _authenticated_player(request)

        try:
            anon_voter_token, anon_tokens = _cast_mvp_vote_for_request(
                request=request,
                match=match,
                match_data=match_data,
                player=player,
                candidate=candidate,
            )
        except ValueError as exc:
            return Response(
                {"detail": str(exc)},
                status=status.HTTP_409_CONFLICT,
            )

        payload = build_match_mvp_status_payload(
            match=match,
            match_data=match_data,
            voter=player,
            anon_voter_token=anon_voter_token,
        )

        response = Response(payload, status=status.HTTP_200_OK)
        if not player:
            _write_mvp_vote_tokens(
                request=request,
                response=response,
                tokens=anon_tokens or {},
            )
        return response
