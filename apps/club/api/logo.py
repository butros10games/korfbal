"""Cacheable delivery of the logo already exposed by the public club catalogue."""

from uuid import UUID

from django.http import FileResponse, Http404
from django.shortcuts import get_object_or_404
from drf_spectacular.utils import extend_schema
from rest_framework import permissions
from rest_framework.request import Request

from apps.club.models import Club
from apps.kwt_common.api.base import KorfbalAPIView
from apps.player.composition import audio_storage as media_storage


class ClubLogoAPIView(KorfbalAPIView):
    """Resolve only a club's current public logo, never an arbitrary media key."""

    permission_classes = (permissions.AllowAny,)
    authentication_classes = ()

    @extend_schema(responses={(200, "application/octet-stream"): bytes})
    def get(self, request: Request, club_id: UUID, version: str) -> FileResponse:
        """Serve a versioned logo without expiring bearer tokens.

        Raises:
            Http404: The club has no matching public logo or its file is missing.

        """
        club = get_object_or_404(Club, pk=club_id)
        key = club.logo.name or ""
        if (
            not key.startswith("club_pictures/")
            or ".." in key.split("/")
            or version != club.logo_version
        ):
            raise Http404("Logo not found.")
        try:
            stream = media_storage.open(key)
        except FileNotFoundError as exc:
            raise Http404("Logo not found.") from exc
        response = FileResponse(
            stream, filename=key.rsplit("/", 1)[-1], as_attachment=True
        )
        response["Cache-Control"] = "public, max-age=2592000, immutable"
        response["X-Content-Type-Options"] = "nosniff"
        response["Content-Security-Policy"] = "sandbox; default-src 'none'"
        response["Referrer-Policy"] = "no-referrer"
        return response
