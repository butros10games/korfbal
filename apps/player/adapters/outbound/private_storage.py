"""Deliver private bucket objects through bounded, signed application URLs."""

from urllib.parse import urlencode

from django.conf import settings
from django.core import signing
from django.urls import reverse
from storages.backends.s3 import S3Storage

from apps.player.media_paths import MEDIA_DOWNLOAD_SALT


# Reads issued in the same window share one URL per object, so clients reuse the
# image they already downloaded and unchanged responses stay byte-identical. A
# URL is dated at the start of its window: it never outlives
# KORFBAL_MEDIA_URL_MAX_AGE and is valid for at least that minus one window.
MEDIA_URL_REUSE_SECONDS = 15 * 60


class _WindowSigner(signing.TimestampSigner):
    """Date a capability at the start of the current reuse window."""

    def timestamp(self) -> str:
        issued = signing.b62_decode(super().timestamp())
        return signing.b62_encode(issued - issued % MEDIA_URL_REUSE_SECONDS)


class PrivateMediaStorage(S3Storage):
    """Keep S3 transport internal and sign public download capabilities separately."""

    def url(
        self,
        name: str | None,
        parameters: dict[str, object] | None = None,
        expire: int | None = None,
        http_method: str | None = None,
    ) -> str:
        """Issue a short-lived download capability, never a public bucket URL."""
        token = _WindowSigner(salt=MEDIA_DOWNLOAD_SALT).sign_object(name)
        path = reverse("media-download")
        return (
            f"{settings.KORFBAL_MEDIA_API_ORIGIN}{path}?{urlencode({'token': token})}"
        )
