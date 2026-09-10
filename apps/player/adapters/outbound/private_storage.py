"""Deliver private bucket objects through bounded, signed application URLs."""

from urllib.parse import urlencode

from django.conf import settings
from django.core import signing
from django.urls import reverse
from storages.backends.s3 import S3Storage

from apps.player.media_paths import MEDIA_DOWNLOAD_SALT


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
        token = signing.dumps(name, salt=MEDIA_DOWNLOAD_SALT)
        path = reverse("media-download")
        return (
            f"{settings.KORFBAL_MEDIA_API_ORIGIN}{path}?{urlencode({'token': token})}"
        )
