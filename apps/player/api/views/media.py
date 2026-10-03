"""Stream private objects only after validating an expiring download capability."""

import time

from django.conf import settings
from django.core import signing
from django.http import FileResponse, HttpResponse, HttpResponseBase
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import permissions
from rest_framework.request import Request
from rest_framework.response import Response

from apps.kwt_common.api.base import KorfbalAPIView
from apps.player.composition import audio_storage, image_variant_storage
from apps.player.media_paths import (
    MEDIA_DOWNLOAD_SALT,
    PROFILE_PICTURE_PREFIX,
    VARIANT,
)
from apps.player.services.image_variants import (
    VARIANT_CONTENT_TYPE,
    VariantBusyError,
    image_variant,
)


STAND_IN_SECONDS = 60


def _remaining_lifetime(token: str) -> int:
    """Seconds until a validated capability expires."""
    issued = signing.b62_decode(token.rsplit(signing.Signer().sep, 2)[-2])
    return max(0, issued + settings.KORFBAL_MEDIA_URL_MAX_AGE - int(time.time()))


def _protect(
    response: HttpResponseBase, token: str, max_age: int | None = None
) -> HttpResponseBase:
    # The browser may keep the object only as long as the URL itself grants it.
    lifetime = _remaining_lifetime(token)
    response["Cache-Control"] = f"private, max-age={min(lifetime, max_age or lifetime)}"
    response["X-Content-Type-Options"] = "nosniff"
    response["Content-Security-Policy"] = "sandbox; default-src 'none'"
    response["Referrer-Policy"] = "no-referrer"
    return response


class MediaDownloadAPIView(KorfbalAPIView):
    """A signed URL is a time-limited bearer capability minted by authorized reads."""

    permission_classes = (permissions.AllowAny,)
    authentication_classes = ()

    @extend_schema(
        parameters=[
            OpenApiParameter(
                "variant",
                str,
                required=False,
                enum=[VARIANT],
                description="Serve a profile picture as a 256-pixel WebP copy.",
            )
        ],
        responses={(200, "application/octet-stream"): bytes},
    )
    def get(self, request: Request) -> HttpResponseBase:
        """Reject tampered or expired URLs before opening any stored bytes."""
        token = request.query_params.get("token", "")
        try:
            key = signing.loads(
                token,
                salt=MEDIA_DOWNLOAD_SALT,
                max_age=settings.KORFBAL_MEDIA_URL_MAX_AGE,
            )
        except signing.BadSignature:
            return Response({"detail": "Invalid or expired media URL."}, status=403)
        if not isinstance(key, str) or not key or ".." in key.split("/"):
            return Response({"detail": "Invalid media URL."}, status=403)
        # While another request creates the copy, the original stands in briefly.
        stand_in = None
        try:
            if request.query_params.get("variant") == VARIANT and key.startswith(
                PROFILE_PICTURE_PREFIX
            ):
                try:
                    variant = image_variant(image_variant_storage, key)
                except VariantBusyError:
                    variant, stand_in = None, STAND_IN_SECONDS
                if variant is not None:
                    return _protect(
                        HttpResponse(variant, content_type=VARIANT_CONTENT_TYPE),
                        token,
                    )
            stream = audio_storage.open(key)
        except FileNotFoundError:
            return Response({"detail": "Media not found."}, status=404)
        return _protect(
            FileResponse(stream, filename=key.rsplit("/", 1)[-1], as_attachment=True),
            token,
            stand_in,
        )
