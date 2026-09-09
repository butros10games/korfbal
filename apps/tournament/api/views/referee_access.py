"""Tournament referee access API endpoints and helpers."""

from __future__ import annotations

import base64
from io import BytesIO
from urllib.parse import urlparse
from uuid import UUID
from zoneinfo import ZoneInfo

from django.conf import settings
from django.db import transaction
from django.http import HttpResponse
from django.shortcuts import get_object_or_404
from django.utils.text import slugify
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import extend_schema
import qrcode
from qrcode.image.svg import SvgPathImage
from rest_framework import permissions, status
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.tournament.api.serializers import (
    TournamentRefereeAssignmentSerializer,
    TournamentRefereeClaimSerializer,
)
from apps.tournament.composition import touch_tournament
from apps.tournament.models import TournamentMatch, TournamentTeam
from apps.tournament.services.referee_pdf import (
    RefereeDutyCard,
    build_referee_duties_pdf,
)
from apps.tournament.services.referee_tracker import (
    RefereeTrackerError,
    assign_referee_team,
    build_direct_referee_duty_state,
    build_referee_duties_state,
    claim_direct_referee_duty,
    claim_referee_duty,
    ensure_match_referee_access_token,
    ensure_referee_access_token,
)
from apps.tournament.services.snapshot import build_tournament_snapshot

from .common import (
    get_tournament,
    lock_tournament,
    lock_tournament_for_match,
    require_manager,
)


class TournamentRefereeAssignmentView(APIView):
    """Assign a tournament team to referee one match."""

    @extend_schema(
        request=TournamentRefereeAssignmentSerializer,
        responses={200: OpenApiTypes.OBJECT},
    )
    @transaction.atomic
    def patch(self, request: Request, tournament_id: str, match_id: str) -> Response:
        """Update the duty or release its current guest claim."""
        tournament = lock_tournament(tournament_id)
        require_manager(request, tournament)
        serializer = TournamentRefereeAssignmentSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        match = get_object_or_404(
            TournamentMatch.objects.select_for_update(of=("self",)).select_related(
                "home_team", "away_team", "referee_team"
            ),
            id_uuid=match_id,
            tournament=tournament,
        )
        try:
            assign_referee_team(
                match,
                team_id=serializer.validated_data.get("team_id", match.referee_team_id),
                reset_claim=serializer.validated_data["reset_claim"],
            )
        except RefereeTrackerError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_409_CONFLICT)
        touch_tournament(tournament)
        return Response(build_tournament_snapshot(tournament))


def _referee_access_url(request: Request, access_token: UUID) -> str:
    """Build a scannable web URL for the request's deployed environment."""
    web_origin = str(settings.WEB_APP_ORIGIN).rstrip("/")
    public_api_host = urlparse(str(settings.KORFBAL_ORIGIN)).hostname
    request_host = request.get_host().partition(":")[0].lower()
    if public_api_host and request_host == public_api_host.lower():
        web_origin = str(settings.WEB_KORFBAL_ORIGIN).rstrip("/")
    return f"{web_origin}/tournaments/referee/{access_token}"


class TournamentRefereeQrView(APIView):
    """Generate the team-scoped referee-duty QR for a tournament manager."""

    @extend_schema(responses={200: OpenApiTypes.OBJECT})
    def get(self, request: Request, tournament_id: str, team_id: str) -> Response:
        """Return an embeddable QR without persisting generated image files."""
        tournament = get_tournament(tournament_id)
        require_manager(request, tournament)
        team = get_object_or_404(
            tournament.teams.select_related("tournament"), id_uuid=team_id
        )
        if not team.referee_matches.exists():
            return Response(
                {"detail": "Wijs dit team eerst een wedstrijd toe."},
                status=status.HTTP_409_CONFLICT,
            )
        access_token = ensure_referee_access_token(team)
        access_url = _referee_access_url(request, access_token)
        image = qrcode.make(access_url, image_factory=SvgPathImage)
        output = BytesIO()
        image.save(output)
        encoded = base64.b64encode(output.getvalue()).decode("ascii")
        return Response({
            "team": {"id_uuid": str(team.id_uuid), "name": team.name},
            "qr_data_url": f"data:image/svg+xml;base64,{encoded}",
        })


class TournamentRefereePdfView(APIView):
    """Export every active match as a printable direct-access QR card."""

    @extend_schema(
        responses={
            (200, "application/pdf"): OpenApiTypes.BINARY,
            409: OpenApiTypes.OBJECT,
        }
    )
    def get(
        self,
        request: Request,
        tournament_id: str,
    ) -> Response | HttpResponse:
        """Return an A4 PDF even when knockout referees are not known yet."""
        tournament = get_tournament(tournament_id)
        require_manager(request, tournament)
        matches = list(
            tournament.matches
            .exclude(
                status__in=(
                    TournamentMatch.Status.FINAL,
                    TournamentMatch.Status.CANCELLED,
                )
            )
            .select_related("home_team", "away_team", "field", "referee_team")
            .order_by("starts_at", "match_number")
        )
        if not matches:
            return Response(
                {"detail": "Maak eerst het wedstrijdschema."},
                status=status.HTTP_409_CONFLICT,
            )

        duties: list[RefereeDutyCard] = []
        tournament_timezone = ZoneInfo(tournament.timezone)
        for match in matches:
            referee_team = match.referee_team
            access_url = _referee_access_url(
                request,
                ensure_match_referee_access_token(match),
            )
            starts_at_label = (
                match.starts_at.astimezone(tournament_timezone).strftime("%H:%M")
                if match.starts_at
                else "Tijd nog niet bekend"
            )
            duties.append(
                RefereeDutyCard(
                    referee_team_name=(
                        referee_team.name if referee_team else "Nog niet toegewezen"
                    ),
                    access_url=access_url,
                    match_number=match.match_number,
                    home_team_name=(
                        match.home_team.name if match.home_team else "Nog te bepalen"
                    ),
                    away_team_name=(
                        match.away_team.name if match.away_team else "Nog te bepalen"
                    ),
                    field_label=(
                        match.field.label if match.field else "Veld nog niet bekend"
                    ),
                    starts_at_label=starts_at_label,
                )
            )

        document = build_referee_duties_pdf(tournament.name, duties)
        response = HttpResponse(document, content_type="application/pdf")
        filename = f"{slugify(tournament.name) or 'toernooi'}-scheidsrechter-qr.pdf"
        response["Content-Disposition"] = f'attachment; filename="{filename}"'
        response["Cache-Control"] = "private, no-store"
        return response


class TournamentRefereeDutiesView(APIView):
    """Open team duties or one exact match through an account-free QR."""

    permission_classes = (permissions.AllowAny,)

    @extend_schema(responses={200: OpenApiTypes.OBJECT})
    def get(self, request: Request, access_token: str) -> Response:
        """Return the duties represented by either kind of QR credential."""
        team = (
            TournamentTeam.objects
            .select_related("tournament", "linked_team")
            .filter(referee_access_token=access_token)
            .first()
        )
        if team is not None:
            return Response(build_referee_duties_state(team))

        match = get_object_or_404(
            TournamentMatch.objects.select_related(
                "tournament",
                "field",
                "home_team",
                "away_team",
                "referee_team__tournament",
                "referee_team__linked_team",
            ).exclude(
                status__in=(
                    TournamentMatch.Status.FINAL,
                    TournamentMatch.Status.CANCELLED,
                )
            ),
            referee_access_token=access_token,
        )
        return Response(build_direct_referee_duty_state(match))


class TournamentRefereeClaimView(APIView):
    """Claim a team duty or a directly linked match without an account."""

    permission_classes = (permissions.AllowAny,)

    @extend_schema(
        request=TournamentRefereeClaimSerializer, responses={200: OpenApiTypes.OBJECT}
    )
    @transaction.atomic
    def post(self, request: Request, access_token: str, match_id: str) -> Response:
        """Issue a match-scoped credential after recording the referee's name."""
        serializer = TournamentRefereeClaimSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        team = (
            TournamentTeam.objects
            .select_related("tournament", "linked_team")
            .filter(referee_access_token=access_token)
            .first()
        )
        tournament = lock_tournament_for_match(match_id)
        match_queryset = TournamentMatch.objects.select_for_update(
            of=("self",)
        ).select_related(
            "tournament", "referee_team__tournament", "referee_team__linked_team"
        )
        if team is not None:
            match = get_object_or_404(
                match_queryset,
                id_uuid=match_id,
                tournament=tournament,
                referee_team=team,
            )
        else:
            match = get_object_or_404(
                match_queryset.exclude(
                    status__in=(
                        TournamentMatch.Status.FINAL,
                        TournamentMatch.Status.CANCELLED,
                    )
                ),
                id_uuid=match_id,
                tournament=tournament,
                referee_access_token=access_token,
            )
        try:
            claim_kwargs = {
                "name": serializer.validated_data.get("name", ""),
                "player_id": serializer.validated_data.get("player_id"),
            }
            claim_token = (
                claim_referee_duty(match, team=team, **claim_kwargs)
                if team is not None
                else claim_direct_referee_duty(match, **claim_kwargs)
            )
        except RefereeTrackerError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_409_CONFLICT)
        touch_tournament(tournament)
        return Response({
            "match_id": str(match.id_uuid),
            "claim_token": str(claim_token),
        })
