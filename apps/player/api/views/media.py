"""Stream private objects only after validating an expiring download capability."""

from django.conf import settings
from django.core import signing
from django.http import FileResponse
from drf_spectacular.utils import extend_schema
from rest_framework import permissions
from rest_framework.request import Request
from rest_framework.response import Response

from apps.kwt_common.api.base import KorfbalAPIView
from apps.player.composition import audio_storage
from apps.player.media_paths import MEDIA_DOWNLOAD_SALT


class MediaDownloadAPIView(KorfbalAPIView):
    """A signed URL is a time-limited bearer capability minted by authorized reads."""

    permission_classes = (permissions.AllowAny,)
    authentication_classes = ()

    @extend_schema(responses={(200, "application/octet-stream"): bytes})
    def get(self, request: Request) -> FileResponse | Response:
        """Reject tampered or expired URLs before opening any stored bytes."""
        try:
            key = signing.loads(
                request.query_params.get("token", ""),
                salt=MEDIA_DOWNLOAD_SALT,
                max_age=settings.KORFBAL_MEDIA_URL_MAX_AGE,
            )
        except signing.BadSignature:
            return Response({"detail": "Invalid or expired media URL."}, status=403)
        if not isinstance(key, str) or not key or ".." in key.split("/"):
            return Response({"detail": "Invalid media URL."}, status=403)
        try:
            stream = audio_storage.open(key)
        except FileNotFoundError:
            return Response({"detail": "Media not found."}, status=404)
        response = FileResponse(
            stream, filename=key.rsplit("/", 1)[-1], as_attachment=True
        )
        response["Cache-Control"] = "private, no-store"
        response["X-Content-Type-Options"] = "nosniff"
        response["Content-Security-Policy"] = "sandbox; default-src 'none'"
        response["Referrer-Policy"] = "no-referrer"
        return response
