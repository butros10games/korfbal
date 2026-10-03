"""Cacheable delivery of the logo already exposed by the public club catalogue."""

from uuid import UUID

from django.http import FileResponse, Http404, HttpResponse, HttpResponseBase
from django.shortcuts import get_object_or_404
from drf_spectacular.utils import extend_schema
from rest_framework import permissions
from rest_framework.request import Request

from apps.club.models import Club
from apps.kwt_common.api.base import KorfbalAPIView
from apps.player.composition import (
    audio_storage as media_storage,
    image_variant_storage,
)
from apps.player.services.image_variants import (
    VARIANT_CONTENT_TYPE,
    VariantBusyError,
    image_variant,
)


def _logo_key(club_id: UUID, version: str) -> str:
    """Resolve only a club's current logo, never an arbitrary media key.

    Raises:
        Http404: The club has no logo with this version.

    """
    club = get_object_or_404(Club, pk=club_id)
    key = club.logo.name or ""
    if (
        not key.startswith("club_pictures/")
        or ".." in key.split("/")
        or not club.accepts_logo_version(version)
    ):
        raise Http404("Logo not found.")
    return key


IMMUTABLE = "public, max-age=2592000, immutable"
# While another request creates the copy, the original stands in only briefly.
STAND_IN = "public, max-age=60"


def _cacheable(
    response: HttpResponseBase, cache_control: str = IMMUTABLE
) -> HttpResponseBase:
    # Versioned URLs never change content.
    response["Cache-Control"] = cache_control
    response["X-Content-Type-Options"] = "nosniff"
    response["Content-Security-Policy"] = "sandbox; default-src 'none'"
    response["Referrer-Policy"] = "no-referrer"
    return response


def _original(key: str, cache_control: str = IMMUTABLE) -> HttpResponseBase:
    try:
        stream = media_storage.open(key)
    except FileNotFoundError as exc:
        raise Http404("Logo not found.") from exc
    return _cacheable(
        FileResponse(stream, filename=key.rsplit("/", 1)[-1], as_attachment=True),
        cache_control,
    )


class ClubLogoAPIView(KorfbalAPIView):
    """Serve a club's uploaded logo file."""

    permission_classes = (permissions.AllowAny,)
    authentication_classes = ()

    @extend_schema(responses={(200, "application/octet-stream"): bytes})
    def get(self, request: Request, club_id: UUID, version: str) -> HttpResponseBase:
        """Serve a versioned logo without expiring bearer tokens."""
        return _original(_logo_key(club_id, version))


class ClubLogoVariantAPIView(KorfbalAPIView):
    """Serve the small copy of a club logo that clients render."""

    permission_classes = (permissions.AllowAny,)
    authentication_classes = ()

    @extend_schema(responses={(200, VARIANT_CONTENT_TYPE): bytes})
    def get(self, request: Request, club_id: UUID, version: str) -> HttpResponseBase:
        """Serve the 256-pixel WebP copy, or the original when it cannot be resized.

        Raises:
            Http404: The logo does not exist.

        """
        key = _logo_key(club_id, version)
        try:
            resized = image_variant(image_variant_storage, key)
        except FileNotFoundError as exc:
            raise Http404("Logo not found.") from exc
        except VariantBusyError:
            return _original(key, STAND_IN)
        if resized is None:
            return _original(key)
        return _cacheable(HttpResponse(resized, content_type=VARIANT_CONTENT_TYPE))
